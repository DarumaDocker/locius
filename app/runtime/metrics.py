"""Trust & outcome metrics (roadmap batch 1): how often OMuse finishes what it is asked, how often it needs the user,
and whether "done" is backed by proof. Computed from the task records and their events; nothing extra is logged.

Definitions (finished = COMPLETED + FAILED + CANCELLED; golden test runs are left out):
  completion_rate      COMPLETED / finished
  autonomous_rate      COMPLETED with no approval, no takeover and no question back to the user / COMPLETED
  intervention_rate    finished tasks that needed an approval, a takeover or an answer from the user / finished
  approval_burden      approvals asked / tasks that changed something (an order, an email, a calendar event …)
  denied               approvals the user refused (an action OMuse wanted that the user stopped)
  verified / unverified  tasks that changed something, with / without proof (order number, message id …)
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict

from app.runtime.health import CAUSES, failure_cause

FINISHED = ("COMPLETED", "FAILED", "CANCELLED")


def task_facts(t: dict, events: list[dict]) -> dict:
    approvals = takeovers = approved = denied = 0
    needs_user = False
    for e in events:
        d = e.get("data") or {}
        if e.get("type") == "waiting":
            if d.get("type") == "approval":
                approvals += 1
            elif d.get("type") in ("takeover_requested", "takeover"):
                takeovers += 1
        elif e.get("type") == "approval_resolved":
            if d.get("decision") == "approved":
                approved += 1
            elif d.get("decision") in ("denied", "rejected"):
                denied += 1
        elif e.get("type") == "needs_user":
            needs_user = True
    oc = t.get("outcome") or {}
    return {"approvals": approvals, "approved": approved, "denied": denied, "takeovers": takeovers, "needs_user": needs_user,
            "outcome": oc.get("status") or "", "acted": bool(oc.get("actions")),
            "cause": failure_cause(t.get("error") or "") if t.get("status") == "FAILED" else ""}


def compute(store, days: int = 7, now: float | None = None, tz_offset_h: float = 8.0) -> dict:
    now = now or time.time()
    since = now - days * 86400
    rows = [t for t in store.tasks(None, 2000) if (t.get("created_at") or 0) >= since and t.get("source") != "golden"]
    per_day: dict[str, Counter] = defaultdict(Counter)
    causes, errors = Counter(), Counter()
    agg = Counter()
    sites: dict[str, Counter] = defaultdict(Counter)   # registered site -> {opens, blocked}
    block_kinds = Counter()
    for t in rows:
        evs = store.events(t["id"])
        for e in evs:
            d = e.get("data") or {}
            if e.get("type") == "tool_call" and d.get("name") in ("browser_navigate", "browser_read"):
                for u in ([d.get("args", {}).get("url")] + list(d.get("args", {}).get("urls") or [])):
                    s = _site_key(str(u or ""))
                    if s:
                        sites[s]["opens"] += 1
            elif e.get("type") == "site_blocked":
                s = str(d.get("site") or "")
                if s:
                    sites[s]["blocked"] += 1
                    block_kinds[d.get("detail") or d.get("kind") or "?"] += 1
        f = task_facts(t, evs)
        st = t.get("status")
        day = time.strftime("%Y-%m-%d", time.gmtime((t.get("created_at") or now) + tz_offset_h * 3600))
        per_day[day]["total"] += 1
        per_day[day][st] += 1
        agg["total"] += 1
        agg[st] += 1
        if st not in FINISHED:
            continue
        agg["finished"] += 1
        helped = f["approvals"] or f["takeovers"] or f["needs_user"]
        if helped:
            agg["intervened"] += 1
        if st == "COMPLETED" and not helped:
            agg["autonomous"] += 1
        agg["approvals"] += f["approvals"]
        agg["denied"] += f["denied"]
        agg["takeovers"] += f["takeovers"]
        if f["acted"]:
            agg["acted"] += 1
            agg["acted_approvals"] += f["approvals"]
        if f["outcome"] in ("verified", "unverified"):
            agg[f["outcome"]] += 1
        if st == "FAILED":
            causes[f["cause"]] += 1
            errors[str(t.get("error") or "")[:90]] += 1

    def rate(a, b):
        return round(a / b, 3) if b else None

    return {
        "days": days, "since": since, "until": now,
        "tasks": agg["total"], "finished": agg["finished"],
        "completed": agg["COMPLETED"], "failed": agg["FAILED"], "cancelled": agg["CANCELLED"],
        "completion_rate": rate(agg["COMPLETED"], agg["finished"]),
        "autonomous_rate": rate(agg["autonomous"], agg["COMPLETED"]),
        "intervention_rate": rate(agg["intervened"], agg["finished"]),
        "approvals": agg["approvals"], "denied": agg["denied"], "takeovers": agg["takeovers"],
        "acted": agg["acted"], "approval_burden": rate(agg["acted_approvals"], agg["acted"]),
        "verified": agg["verified"], "unverified": agg["unverified"],
        "failure_causes": [{"cause": k, "label": CAUSES.get(k, k), "count": v} for k, v in causes.most_common()],
        "top_errors": [{"error": k, "count": v} for k, v in errors.most_common(5)],
        "per_day": [{"day": d, "total": c["total"], "completed": c["COMPLETED"], "failed": c["FAILED"],
                     "cancelled": c["CANCELLED"]} for d, c in sorted(per_day.items())],
        "browser": _browser_reliability(sites, block_kinds),
    }


def _site_key(url: str) -> str:
    from app.runtime.context import site_name
    return site_name(url)


def _browser_reliability(sites: dict, block_kinds: Counter) -> dict:
    opens = sum(c["opens"] for c in sites.values())
    blocked = sum(c["blocked"] for c in sites.values())
    rows = sorted(({"site": s, "opens": c["opens"], "blocked": c["blocked"],
                    "block_rate": round(c["blocked"] / c["opens"], 3) if c["opens"] else None}
                   for s, c in sites.items() if c["blocked"]), key=lambda x: -x["blocked"])
    return {"opens": opens, "blocked": blocked,
            "block_rate": round(blocked / opens, 3) if opens else None,
            "sites": rows[:12],
            "kinds": [{"detail": k, "count": v} for k, v in block_kinds.most_common(8)]}
