"""Goals (场景目标): long-running objectives that OMuse keeps working on until they are achieved.

A goal = objective + success criteria + how often to check (cron / interval / event trigger) + optional deadline.
Each check is a normal agent task (same tools, same Sentinel approvals). The agent must end every check with
goal_update(status, progress): progress notes build a history the next check sees; "achieved"/"failed" close
the goal, "blocked" asks the user for help. When the deadline passes, one final check decides the outcome.
"""
from __future__ import annotations

import time

from app.common.util import dumps, new_id, now_ts, truncate

STATUSES = ("active", "paused", "achieved", "failed", "expired", "cancelled")
UPDATE_STATUSES = ("active", "blocked", "achieved", "failed")
STATUS_ZH = {"active": "进行中", "paused": "已暂停", "achieved": "已达成", "failed": "未达成", "expired": "已过期",
             "cancelled": "已取消", "blocked": "需要你帮忙"}


def _fmt(ts: float | None, tz: str = "") -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def parse_deadline(v) -> float | None:
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            ts = time.mktime(time.strptime(s[:16] if "H" in fmt else s[:10], fmt))
            return ts + (86399 if fmt == "%Y-%m-%d" else 0)  # a bare date means end of that day
        except ValueError:
            continue
    raise ValueError("截止时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM (deadline format)")


def create_goal(store, *, title: str, objective: str, criteria: str, kind: str, spec, tz: str, deadline=None) -> dict:
    from app.runtime.scheduler import create_schedule
    title = str(title or "").strip()[:80]
    objective = str(objective or "").strip()[:3000]
    if not title or not objective:
        raise ValueError("目标需要标题和描述 (title and objective required)")
    dl = parse_deadline(deadline)
    if dl and dl < time.time():
        raise ValueError("截止时间已经过去了 (deadline is in the past)")
    gid = new_id("goal")
    conv = store.create_conv(f"🎯 {title}", kind="schedule", cid=f"conv_{gid}")
    sch = create_schedule(store, f"🎯 {title}", f"(goal {gid})", kind, spec, tz, goal_id=gid, conv_id=conv)
    store.db.insert("goals", {"id": gid, "title": title, "objective": objective, "criteria": str(criteria or "").strip()[:2000],
                              "status": "active", "deadline": dl, "schedule_id": sch["id"], "conv_id": conv,
                              "progress": dumps([]), "result": "", "created_at": now_ts(), "updated_at": now_ts(),
                              "finished_at": None})
    return store.goal(gid)


def goal_prompt(g: dict, final: bool = False, lang: str = "zh") -> str:
    prog = g.get("progress") or []
    hist = "\n".join(f"- {p.get('at', '')} [{p.get('status')}] {p.get('note', '')}" for p in prog[-12:]) \
        or ("(no progress yet — this is the first check)" if lang == "en" else "（还没有进展记录 no progress yet — this is the first check）")
    dl = f"{_fmt(g['deadline'])}" if g.get("deadline") else "none"
    if lang == "en":
        task = ("⏰ The deadline has passed. This is the final check: decide whether the goal was achieved, then call goal_update "
                "with status achieved or failed." if final else
                "This run: check the latest progress, take the next useful step toward the goal (sending/submitting still needs "
                "the user's approval as usual), then ALWAYS call goal_update: active / blocked (need the user's help) / "
                "achieved / failed.")
        return (f"🎯 Goal: {g['title']}\nObjective: {g['objective']}\n"
                f"Success criteria: {g.get('criteria') or '(use your judgement)'}\nDeadline: {dl}\n"
                f"Progress so far (oldest first):\n{hist}\n\n{task}")
    task = ("⏰ 截止时间已到。这是最后一次检查：判断目标是否达成，然后调用 goal_update，status 只能是 achieved 或 failed。\n"
            "Deadline reached — final check: decide and call goal_update with status achieved or failed."
            if final else
            "本次任务：检查目标的最新进展，做下一步能推进目标的事情（发送/提交等操作照常需要用户审批），"
            "最后必须调用 goal_update 记录进展：仍在进行 active / 需要用户帮忙 blocked / 已达成 achieved / 无法达成 failed。\n"
            "This run: check progress, take the next useful step toward the goal, then ALWAYS call goal_update.")
    return (f"🎯 长期目标 Goal: {g['title']}\n"
            f"目标描述 Objective: {g['objective']}\n"
            f"完成标准 Success criteria: {g.get('criteria') or '（由你根据目标判断 use judgement）'}\n"
            f"截止时间 Deadline: {dl}\n"
            f"以往进展 Progress so far (oldest first):\n{hist}\n\n{task}")


async def apply_update(rt, g: dict, status: str, note: str, task_id: str = "") -> str:
    store = rt.store
    status = status if status in UPDATE_STATUSES else "active"
    prog = (g.get("progress") or []) + [{"at": _fmt(time.time()), "status": status, "note": truncate(note, 800), "task_id": task_id}]
    data = {"progress": dumps(prog[-50:]), "updated_at": now_ts()}
    final = status in ("achieved", "failed")
    if final:
        data.update(status=status, result=truncate(note, 2000), finished_at=now_ts())
        store.db.execute("UPDATE schedules SET enabled=0 WHERE id=?", (g["schedule_id"],))
    store.db.update("goals", "id", g["id"], data)
    if final or status == "blocked":
        icon = {"achieved": "🎉", "failed": "⚠️", "blocked": "🙋"}[status]
        if store.settings().get("language") == "en":
            title = f"{icon} Goal {({'achieved': 'achieved', 'failed': 'failed', 'blocked': 'needs your help'})[status]}: {g['title']}"
        else:
            title = f"{icon} 目标{STATUS_ZH[status]}：{g['title']}"
        n = store.notify(title, truncate(note, 1500), task_id, level="info" if status == "achieved" else "warning")
        await rt.publish({"kind": "notification", "notification": n})
        try:
            await rt.sentinel("POST", "/internal/notify", {"task_id": task_id, "text": f"{title}\n{truncate(note, 1500)}"}, timeout=20)
        except Exception:
            pass
    await rt.publish({"kind": "goal_update", "goal_id": g["id"]})
    await rt.audit("executor", "goal.update", task_id, resource=g["id"], result=status, detail={"note": truncate(note, 300)})
    return f"已记录 recorded: {STATUS_ZH.get(status, status)}" + ("（目标已结束 goal closed）" if final else "")


async def check_deadlines(rt):
    """Goals past their deadline get one final check; if that run doesn't decide, they expire."""
    store = rt.store
    now = time.time()
    for g in store.goals():
        if g["status"] != "active" or not g.get("deadline") or g["deadline"] > now:
            continue
        sch = store.schedule(g["schedule_id"]) if g.get("schedule_id") else None
        st = (sch or {}).get("state") or {}
        if not st.get("_final_check"):
            if sch:
                last = store.task(sch["last_task"]) if sch.get("last_task") else None
                if last and last["status"] not in ("COMPLETED", "FAILED", "CANCELLED"):
                    continue
                st["_final_check"] = time.time()
                store.db.execute("UPDATE schedules SET state=?, enabled=0 WHERE id=?", (dumps(st), sch["id"]))
                en = store.settings().get("language") == "en"
                store.add_msg(g["conv_id"], "user", f"⏰ {'Deadline reached — final check' if en else '截止时间已到，最后检查 Final check'}: {g['title']}")
                t = await rt.submit(g["conv_id"], goal_prompt(g, final=True, lang="en" if en else "zh"), source="schedule", schedule_id=sch["id"])
                store.db.execute("UPDATE schedules SET last_task=? WHERE id=?", (t["id"], sch["id"]))
                continue
        elif now - st["_final_check"] < 3600:
            continue
        store.db.update("goals", "id", g["id"], {"status": "expired", "finished_at": now_ts(), "updated_at": now_ts()})
        if store.settings().get("language") == "en":
            n = store.notify(f"⌛ Goal expired: {g['title']}", "The deadline passed and the final check reached no conclusion.", level="warning")
        else:
            n = store.notify(f"⌛ 目标已过期：{g['title']}", "截止时间已过，最后一次检查没有给出结论。", level="warning")
        await rt.publish({"kind": "notification", "notification": n})


def set_status(store, gid: str, status: str) -> dict:
    g = store.goal(gid)
    if not g:
        raise KeyError(gid)
    if status not in ("active", "paused", "cancelled"):
        raise ValueError("只能设置为 active / paused / cancelled")
    if g["status"] in ("achieved", "failed", "expired", "cancelled") and status == "active":
        dl = g.get("deadline")
        if dl and dl < time.time():
            raise ValueError("截止时间已过，请先修改截止时间 (deadline passed)")
    store.db.update("goals", "id", gid, {"status": status, "updated_at": now_ts(),
                                         "finished_at": now_ts() if status == "cancelled" else None})
    store.db.execute("UPDATE schedules SET enabled=? WHERE id=?", (1 if status == "active" else 0, g["schedule_id"]))
    return store.goal(gid)


def goal_for_task(store, t: dict) -> dict | None:
    if not t.get("schedule_id"):
        return None
    sch = store.schedule(t["schedule_id"])
    return store.goal(sch["goal_id"]) if sch and sch.get("goal_id") else None


def brief(store, g: dict) -> dict:
    from app.runtime.scheduler import describe
    sch = store.schedule(g["schedule_id"]) if g.get("schedule_id") else None
    return {**g, "check": describe(sch) if sch else "", "next_run": (sch or {}).get("next_run"),
            "last_run": (sch or {}).get("last_run"), "last_task": (sch or {}).get("last_task"),
            "kind": (sch or {}).get("kind"), "spec": (sch or {}).get("spec"), "state_error": ((sch or {}).get("state") or {}).get("_error", "")}
