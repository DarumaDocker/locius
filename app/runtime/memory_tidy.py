"""Daily memory housekeeping: prune → drop sensitive → merge duplicates → LLM review → report.

Safety rules
* The profile is never changed here: profile details found in facts only become pending suggestions.
* Nothing the LLM proposes deletes a long-term fact outright. "Demote" moves it to recent memory, where it
  expires after 30 days (and can still be promoted back from the Memory page). Merges keep the merged-away
  texts in the surviving fact's history.
* A dry run computes the full plan and stores it; "apply" executes exactly that stored plan (ids re-checked),
  so what the user previewed is what happens.
"""
from __future__ import annotations

import difflib
import re
import time
from datetime import datetime

from app.common.util import truncate
from app.runtime.llm import extract_json
from app.runtime.store import looks_sensitive, norm_fact, profile_key, profile_value_ok

BATCH = 60
NEAR_DUP = 0.93

TIDY_PROMPT = """You are tidying a personal assistant's long-term memory about its user. Below are memory facts, one per line as `id | tier | category | entity | uses | fact`.
tier "long" = kept for good; tier "recent" = one-off detail that expires after 30 days.

Return ONLY JSON with these optional lists (use ids exactly as given, never invent ids):
{"merge":   [{"ids": ["<id>", "<id>", ...], "fact": "<one combined short third-person sentence>", "category": "<preference|person|company|project|habit|other>"}],
 "rewrite": [{"id": "<id>", "fact": "<clearer, shorter sentence with the same meaning>"}],
 "demote":  [{"id": "<id>", "reason": "<why it is one-off / outdated / not about the user>"}],
 "promote": [{"id": "<id>", "reason": "<why this recent fact is durable>"}],
 "profile": [{"field": "<name_zh|name_en|preferred_name|phone|email_personal|email_work|address_home|address_work|company|job_title|birthday|nationality>", "value": "<value>", "ids": ["<id>"], "reason": "<short>"}]}

Rules:
- merge: facts that say the same thing or are clearly about one subject and read better as one sentence. Keep every detail.
- rewrite: only when the text is messy (logs, tool output, very long). Never change meaning and never add details or examples.
- demote (long → recent): process notes, task logs, one-off bookings/orders/dates, things that are no longer true, facts about web pages rather than the user.
- promote (recent → long): stable preferences, people, companies, habits that will matter for months.
- profile: a fixed personal detail of the user that belongs in their profile (name, phone, email, address, company, title, birthday, nationality). These are only suggested; the user confirms.
- Never output ID / passport / membership / card numbers, passwords or codes.
- When unsure, leave a fact alone. An empty answer {} is fine.

Facts:
"""


def _mask(text: str) -> str:
    return re.sub(r"\d(?=[\d -]{3,}\d)", "•", text or "")


def _line(f: dict) -> str:
    fact = re.sub(r"\s+", " ", f["fact"])[:300].replace("|", "/")
    return f"{f['id']} | {f.get('tier') or 'long'} | {f.get('category') or ''} | {(f.get('entity') or '')[:40]} | {f.get('uses') or 0} | {fact}"


def _all_facts(store) -> list[dict]:
    return store.facts(5000, tier=None)


def counts(store) -> dict:
    fs = _all_facts(store)
    return {"long": sum(1 for f in fs if (f.get("tier") or "long") == "long"),
            "recent": sum(1 for f in fs if f.get("tier") == "recent"),
            "episodes": len(store.episodes(100000)), "profile": len(store.profile()),
            "pending": len(store.profile_pending())}


_FILLER_WORDS = {"a", "an", "the"}
_FILLER_CJK = set("的了地得之着")


def _tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[A-Za-z0-9']+|[^\sA-Za-z0-9'\W]", (text or "").lower()):
        if re.fullmatch(r"[a-z]+", w) and len(w) > 3:
            w = re.sub(r"(es|s)$", "", w)       # seat / seats
        out.append(w)
    return out


def _near_dup(a: str, b: str) -> bool:
    """The same sentence up to articles, plurals or 的/了 ("an aisle seat" / "aisle seats").
    A different word, name, number or a negation is never a duplicate."""
    ta, tb = _tokens(a), _tokens(b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    if sm.real_quick_ratio() < NEAR_DUP - 0.1:
        return False
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        for w in ta[i1:i2] + tb[j1:j2]:
            if w not in _FILLER_WORDS and w not in _FILLER_CJK:
                return False
    return True


# ------------------------------------------------------------------ planning (no writes)
def _plan_local(store) -> dict:
    now = time.time()
    plan: dict = {"prune_facts": [], "prune_episodes": 0, "sensitive": [], "dups": [], "auto_promote": []}
    fs = store.db.all("SELECT * FROM facts")
    plan["prune_facts"] = [f["id"] for f in fs if f.get("expires_at") and f["expires_at"] < now]
    cut = now - store.RECENT_DAYS * 86400
    plan["prune_episodes"] = store.db.one("SELECT COUNT(*) AS n FROM episodes WHERE ts < ?", (cut,))["n"]
    live = [f for f in fs if f["id"] not in set(plan["prune_facts"]) and store._live(f)]
    for f in live:
        if looks_sensitive(f["fact"]):
            plan["sensitive"].append({"id": f["id"], "preview": _mask(truncate(f["fact"], 80))})
    gone = {x["id"] for x in plan["sensitive"]}
    live = [f for f in live if f["id"] not in gone]
    # duplicates: same normalised text, or nearly the same within one entity/category
    live.sort(key=lambda f: (-(f.get("uses") or 0), (f.get("tier") or "long") != "long", f["created_at"]))
    taken: set[str] = set()
    norms = {f["id"]: norm_fact(f["fact"]) for f in live}
    for i, a in enumerate(live):
        if a["id"] in taken:
            continue
        group = []
        for b in live[i + 1:]:
            if b["id"] in taken:
                continue
            same = norms[a["id"]] == norms[b["id"]]
            if not same and (a.get("entity") or "") == (b.get("entity") or ""):
                same = _near_dup(a["fact"], b["fact"])
            if same:
                group.append(b["id"])
                taken.add(b["id"])
        if group:
            taken.add(a["id"])
            plan["dups"].append({"keep": a["id"], "drop": group, "fact": a["fact"]})
    # recent facts the agent keeps using are evidently durable
    plan["auto_promote"] = [f["id"] for f in live if f.get("tier") == "recent" and (f.get("uses") or 0) >= 2
                            and f["id"] not in taken]
    return plan


async def _plan_llm(rt, skip: set[str]) -> tuple[dict, list[str]]:
    store = rt.store
    facts = [f for f in _all_facts(store) if f["id"] not in skip]
    out: dict = {"merge": [], "rewrite": [], "demote": [], "promote": [], "profile": []}
    errors: list[str] = []
    # group by entity/category so related facts land in the same batch
    facts.sort(key=lambda f: ((f.get("entity") or "~").lower(), f.get("category") or "", f["created_at"]))
    for i in range(0, len(facts), BATCH):
        chunk = facts[i:i + BATCH]
        ids = {f["id"]: f for f in chunk}
        prompt = TIDY_PROMPT + "\n".join(_line(f) for f in chunk)
        try:
            r = await rt.llm.chat([{"role": "user", "content": prompt}], purpose="memory", task_id="",
                                  max_tokens=3000, temperature=0.1, no_think=True)
            data = extract_json(r["content"]) or {}
        except Exception as e:
            errors.append(f"batch {i // BATCH + 1}: {str(e)[:160]}")
            continue
        if not isinstance(data, dict):
            continue
        used: set[str] = set()

        def ok(fid) -> bool:
            return isinstance(fid, str) and fid in ids and fid not in used

        for m in data.get("merge") or []:
            mids = [x for x in (m.get("ids") or []) if ok(x)] if isinstance(m, dict) else []
            mids = list(dict.fromkeys(mids))
            text = str((m or {}).get("fact") or "").strip()
            if len(mids) < 2 or not text or len(text) > 400 or looks_sensitive(text):
                continue
            used.update(mids)
            cat = str(m.get("category") or "").lower()
            keep = max(mids, key=lambda x: ((ids[x].get("tier") or "long") == "long", ids[x].get("uses") or 0))
            out["merge"].append({"keep": keep, "drop": [x for x in mids if x != keep], "fact": text,
                                 "category": cat if cat in ("preference", "person", "company", "project", "habit", "other") else "",
                                 "before": [ids[x]["fact"] for x in mids]})
        for w in data.get("rewrite") or []:
            fid, text = (w or {}).get("id"), str((w or {}).get("fact") or "").strip()
            # a rewrite tidies; it must not grow (that is how invented details sneak in)
            if ok(fid) and text and len(text) <= int(len(ids[fid]["fact"]) * 1.1) + 5 \
                    and norm_fact(text) != norm_fact(ids[fid]["fact"]) and not looks_sensitive(text):
                used.add(fid)
                out["rewrite"].append({"id": fid, "fact": text, "before": ids[fid]["fact"]})
        for d in data.get("demote") or []:
            fid = (d or {}).get("id")
            if ok(fid) and (ids[fid].get("tier") or "long") == "long":
                used.add(fid)
                out["demote"].append({"id": fid, "fact": ids[fid]["fact"], "reason": str(d.get("reason") or "")[:160]})
        for p in data.get("promote") or []:
            fid = (p or {}).get("id")
            if ok(fid) and ids[fid].get("tier") == "recent":
                used.add(fid)
                out["promote"].append({"id": fid, "fact": ids[fid]["fact"], "reason": str(p.get("reason") or "")[:160]})
        for p in data.get("profile") or []:
            if not isinstance(p, dict):
                continue
            key, val = profile_key(str(p.get("field") or "")), str(p.get("value") or "").strip()
            if key and val and not looks_sensitive(val) and len(val) <= 300 and profile_value_ok(key, val):
                out["profile"].append({"field": key, "value": val, "reason": str(p.get("reason") or "")[:160],
                                       "ids": [x for x in (p.get("ids") or []) if x in ids]})
    # one suggestion per field: the most complete value (e.g. the address with the unit number)
    best: dict[str, dict] = {}
    for s in out["profile"]:
        if s["field"] not in best or len(s["value"]) > len(best[s["field"]]["value"]):
            best[s["field"]] = s
    out["profile"] = list(best.values())
    return out, errors


async def plan(rt) -> dict:
    local = _plan_local(rt.store)
    skip = set(local["prune_facts"]) | {x["id"] for x in local["sensitive"]} | \
        {d for g in local["dups"] for d in g["drop"]}
    llm, errors = await _plan_llm(rt, skip)
    # don't let the LLM act on facts the local pass already handles
    return {"local": local, "llm": llm, "errors": errors}


# ------------------------------------------------------------------ applying a plan
def apply(store, p: dict) -> dict:
    done = {"pruned_facts": 0, "pruned_episodes": 0, "sensitive": 0, "merged": 0, "rewritten": 0, "demoted": 0,
            "promoted": 0, "profile_suggestions": 0, "skipped": 0}
    local, llm = p.get("local") or {}, p.get("llm") or {}
    for fid in local.get("prune_facts") or []:
        if store.fact(fid):
            store.delete_fact(fid)
            done["pruned_facts"] += 1
    cut = time.time() - store.RECENT_DAYS * 86400
    done["pruned_episodes"] = store.db.one("SELECT COUNT(*) AS n FROM episodes WHERE ts < ?", (cut,))["n"]
    store.db.execute("DELETE FROM episodes WHERE ts < ?", (cut,))
    for s in local.get("sensitive") or []:
        if store.fact(s["id"]):
            store.delete_fact(s["id"])
            done["sensitive"] += 1

    def merge(keep: str, drop: list[str], text: str | None, cat: str = ""):
        k = store.fact(keep)
        others = [store.fact(x) for x in drop]
        others = [o for o in others if o]
        if not k or not others:
            done["skipped"] += 1
            return False
        hist = "\n".join(o["fact"] for o in others)
        upd = {"uses": (k.get("uses") or 0) + sum(o.get("uses") or 0 for o in others),
               "history": ((k.get("history") or "") + "\n" + hist).strip()[-2000:]}
        if any((o.get("tier") or "long") == "long" for o in others) and k.get("tier") == "recent":
            upd["tier"] = "long"
        if cat:
            upd["category"] = cat
        if text and text != k["fact"]:
            upd["fact"] = text
        store.update_fact(keep, **upd)
        for o in others:
            store.delete_fact(o["id"])
        return True

    for g in local.get("dups") or []:
        if merge(g["keep"], g["drop"], None):
            done["merged"] += len(g["drop"])
    for m in llm.get("merge") or []:
        if merge(m["keep"], m["drop"], m["fact"], m.get("category") or ""):
            done["merged"] += len(m["drop"])
    for w in llm.get("rewrite") or []:
        f = store.fact(w["id"])
        if f and f["fact"] == w.get("before", f["fact"]):
            store.update_fact(w["id"], fact=w["fact"])
            done["rewritten"] += 1
        else:
            done["skipped"] += 1
    for d in llm.get("demote") or []:
        f = store.fact(d["id"])
        if f and (f.get("tier") or "long") == "long":
            store.update_fact(d["id"], tier="recent")
            done["demoted"] += 1
    for pid in (local.get("auto_promote") or []) + [x["id"] for x in llm.get("promote") or []]:
        f = store.fact(pid)
        if f and f.get("tier") == "recent":
            store.update_fact(pid, tier="long")
            done["promoted"] += 1
    for s in llm.get("profile") or []:
        if store.suggest_profile(s["field"], s["value"], reason=s.get("reason") or "memory tidy", source="memory-tidy"):
            done["profile_suggestions"] += 1
    return done


def summary_lines(p: dict, done: dict | None, before: dict, after: dict | None, en: bool) -> list[str]:
    local, llm = p.get("local") or {}, p.get("llm") or {}
    if done is None:   # preview
        n = {"pruned_facts": len(local.get("prune_facts") or []), "pruned_episodes": local.get("prune_episodes") or 0,
             "sensitive": len(local.get("sensitive") or []),
             "merged": sum(len(g["drop"]) for g in local.get("dups") or []) + sum(len(m["drop"]) for m in llm.get("merge") or []),
             "rewritten": len(llm.get("rewrite") or []), "demoted": len(llm.get("demote") or []),
             "promoted": len(local.get("auto_promote") or []) + len(llm.get("promote") or []),
             "profile_suggestions": len(llm.get("profile") or [])}
    else:
        n = done
    if en:
        lines = [f"Long-term {before['long']} · recent {before['recent']} · episodes {before['episodes']}"
                 + (f" → long-term {after['long']} · recent {after['recent']} · episodes {after['episodes']}" if after else ""),
                 f"Merged duplicates: {n['merged']} · rewritten: {n['rewritten']}",
                 f"Moved to recent (expire in 30 days): {n['demoted']} · kept for good: {n['promoted']}",
                 f"Expired / removed: {n['pruned_facts']} facts, {n['pruned_episodes']} episodes"]
        if n["sensitive"]:
            lines.append(f"Removed {n['sensitive']} facts with ID/card numbers or passwords (put those in the vault)")
        if n["profile_suggestions"]:
            lines.append(f"Profile suggestions waiting for you: {n['profile_suggestions']} (Memory page)")
    else:
        lines = [f"长期 {before['long']} 条 · 近期 {before['recent']} 条 · 经历 {before['episodes']} 条"
                 + (f" → 长期 {after['long']} · 近期 {after['recent']} · 经历 {after['episodes']}" if after else ""),
                 f"合并重复：{n['merged']} 条 · 改写：{n['rewritten']} 条",
                 f"降为近期（30 天后过期）：{n['demoted']} 条 · 升为长期：{n['promoted']} 条",
                 f"过期清理：记忆 {n['pruned_facts']} 条，经历 {n['pruned_episodes']} 条"]
        if n["sensitive"]:
            lines.append(f"删除了 {n['sensitive']} 条含证件号/卡号/密码的记忆（这类信息请放进保险箱）")
        if n["profile_suggestions"]:
            lines.append(f"有 {n['profile_suggestions']} 条档案修改建议等你确认（记忆页）")
    return lines


async def run(rt, *, dry_run: bool, kind: str = "manual", from_run: int | None = None) -> dict:
    """dry_run → compute & store a plan. from_run → apply that stored plan. Otherwise plan + apply at once."""
    store = rt.store
    en = store.settings().get("language") == "en"
    before = counts(store)
    if from_run:
        prev = next((r for r in store.memory_runs(50) if r["id"] == from_run), None)
        if not prev or prev["kind"] != "dry_run" or (prev["report"] or {}).get("applied"):
            raise ValueError("该预览不存在或已执行 (preview not found or already applied)")
        p = prev["report"]["plan"]
    else:
        p = await plan(rt)
    report = {"plan": p, "before": before, "errors": p.get("errors") or [], "ts": time.time()}
    if dry_run:
        report["lines"] = summary_lines(p, None, before, None, en)
        report["id"] = store.add_memory_run("dry_run", report)
        return report
    done = apply(store, p)
    after = counts(store)
    report.update(done=done, after=after, lines=summary_lines(p, done, before, after, en), from_run=from_run)
    report["id"] = store.add_memory_run("applied" if from_run else kind, report)
    if from_run:
        prev["report"]["applied"] = report["id"]
        from app.common.util import dumps
        store.db.execute("UPDATE memory_runs SET report=? WHERE id=?", (dumps(prev["report"]), from_run))
    await rt.audit("memory", "memory.consolidate", result="success", detail={"kind": kind, "done": done, "before": before, "after": after})
    await rt.publish({"kind": "memory_update"})
    changed = any(v for k, v in done.items() if k != "skipped") or report["errors"]
    if not changed and kind == "daily":
        return report          # a quiet day: no Telegram message
    title = "🧠 Memory tidied" if en else "🧠 记忆整理完成"
    body = "\n".join(report["lines"])
    try:
        await rt.sentinel("POST", "/internal/notify", {"task_id": "", "text": f"{title}\n{body}"}, timeout=20)
    except Exception:
        pass
    return report


def due(store, now: float | None = None) -> bool:
    """True once per local day, after memory_consolidate_at."""
    s = store.settings()
    if not s.get("memory_consolidation", True):
        return False
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(s.get("timezone") or "UTC")
    except Exception:
        from datetime import timezone as _tz
        tz = _tz.utc
    now_dt = datetime.fromtimestamp(now or time.time(), tz)
    try:
        hh, mm = [int(x) for x in str(s.get("memory_consolidate_at") or "03:30").split(":")[:2]]
    except ValueError:
        hh, mm = 3, 30
    if (now_dt.hour, now_dt.minute) < (hh, mm):
        return False
    if needs_first_review(store):
        return False
    last = store.db.one("SELECT ts FROM memory_runs WHERE kind='daily' ORDER BY id DESC LIMIT 1")
    return not (last and datetime.fromtimestamp(last["ts"], tz).date() == now_dt.date())


def needs_first_review(store) -> bool:
    """A memory that already holds a lot is tidied the first time only after the user previewed and applied it."""
    applied = store.db.one("SELECT id FROM memory_runs WHERE kind IN ('manual','daily','applied') LIMIT 1")
    return not applied and len(_all_facts(store)) > 20


# ------------------------------------------------------------------ background job (the LLM pass can take minutes)
_job: dict = {"running": False}


def job_state() -> dict:
    return dict(_job)


def start(rt, *, dry_run: bool, kind: str = "manual", from_run: int | None = None) -> bool:
    import asyncio
    import traceback
    if _job.get("running"):
        return False
    _job.clear()
    _job.update(running=True, started=time.time(), dry_run=dry_run, kind=kind)

    async def go():
        try:
            r = await run(rt, dry_run=dry_run, kind=kind, from_run=from_run)
            _job.update(last_id=r.get("id"), error="")
        except Exception as e:
            traceback.print_exc()
            _job.update(error=str(e)[:300])
        finally:
            _job.update(running=False, finished=time.time())
            try:
                await rt.publish({"kind": "memory_update"})
            except Exception:
                pass
    asyncio.create_task(go())
    return True
