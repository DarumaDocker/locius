"""Agent Runtime HTTP API (reached only through Sentinel's reverse proxy)."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.common.util import VERSION, token_ok
from app.runtime.agent import TERMINAL, WORKSPACE, Runtime
from app.runtime import goals as G
from app.runtime.scheduler import EVENT_SOURCES, Scheduler, create_schedule, describe, next_run, validate
from app.runtime.scheduler import WAITING as SCHED_WAITING

DATA = os.environ.get("RUNTIME_DATA", "/data")
RUNTIME_TOKEN = os.environ.get("RUNTIME_TOKEN", "")

subscribers: set[asyncio.Queue] = set()


async def publish(ev: dict):
    ev.setdefault("ts", time.time())
    for q in list(subscribers):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass


rt: Runtime = None  # type: ignore
sched: Scheduler = None  # type: ignore


@asynccontextmanager
async def lifespan(app):
    global rt, sched
    from app.common import stallwatch
    stallwatch.start(DATA, "runtime")
    rt = Runtime(DATA, publish)
    sched = Scheduler(rt)
    sched.start()
    await rt.recover()
    yield


app = FastAPI(lifespan=lifespan, title="OMuse Runtime")


def internal_auth(x_persona_runtime: str | None = Header(default=None)):
    if not token_ok(x_persona_runtime, RUNTIME_TOKEN):
        raise HTTPException(401, "unauthorized")


@app.get("/api/health")
async def health():
    return {"ok": True, "version": VERSION}


# ------------------------------------------------------------------ chat & conversations
@app.put("/api/upload")
async def upload(req: Request, name: str = ""):
    """The chat's ＋ button: the raw file is the request body; it is stored under workspace/uploads/<YYYY-MM>/."""
    from app.runtime import attachments as AT
    if int(req.headers.get("content-length") or 0) > AT.UPLOAD_MAX:
        raise HTTPException(413, f"文件超过 {AT.UPLOAD_MAX // 1024 // 1024} MB (file too large)")
    data = await req.body()
    try:
        info = await asyncio.to_thread(AT.save_upload, WORKSPACE, name, data)
    except AT.AttachmentError as e:
        raise HTTPException(400, str(e))
    await rt.audit("user", "file.upload", resource=info["path"], detail={"size": info["size"], "mime": info["mime"]})
    return info


def _attachments(raw) -> list[dict]:
    from app.runtime import attachments as AT
    out = []
    for x in (raw or [])[:AT.UPLOAD_MAX_FILES]:
        rel = str((x or {}).get("path") if isinstance(x, dict) else x or "").strip().lstrip("/")
        fp = os.path.realpath(os.path.join(WORKSPACE, rel))
        if not rel.startswith("uploads/") or not fp.startswith(os.path.join(WORKSPACE, "uploads") + os.sep) or not os.path.isfile(fp):
            raise HTTPException(400, f"附件不存在 attachment not found: {rel}")
        out.append(AT.info(WORKSPACE, fp))
    return out


@app.post("/api/chat")
async def chat(req: Request):
    b = await req.json()
    text = str(b.get("message", "")).strip()
    atts = _attachments(b.get("attachments"))
    if not text and atts:
        en = rt.store.settings().get("language") == "en"
        text = "Please look at the attached file(s)." if en else "请看一下附件。"
    if not text:
        raise HTTPException(400, "message is empty")
    cid = b.get("conversation_id")
    if not cid or not rt.store.conv(cid):
        cid = rt.store.create_conv(text[:40])
    rt.store.add_msg(cid, "user", text, meta={"attachments": atts} if atts else None)
    t = await rt.submit(cid, text, attachments=atts)
    rt.store.db.execute("UPDATE messages SET task_id=? WHERE id=(SELECT MAX(id) FROM messages WHERE conv_id=? AND role='user')", (t["id"], cid))
    await publish({"kind": "conv_update", "conv_id": cid})
    return {"conversation_id": cid, "task_id": t["id"]}


@app.get("/api/conversations")
async def conversations(kind: str | None = None):
    rows = rt.store.convs(200)
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    return {"conversations": rows}


@app.get("/api/conversations/{cid}")
async def conversation(cid: str):
    c = rt.store.conv(cid)
    if not c:
        raise HTTPException(404)
    msgs = rt.store.msgs(cid, 300)
    tids = sorted({m["task_id"] for m in msgs if m["task_id"]})
    tasks = {tid: rt.task_brief(rt.store.task(tid)) for tid in tids if rt.store.task(tid)}
    return {"conversation": c, "messages": msgs, "tasks": tasks}


@app.delete("/api/conversations/{cid}")
async def del_conv(cid: str):
    rt.store.delete_conv(cid)
    return {"ok": True}


# ------------------------------------------------------------------ tasks
@app.get("/api/tasks")
async def tasks(status: str | None = None, limit: int = 100):
    return {"tasks": rt.store.tasks(status, min(limit, 500))}


@app.get("/api/tasks/{tid}")
async def task(tid: str):
    t = rt.store.task(tid)
    if not t:
        raise HTTPException(404)
    brief = rt.task_brief(t)
    brief["events"] = rt.store.events(tid)
    return brief


@app.post("/api/tasks/{tid}/{action}")
async def task_action(tid: str, action: str):
    t = rt.store.task(tid)
    if not t:
        raise HTTPException(404)
    if action == "cancel":
        await rt.cancel(tid)
    elif action == "pause":
        await rt.pause(tid)
    elif action == "resume":
        await rt.resume(tid)
    elif action == "retry":
        if t["status"] not in TERMINAL:
            raise HTTPException(409, "task still active")
        nt = await rt.submit(t["conv_id"], t["goal"], t["source"], t["schedule_id"], attachments=t.get("attachments") or None)
        return {"ok": True, "task_id": nt["id"]}
    else:
        raise HTTPException(404, "unknown action")
    return {"ok": True}


# ------------------------------------------------------------------ live events (SSE)
@app.get("/api/stream")
async def stream(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    subscribers.add(q)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                except asyncio.TimeoutError:
                    # a real event (not an SSE comment) so the page can tell a live stream from a silently dead one
                    yield 'data: {"kind": "ping"}\n\n'
        finally:
            subscribers.discard(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ schedules
@app.get("/api/schedules")
async def schedules():
    out = []
    for x in rt.store.schedules():
        if x.get("goal_id"):
            continue  # shown on the goal card
        st = x["state"] or {}
        x["describe"] = describe(x)
        x["state"] = {k: v for k, v in st.items() if not str(k).startswith("_")}
        x["trigger"] = {k: st.get(k) for k in ("_error", "_error_at", "_checked", "_seen", "_last_events") if st.get(k) is not None}
        last = rt.store.task(x["last_task"]) if x.get("last_task") else None
        x["last_status"] = last["status"] if last else ""
        x["blocked"] = {k[1:]: st[k] for k in ("_skipped", "_superseded") if st.get(k)}
        out.append(x)
    return {"schedules": out, "sources": EVENT_SOURCES}


# ------------------------------------------------------------------ goals
@app.get("/api/goals")
async def goals():
    return {"goals": [G.brief(rt.store, g) for g in rt.store.goals()]}


@app.post("/api/goals")
async def add_goal(req: Request):
    b = await req.json()
    try:
        g = G.create_goal(rt.store, title=str(b.get("title", "")), objective=str(b.get("objective", "")),
                          criteria=str(b.get("criteria", "")), kind=str(b.get("kind", "interval")), spec=b.get("spec", "60"),
                          tz=rt.store.settings()["timezone"], deadline=b.get("deadline") or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await rt.audit("user", "goal.create", resource=g["id"], detail={"title": g["title"]})
    if b.get("run_now"):
        await sched.run_now(g["schedule_id"])
    return G.brief(rt.store, rt.store.goal(g["id"]))


@app.put("/api/goals/{gid}")
async def upd_goal(gid: str, req: Request):
    g = rt.store.goal(gid)
    if not g:
        raise HTTPException(404)
    b = await req.json()
    data = {}
    for k in ("title", "objective", "criteria"):
        if k in b:
            data[k] = str(b[k])[:3000]
    try:
        if "deadline" in b:
            data["deadline"] = G.parse_deadline(b["deadline"])
        if data:
            data["updated_at"] = time.time()
            rt.store.db.update("goals", "id", gid, data)
        if "kind" in b or "spec" in b:
            sch = rt.store.schedule(g["schedule_id"])
            kind, spec = str(b.get("kind", sch["kind"])), b.get("spec", sch["spec"])
            spec = spec if isinstance(spec, str) else json.dumps(spec, ensure_ascii=False)
            validate(kind, spec)
            rt.store.db.update("schedules", "id", sch["id"], {"kind": kind, "spec": spec, "next_run": next_run(kind, spec, sch["tz"])})
        if "status" in b:
            G.set_status(rt.store, gid, str(b["status"]))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await rt.audit("user", "goal.update", resource=gid, detail={k: b[k] for k in b if k != "objective"})
    return G.brief(rt.store, rt.store.goal(gid))


@app.post("/api/goals/{gid}/run")
async def run_goal(gid: str):
    g = rt.store.goal(gid)
    if not g:
        raise HTTPException(404)
    if g["status"] != "active":
        raise HTTPException(400, "目标不在进行中 (goal is not active)")
    t = await sched.run_now(g["schedule_id"])
    return {"task_id": t["id"]}


@app.delete("/api/goals/{gid}")
async def del_goal(gid: str):
    g = rt.store.goal(gid)
    if g:
        rt.store.db.execute("DELETE FROM schedules WHERE id=?", (g["schedule_id"],))
        rt.store.db.execute("DELETE FROM goals WHERE id=?", (gid,))
    await rt.audit("user", "goal.delete", resource=gid)
    return {"ok": True}


@app.post("/api/schedules")
async def add_schedule(req: Request):
    b = await req.json()
    try:
        spec = b["spec"] if isinstance(b["spec"], str) else json.dumps(b["spec"], ensure_ascii=False)
        s = create_schedule(rt.store, str(b["name"]), str(b["goal"]), str(b.get("kind", "cron")), spec,
                            str(b.get("tz") or rt.store.settings()["timezone"]))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    await rt.audit("user", "schedule.create", resource=s["id"], detail={"name": s["name"], "spec": s["spec"]})
    return s


@app.put("/api/schedules/{sid}")
async def upd_schedule(sid: str, req: Request):
    s = rt.store.schedule(sid)
    if not s:
        raise HTTPException(404)
    b = await req.json()
    data = {}
    for k in ("name", "goal", "kind", "spec", "tz"):
        if k in b:
            data[k] = b[k] if isinstance(b[k], str) else json.dumps(b[k], ensure_ascii=False)
    if "enabled" in b:
        data["enabled"] = 1 if b["enabled"] else 0
    merged = {**s, **data}
    try:
        validate(merged["kind"], merged["spec"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    data["next_run"] = next_run(merged["kind"], merged["spec"], merged["tz"])
    rt.store.db.update("schedules", "id", sid, data)
    return rt.store.schedule(sid)


@app.delete("/api/schedules/{sid}")
async def del_schedule(sid: str):
    rt.store.db.execute("DELETE FROM schedules WHERE id=?", (sid,))
    await rt.audit("user", "schedule.delete", resource=sid)
    return {"ok": True}


@app.post("/api/schedules/{sid}/poll")
async def poll_schedule(sid: str):
    """Check an event trigger right now (instead of waiting for the next poll)."""
    s = rt.store.schedule(sid)
    if not s or s["kind"] != "event":
        raise HTTPException(400, "不是事件触发器 (not an event trigger)")
    last = rt.store.task(s["last_task"]) if s.get("last_task") else None
    if last and last["status"] not in TERMINAL:
        raise HTTPException(409, "上一次运行还没结束 (previous run still active)")
    n = await sched.poll_event(s)
    s2 = rt.store.schedule(sid)
    st = s2["state"] or {}
    return {"ok": not st.get("_error"), "error": st.get("_error", ""), "fired": bool(n) or s2["last_task"] != s.get("last_task"),
            "task_id": s2["last_task"] if s2["last_task"] != s.get("last_task") else ""}


@app.post("/api/schedules/{sid}/run")
async def run_schedule(sid: str):
    s = rt.store.schedule(sid)
    last = rt.store.task(s["last_task"]) if s and s.get("last_task") else None
    if last and last["status"] in SCHED_WAITING:
        await sched.supersede(s, last, manual=True)   # don't leave the old run (and its approval) dangling
    t = await sched.run_now(sid)
    return {"task_id": t["id"]}


# ------------------------------------------------------------------ memory
@app.get("/api/memory")
async def memory():
    from app.runtime import memory_tidy as MT
    from app.runtime.store import PROFILE_FIELDS
    st = rt.store
    runs = [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "lines": (r["report"] or {}).get("lines") or [],
             "errors": (r["report"] or {}).get("errors") or [], "applied": (r["report"] or {}).get("applied")}
            for r in st.memory_runs(10)]
    return {"facts": st.facts(), "recent": st.facts(1000, tier="recent"), "episodes": st.episodes(50),
            "profile": st.profile(), "profile_fields": [{"key": k, "zh": zh, "en": en} for k, zh, en in PROFILE_FIELDS],
            "pending": st.profile_pending(), "runs": runs, "job": MT.job_state(),
            "needs_first_review": MT.needs_first_review(st),
            "settings": {k: st.settings().get(k) for k in ("memory_consolidation", "memory_consolidate_at", "memory_extraction")}}


@app.post("/api/memory")
async def add_memory(req: Request):
    from app.runtime.store import looks_sensitive
    b = await req.json()
    fact = str(b.get("fact", ""))
    if looks_sensitive(fact):
        raise HTTPException(400, "证件号、卡号和密码请放进保险箱 (ID / card numbers and passwords belong in the vault)")
    r = rt.store.add_fact(fact, str(b.get("category") or "preference"), str(b.get("entity") or ""),
                          source="user-ui", confidence=1.0, tier="recent" if b.get("tier") == "recent" else "long")
    if not r:
        raise HTTPException(400, "empty fact")
    await rt.audit("user", "memory.add", detail={"fact": r["fact"]})
    return r


@app.put("/api/memory/{fid}")
async def edit_memory(fid: str, req: Request):
    from app.runtime.store import looks_sensitive
    b = await req.json()
    kw = {}
    if isinstance(b.get("fact"), str) and b["fact"].strip():
        if looks_sensitive(b["fact"]):
            raise HTTPException(400, "证件号、卡号和密码请放进保险箱 (ID / card numbers and passwords belong in the vault)")
        kw["fact"] = b["fact"].strip()[:500]
    if b.get("tier") in ("long", "recent"):
        kw["tier"] = b["tier"]
    if isinstance(b.get("category"), str):
        kw["category"] = b["category"][:30]
    r = rt.store.update_fact(fid, **kw)
    if not r:
        raise HTTPException(404, "not found")
    await rt.audit("user", "memory.edit", resource=fid, detail={k: v for k, v in kw.items()})
    return r


@app.delete("/api/memory/{fid}")
async def del_memory(fid: str):
    rt.store.delete_fact(fid)
    await rt.audit("user", "memory.forget", resource=fid)
    return {"ok": True}


@app.put("/api/profile")
async def set_profile(req: Request):
    """The user's own edit on the Memory page (this is the confirmation)."""
    from app.runtime.store import looks_sensitive, profile_key
    b = await req.json()
    key, val = profile_key(str(b.get("key", ""))), str(b.get("value") or "")
    if not key:
        raise HTTPException(400, "unknown field")
    if looks_sensitive(val):
        raise HTTPException(400, "证件号、卡号和密码请放进保险箱 (ID / card numbers and passwords belong in the vault)")
    rt.store.set_profile(key, val, source="user-ui")
    await rt.audit("user", "profile.set", resource=key)
    return {"profile": rt.store.profile()}


@app.post("/api/profile/pending/{pid}")
async def resolve_profile(pid: str, req: Request):
    b = await req.json()
    val = b.get("value")
    r = rt.store.resolve_profile(pid, bool(b.get("accept")), str(val) if isinstance(val, str) and val.strip() else None)
    if not r:
        raise HTTPException(409, "already resolved")
    await rt.audit("user", "profile.accept" if r["status"] == "accepted" else "profile.reject", resource=r["key"])
    await publish({"kind": "memory_update"})
    return r


@app.post("/api/memory/consolidate")
async def consolidate(req: Request):
    """{dry_run: true} → preview; {from_run: <id>} → apply that preview exactly; {} → plan and apply now."""
    from app.runtime import memory_tidy as MT
    b = await req.json()
    from_run = int(b["from_run"]) if str(b.get("from_run") or "").isdigit() else None
    ok = MT.start(rt, dry_run=bool(b.get("dry_run")) and not from_run, kind="manual", from_run=from_run)
    if not ok:
        raise HTTPException(409, "正在整理中 (a tidy run is already in progress)")
    return {"started": True}


@app.get("/api/memory/runs/{rid}")
async def memory_run(rid: int):
    r = next((x for x in rt.store.memory_runs(50) if x["id"] == rid), None)
    if not r:
        raise HTTPException(404, "not found")
    return r


# ------------------------------------------------------------------ notifications
@app.get("/api/notifications")
async def notifications():
    return {"notifications": rt.store.notifications(50)}


@app.post("/api/notifications/read")
async def notifications_read():
    rt.store.db.execute("UPDATE notifications SET read=1")
    return {"ok": True}


# ------------------------------------------------------------------ settings
@app.get("/api/settings")
async def get_settings():
    return {"settings": rt.store.settings(), "skills": [{"name": s["name"], "description": s["description"]} for s in rt.skills()]}


@app.put("/api/settings")
async def put_settings(req: Request):
    b = await req.json()
    if "memory_consolidate_at" in b and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(b["memory_consolidate_at"]).strip()):
        raise HTTPException(400, "整理时间格式应为 HH:MM (time must be HH:MM)")
    s = rt.store.set_settings(b)
    await rt.audit("user", "settings.update", detail={k: v for k, v in b.items() if k != "extra_body"})
    return {"settings": s}


@app.post("/api/settings/test-model")
async def test_model():
    t0 = time.time()
    try:
        r = await rt.llm.chat([{"role": "user", "content": "只回复 OK 两个字母。Reply with just OK."}], purpose="test",
                              max_tokens=400, no_think=True)
        return {"ok": True, "reply": r["content"][:200], "latency_s": round(time.time() - t0, 2)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


# ------------------------------------------------------------------ workspace files (read-only for the UI)
@app.get("/api/files")
async def files(path: str = ""):
    base = os.path.realpath(os.path.join(WORKSPACE, path.lstrip("/")))
    if not base.startswith(WORKSPACE):
        raise HTTPException(400)
    if not os.path.isdir(base):
        raise HTTPException(404)
    items = []
    for n in sorted(os.listdir(base)):
        if n.startswith("."):
            continue
        fp = os.path.join(base, n)
        items.append({"name": n, "path": os.path.relpath(fp, WORKSPACE), "dir": os.path.isdir(fp),
                      "size": os.path.getsize(fp) if os.path.isfile(fp) else None, "mtime": os.path.getmtime(fp)})
    return {"path": os.path.relpath(base, WORKSPACE), "items": items}


@app.get("/api/files/raw")
async def file_raw(path: str, download: int = 0):
    fp = os.path.realpath(os.path.join(WORKSPACE, path.lstrip("/")))
    if not fp.startswith(WORKSPACE + os.sep) or not os.path.isfile(fp) or "/.quarantine/" in fp:
        raise HTTPException(404)
    # download=1: save with its real name (the chat's download button); otherwise open in the browser (PDF, images).
    # Active content (HTML/SVG/XML/JS, e.g. a page the agent downloaded) is never rendered on the app's own origin —
    # it could call the app's APIs — so it is always a download, and sandboxed just in case.
    mime = mimetypes.guess_type(fp)[0] or "application/octet-stream"
    risky = any(x in mime for x in ("html", "svg", "xml", "javascript")) or fp.lower().endswith((".htm", ".html", ".svg", ".xhtml", ".js", ".mjs"))
    inline = not download and not risky
    headers = {"X-Content-Type-Options": "nosniff"}
    if risky:
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return FileResponse(fp, filename=os.path.basename(fp), content_disposition_type="inline" if inline else "attachment",
                        media_type=mime if not risky else "application/octet-stream", headers=headers)


# ------------------------------------------------------------------ internal callbacks from Sentinel
@app.post("/internal/approval_resolved", dependencies=[Depends(internal_auth)])
async def approval_resolved(req: Request):
    await rt.on_approval_resolved(await req.json())
    return {"ok": True}


@app.post("/internal/takeover_ended", dependencies=[Depends(internal_auth)])
async def takeover_ended(req: Request):
    await rt.on_takeover_ended(await req.json())
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_exc(req, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
