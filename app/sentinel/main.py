"""Sentinel: the front door and the sole permission authority.

* Serves the web UI and reverse-proxies /api/* to the Agent Runtime.
* /internal/* is for the runtime only (RUNTIME_TOKEN): tool catalog, act, audit, notify.
* /sentinel/api/* is for the user (via the Olares entrance): approvals, connections,
  credentials, audit, browser live view / takeover.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.common.util import VERSION, token_ok, truncate
from app.sentinel import actions, guard, mailboxes, mcp_hub, watchers
from app.sentinel.actions import ActionError
from app.sentinel.catalog import TOOLS, llm_schemas
from app.sentinel.policy import ALLOW, ASK, DENY, decide
from app.sentinel.store import Store
from app.sentinel.telegram_bot import TelegramBot

SDATA = os.environ.get("SENTINEL_DATA", "/sdata")
RUNTIME_URL = os.environ.get("RUNTIME_URL", "http://127.0.0.1:8081")
RUNTIME_TOKEN = os.environ.get("RUNTIME_TOKEN", "")
WEB_DIR = os.environ.get("WEB_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "web"))
SHOTS = os.path.join(SDATA, "shots")
EDITABLE = {"gmail_send": ["to", "cc", "subject", "body"], "gmail_reply": ["to", "cc", "subject", "body"],
            "gmail_create_draft": ["to", "cc", "subject", "body"], "gmail_forward": ["to", "note"],
            "browser_type": ["text"], "gmail_unsubscribe": ["message_ids"], "slack_send_message": ["text"],
            "notion_create_page": ["title", "content"], "notion_append": ["content"]}
CONNECTORS = ("gmail", "browser", "telegram", "notion", "slack")
SCOPE_TTL = {"SESSION": 8 * 3600, "PERMANENT": None, "TASK": None}

store: Store = None  # type: ignore
proxy_client: httpx.AsyncClient = None  # type: ignore
bot: TelegramBot = None  # type: ignore
_resolving: set[str] = set()   # approvals being resolved right now (web + Telegram may race)


@asynccontextmanager
async def lifespan(app):
    global store, proxy_client, bot
    os.makedirs(SHOTS, exist_ok=True)
    store = Store(SDATA)
    proxy_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    store.audit("sentinel", "sentinel.start", detail={"version": VERSION})
    try:
        mcp_hub.sync_catalog(store)
    except Exception as e:  # never block startup on a bad MCP config
        store.audit("sentinel", "mcp.sync", result="error", detail={"error": repr(e)[:300]})
    bot = TelegramBot(store, RUNTIME_URL, resolve_core)
    if os.environ.get("TELEGRAM_BOT", "1") != "0":
        bot.start()
    yield
    await bot.stop()
    for sid in list(mcp_hub._sessions):
        await mcp_hub._drop(sid)
    await proxy_client.aclose()


app = FastAPI(lifespan=lifespan, title="Locius Sentinel")


# ================================================================== auth helpers
def runtime_auth(x_persona_runtime: str | None = Header(default=None)):
    if not token_ok(x_persona_runtime, RUNTIME_TOKEN):
        raise HTTPException(401, "runtime token required")


def ui_auth(request: Request, x_persona_ui: str | None = Header(default=None)):
    """User endpoints: must come through the web UI (custom header => no cross-site form posts)."""
    if request.method not in ("GET", "HEAD") and x_persona_ui != "1":
        raise HTTPException(403, "UI header required")
    origin = request.headers.get("origin")
    if origin and request.method not in ("GET", "HEAD"):
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        if origin.split("://", 1)[-1].split("/")[0] != host.split(",")[0].strip():
            raise HTTPException(403, "cross-origin request rejected")


async def notify_runtime(path: str, payload: dict):
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(RUNTIME_URL + path, json=payload, headers={"X-Persona-Runtime": RUNTIME_TOKEN})
    except Exception as e:
        store.audit("sentinel", "runtime.notify_failed", resource=path, result="error", detail={"error": str(e)[:200]})


async def notify_phone(text: str):
    """Best-effort Telegram push for approvals / takeover requests."""
    conn = store.connection("telegram")
    if conn["enabled"] and conn["config"].get("notify_approvals", True) and store.has_secret("cred_telegram_1"):
        try:
            await actions.telegram_send(store, text)
        except Exception:
            pass


async def _safe(coro):
    try:
        await coro
    except Exception as e:
        store.audit("sentinel", "telegram.send_failed", result="error", detail={"error": str(e)[:300]})


def gmail_ready() -> bool:
    return bool(mailboxes.ready_accounts(store))


def _from_account(tool: str, args: dict) -> str:
    """Which mailbox an outgoing email will be sent from (shown in the approval so nobody sends as the wrong you)."""
    try:
        rid = args.get("reply_to_message_id") or (args.get("message_id") if tool in ("gmail_reply", "gmail_forward") else None)
        if rid:
            aid, _ = mailboxes.split_id(str(rid))
            acc = mailboxes.find(store, aid)
        else:
            acc = mailboxes.find(store, args.get("from_account"))
        return acc["email"] if acc else str(args.get("from_account") or "")
    except Exception:
        return ""


def _safe_args(tool: str, args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        out[k] = truncate(v, 2000) if isinstance(v, str) else v
    return out


# ================================================================== internal API (runtime only)
@app.get("/internal/catalog", dependencies=[Depends(runtime_auth)])
async def catalog():
    enabled = set()
    status = {}
    for name in CONNECTORS:
        c = store.connection(name)
        ready = c["enabled"]
        if name in ("notion", "slack"):
            ready = ready and store.has_secret(f"cred_{name}_1")
        if name == "gmail":
            ready = ready and gmail_ready()
        if name == "telegram":
            ready = ready and store.has_secret("cred_telegram_1") and bool(c["config"].get("chat_id"))
        if ready:
            enabled.add(name)
        status[name] = {"ready": bool(ready), "permissions": c["permissions"],
                        "workspace": c["config"].get("workspace") or c["config"].get("team") or "" if name in ("notion", "slack") else "",
                        "account": c["config"].get("email", "") if name == "gmail" else "",
                        "accounts": [a["email"] for a in sorted(mailboxes.ready_accounts(store), key=lambda a: a["id"] != mailboxes.default_id(store))] if name == "gmail" else []}
    status["mcp"] = mcp_hub.status(store)
    enabled |= {f"mcp:{x['id']}" for x in status["mcp"]["servers"] if x["enabled"]}

    def allowed(name: str) -> bool:
        t = TOOLS[name]
        if str(t["connector"]).startswith("mcp:"):
            return True  # sync_catalog only publishes enabled, reviewed, non-"off" tools
        return bool(store.connection(t["connector"])["permissions"].get(t["capability"]))
    schemas = [s for s in llm_schemas(enabled) if allowed(s["function"]["name"])]
    return {"tools": schemas, "connections": status,
            "risk": {k: v["risk"] for k, v in TOOLS.items()}}


@app.post("/internal/audit", dependencies=[Depends(runtime_auth)])
async def internal_audit(req: Request):
    b = await req.json()
    actor = str(b.get("actor", "runtime"))
    if actor not in ("runtime", "planner", "executor", "llm", "memory", "scheduler", "subagent", "user"):
        actor = "runtime"
    rec = store.audit(actor, str(b.get("action", ""))[:100], task_id=str(b.get("task_id", "")),
                      resource=str(b.get("resource", ""))[:200], risk=str(b.get("risk", "")),
                      decision=str(b.get("decision", "")), result=str(b.get("result", ""))[:50],
                      detail=b.get("detail") or {})
    return {"seq": rec["seq"]}


@app.post("/internal/notify", dependencies=[Depends(runtime_auth)])
async def internal_notify(req: Request):
    b = await req.json()
    conn = store.connection("telegram")
    if not (conn["enabled"] and store.has_secret("cred_telegram_1")):
        return {"sent": False}
    try:
        await actions.telegram_send(store, str(b.get("text", "")))
        store.audit("sentinel", "notify.telegram", task_id=b.get("task_id", ""), decision=ALLOW, result="success")
        return {"sent": True}
    except ActionError as e:
        return {"sent": False, "error": str(e)}


@app.get("/internal/browser_state", dependencies=[Depends(runtime_auth)])
async def internal_browser_state():
    try:
        return await actions.broker("GET", "/state", timeout=10)
    except ActionError as e:
        return {"mode": "unknown", "error": str(e)}


async def _context_for(tool: str, args: dict, task_id: str) -> tuple[dict | None, dict | None]:
    """Ask the broker what a ref points to so the policy can judge the click/type."""
    if tool in ("browser_click", "browser_type", "browser_select", "browser_upload"):
        info = await actions.broker("POST", "/agent/describe", {"task_id": task_id, "ref": args.get("ref", "")}, timeout=20)
        return info, {"url": info.get("page_url", ""), "title": info.get("page_title", "")}
    if tool == "browser_press":
        info = await actions.broker("POST", "/agent/focused", {"task_id": task_id}, timeout=20)
        st = await actions.broker("GET", "/state", timeout=10)
        return info, {"url": st.get("url", ""), "title": st.get("title", "")}
    return None, None


async def _summary(tool: str, args: dict, elem: dict | None, page: dict | None) -> dict:
    t = TOOLS[tool]
    s: dict = {"tool": tool, "connector": t["connector"], "fields": [], "editable": EDITABLE.get(tool, [])}
    if tool in ("gmail_send", "gmail_create_draft"):
        s["title"] = "发送邮件 Send email" if tool == "gmail_send" else "创建草稿 Create draft"
        s["fields"] = [["发件账号 From", _from_account(tool, args)], ["收件人 To", args.get("to", "")],
                       ["抄送 Cc", args.get("cc", "")], ["主题 Subject", args.get("subject", "")]]
        s["body"] = args.get("body", "")
    elif tool == "gmail_reply":
        s["title"] = "回复并发送邮件 Reply & send"
        try:
            g, raw = actions.client_for_id(store, str(args.get("message_id", "")))
            d = await asyncio.to_thread(g.reply_defaults, raw, bool(args.get("reply_all")))
            args.setdefault("to", d["to"])
            args.setdefault("cc", d["cc"])
            args.setdefault("subject", d["subject"])
        except Exception as e:
            s["warning"] = f"无法读取原邮件: {e}"
        s["fields"] = [["发件账号 From", _from_account(tool, args)], ["收件人 To", args.get("to", "")],
                       ["抄送 Cc", args.get("cc", "")], ["主题 Subject", args.get("subject", "")]]
        s["body"] = args.get("body", "")
    elif tool == "gmail_forward":
        s["title"] = "转发邮件 Forward email"
        s["fields"] = [["发件账号 From", _from_account(tool, args)], ["转发给 To", args.get("to", "")],
                       ["原邮件 ID", args.get("message_id", "")]]
        s["body"] = args.get("note", "")
    elif tool == "gmail_unsubscribe":
        s["title"] = "退订邮件 Unsubscribe"
        ids = [str(x) for x in (args.get("message_ids") or [])][:30]
        try:
            targets = await asyncio.to_thread(actions.unsubscribe_targets, store, ids)
            s["items"] = [{"id": t["id"], "from": t.get("from", ""), "subject": t.get("subject", ""), "account": t.get("account", ""),
                           "date": t.get("date", ""), "method": t.get("method", ""), "error": t.get("error", "")} for t in targets]
        except Exception as e:
            s["warning"] = f"无法读取邮件信息: {e}"
            s["items"] = [{"id": i, "from": "", "subject": "", "method": ""} for i in ids]
        s["fields"] = [["邮件数 Emails", str(len(ids))], ["同时归档 Also archive", "是 Yes" if args.get("archive") else "否 No"]]
    elif tool.startswith("browser_"):
        verb = {"browser_click": "点击 Click", "browser_type": "输入 Type", "browser_select": "选择 Select",
                "browser_press": "按键 Press key", "browser_upload": "上传文件 Upload", "browser_navigate": "打开网页 Open page"}
        s["title"] = f"浏览器操作：{verb.get(tool, tool)}"
        s["fields"] = [["网站 Site", (page or {}).get("url") or args.get("url", "")], ["页面 Page", (page or {}).get("title", "")]]
        if elem:
            s["fields"].append(["元素 Element", f"{elem.get('tag', '')} 「{elem.get('name', '')}」"])
        if tool == "browser_type":
            s["body"] = args.get("text", "")
            if args.get("submit"):
                s["fields"].append(["提交 Submit", "输入后按回车 Enter"])
        if tool == "browser_select":
            s["fields"].append(["选项 Option", args.get("value", "")])
        if tool == "browser_press":
            s["fields"].append(["按键 Key", args.get("key", "")])
        if tool == "browser_upload":
            s["fields"].append(["文件 File", args.get("path", "")])
        s["screenshot"] = True
    elif tool == "slack_send_message":
        s["title"] = "发送 Slack 消息 Send Slack message"
        conf = store.connection("slack")["config"]
        s["fields"] = [["工作区 Workspace", conf.get("team", "")], ["频道 Channel", args.get("channel", "")],
                       ["身份 As", f"{'user' if conf.get('token_type') == 'user' else 'bot'} · {conf.get('user', '')}"]]
        if args.get("thread_ts"):
            s["fields"].append(["回复讨论串 Thread", args.get("thread_ts", "")])
        s["body"] = args.get("text", "")
    elif tool.startswith("notion_"):
        s["title"] = {"notion_create_page": "新建 Notion 页面 Create page", "notion_append": "追加到 Notion 页面 Append",
                      "notion_update_page": "修改 Notion 页面 Update page"}.get(tool, tool)
        if args.get("archived"):
            s["title"] = "归档（删除）Notion 页面 Archive page"
        s["fields"] = [["工作区 Workspace", store.connection("notion")["config"].get("workspace", "")],
                       ["页面 Page", args.get("parent_id") or args.get("page_id", "")]]
        if args.get("title"):
            s["fields"].append(["标题 Title", args["title"]])
        for k, v in (args.get("properties") or {}).items():
            s["fields"].append([f"属性 {k}", str(v)[:200]])
        s["body"] = args.get("content", "")
    elif str(t["connector"]).startswith("mcp:"):
        srv, rec = mcp_hub.tool_info(store, tool)
        sname = srv["name"] if srv else t["connector"][4:]
        tname = (rec or {}).get("title") or (rec or {}).get("name") or tool
        s["title"] = f"MCP · {sname}：{tname}"
        s["fields"] = [["服务 Server", sname], ["工具 Tool", (rec or {}).get("name", tool)],
                       ["类型 Kind", {"read": "只读 read", "write": "写入 write", "destructive": "可能删除/覆盖 destructive"}.get((rec or {}).get("kind"), "")]]
        for k, v in list(args.items())[:12]:
            s["fields"].append([f"参数 {k}", truncate(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str), 300)])
        big = {k: v for k, v in args.items() if isinstance(v, str) and len(v) > 300}
        if big:
            s["body"] = "\n\n".join(f"【{k}】\n{v}" for k, v in big.items())
    else:
        s["title"] = tool
        s["fields"] = [[k, str(v)] for k, v in args.items()]
    return s


@app.post("/internal/act", dependencies=[Depends(runtime_auth)])
async def act(req: Request):
    b = await req.json()
    tool, args = str(b.get("tool", "")), dict(b.get("args") or {})
    task_id, call_id = str(b.get("task_id", "")), str(b.get("call_id", ""))
    if tool not in TOOLS:
        store.audit("sentinel", tool or "unknown", task_id=task_id, decision=DENY, result="denied",
                    detail={"reason": "unknown tool"})
        return {"status": "denied", "reason": f"未知工具 unknown tool {tool}"}
    t = TOOLS[tool]
    elem = page = None
    try:
        if t["connector"] == "browser" and tool not in ("browser_request_takeover", "browser_downloads"):
            elem, page = await _context_for(tool, args, task_id)
    except ActionError as e:
        store.audit("sentinel", tool, task_id=task_id, resource="browser", decision="ERROR", result=e.status,
                    detail={"args": _safe_args(tool, args), "error": str(e)})
        return {"status": e.status, "error": str(e)}

    d = decide(store, tool, args, task_id, elem=elem, page=page, gmail_ready=gmail_ready())
    detail = {"args": _safe_args(tool, args), "reason": d.reason, "destination": d.destination}
    if elem:
        detail["element"] = {k: elem.get(k) for k in ("tag", "name", "input_type")}
    if d.decision == DENY:
        store.audit("sentinel", tool, task_id=task_id, resource=t["connector"], risk=d.risk, decision=DENY,
                    result="denied", detail=detail)
        return {"status": "denied", "reason": d.reason}
    if d.decision == ASK and b.get("no_ask"):
        store.audit("sentinel", tool, task_id=task_id, resource=t["connector"], risk=d.risk, decision=DENY,
                    result="denied", detail={**detail, "why": "needs approval but caller is a sub-agent"})
        return {"status": "denied", "reason": "此操作需要用户审批，子 Agent 不能执行 (needs approval; sub-agents cannot request it)"}
    if d.decision == ASK:
        summary = await _summary(tool, args, elem, page)
        summary["reason"] = d.reason
        summary["destination"] = d.destination
        ap = store.create_approval(task_id, call_id, tool, args, summary, d.risk, d.reason)
        if summary.get("screenshot"):
            try:
                img = await actions.broker("GET", f"/screenshot?task_id={task_id}", timeout=20)
                if isinstance(img, (bytes, bytearray)):
                    with open(os.path.join(SHOTS, f"{ap['id']}.jpg"), "wb") as f:
                        f.write(img)
            except Exception:
                pass
        store.audit("sentinel", tool, task_id=task_id, resource=t["connector"], risk=d.risk, decision=ASK,
                    result="pending", detail={**detail, "approval_id": ap["id"]})
        if bot and bot.config():
            asyncio.create_task(_safe(bot.send_approval(ap)))
        else:
            asyncio.create_task(notify_phone(f"Locius 需要你审批 Approval needed：{summary.get('title')}\n"
                                             f"{d.destination or ''}\n{d.reason}"))
        return {"status": "approval_required", "approval_id": ap["id"], "summary": summary, "reason": d.reason}
    # ALLOW
    return await _run(tool, args, task_id, d.risk, detail, decision=ALLOW)


async def _run(tool: str, args: dict, task_id: str, risk: str, detail: dict, decision: str) -> dict:
    t = TOOLS[tool]
    started = time.time()
    try:
        result = await actions.execute(store, tool, args, task_id)
        ms = int((time.time() - started) * 1000)
        store.audit("sentinel", tool, task_id=task_id, resource=t["connector"], risk=risk, decision=decision,
                    result="success", detail={**detail, "ms": ms,
                                              "result_preview": truncate(str(result), 600)})
        return {"status": "ok", "result": result}
    except ActionError as e:
        store.audit("sentinel", tool, task_id=task_id, resource=t["connector"], risk=risk, decision=decision,
                    result=e.status, detail={**detail, "error": str(e)})
        if e.status == "waiting_user":
            if bot and bot.config():
                asyncio.create_task(_safe(bot.send_takeover(str(args.get("reason", "")))))
            else:
                asyncio.create_task(notify_phone(f"Locius 请求你接管浏览器 Takeover requested：{args.get('reason', '')}"))
        return {"status": e.status, "error": str(e)}
    except Exception as e:  # unexpected
        store.audit("sentinel", tool, task_id=task_id, resource=t["connector"], risk=risk, decision=decision,
                    result="error", detail={**detail, "error": repr(e)[:500]})
        return {"status": "error", "error": f"内部错误 internal error: {str(e)[:300]}"}


# ================================================================== user API: approvals
@app.get("/sentinel/api/approvals", dependencies=[Depends(ui_auth)])
async def list_approvals(status: str | None = None, limit: int = 50):
    return {"approvals": store.approvals(status, limit)}


@app.get("/sentinel/api/approvals/{aid}/screenshot", dependencies=[Depends(ui_auth)])
async def approval_shot(aid: str):
    p = os.path.join(SHOTS, f"{os.path.basename(aid)}.jpg")
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")


@app.post("/sentinel/api/approvals/{aid}/resolve", dependencies=[Depends(ui_auth)])
async def resolve(aid: str, req: Request):
    b = await req.json()
    return await resolve_core(aid, b, via="web")


async def resolve_core(aid: str, b: dict, via: str = "web") -> dict:
    """Approve/deny a pending approval. Used by the web UI and the Telegram bot."""
    if aid in _resolving:
        raise HTTPException(409, "这个审批正在处理中 (being resolved)")
    _resolving.add(aid)
    try:
        return await _resolve(aid, b, via)
    finally:
        _resolving.discard(aid)


async def _resolve(aid: str, b: dict, via: str) -> dict:
    ap = store.approval(aid)
    if not ap:
        raise HTTPException(404, "approval not found")
    if ap["status"] != "pending":
        raise HTTPException(409, f"already {ap['status']}")
    decision = b.get("decision")
    scope = str(b.get("scope", "ONCE")).upper()
    if scope not in ("ONCE", "TASK", "SESSION", "TIME_BOUND", "PERMANENT"):
        scope = "ONCE"
    tool, args, task_id = ap["tool"], dict(ap["args"]), ap["task_id"]
    if decision != "approve":
        store.resolve_approval(aid, "denied", scope, {"status": "denied"})
        store.audit("user", "approval.deny", task_id=task_id, resource=tool, risk=ap["risk"], decision=DENY,
                    result="denied", detail={"approval_id": aid, "note": b.get("note", ""), "via": via})
        asyncio.create_task(notify_runtime("/internal/approval_resolved", {
            "approval_id": aid, "task_id": task_id, "call_id": ap["call_id"], "decision": "denied",
            "result": {"status": "denied", "reason": "用户拒绝了此操作 (user denied)" + (f"：{b['note']}" if b.get("note") else "")}}))
        return {"ok": True, "status": "denied"}

    edited = b.get("args") or {}
    for k in EDITABLE.get(tool, []):
        if k in edited and isinstance(edited[k], str):
            args[k] = edited[k]
        elif k == "message_ids" and isinstance(edited.get(k), list):
            keep = {str(x) for x in edited[k]}
            args[k] = [x for x in (args.get(k) or []) if str(x) in keep]  # user may only narrow the list
            if not args[k]:
                store.resolve_approval(aid, "denied", scope, {"status": "denied"})
                store.audit("user", "approval.deny", task_id=task_id, resource=tool, risk=ap["risk"], decision=DENY,
                            result="denied", detail={"approval_id": aid, "note": "no items selected"})
                asyncio.create_task(notify_runtime("/internal/approval_resolved", {
                    "approval_id": aid, "task_id": task_id, "call_id": ap["call_id"], "decision": "denied",
                    "result": {"status": "denied", "reason": "用户取消勾选了所有邮件，没有退订任何一封 (user deselected all)"}}))
                return {"ok": True, "status": "denied"}
    # re-check hard rules (connector may have been disabled meanwhile); approval overrides ASK only
    elem = page = None
    if tool not in TOOLS:
        store.resolve_approval(aid, "denied", scope, {"status": "denied", "reason": "tool no longer available"}, args)
        asyncio.create_task(notify_runtime("/internal/approval_resolved", {
            "approval_id": aid, "task_id": task_id, "call_id": ap["call_id"], "decision": "denied",
            "result": {"status": "denied", "reason": "这个工具已被移除或停用 (tool no longer available)"}}))
        return {"ok": False, "status": "denied", "reason": "tool no longer available"}
    if TOOLS[tool]["connector"] == "browser":
        try:
            elem, page = await _context_for(tool, args, task_id)
        except ActionError:
            pass
    d = decide(store, tool, args, task_id, elem=elem, page=page, gmail_ready=gmail_ready())
    if d.decision == DENY:
        store.resolve_approval(aid, "denied", scope, {"status": "denied", "reason": d.reason}, args)
        asyncio.create_task(notify_runtime("/internal/approval_resolved", {
            "approval_id": aid, "task_id": task_id, "call_id": ap["call_id"], "decision": "denied",
            "result": {"status": "denied", "reason": d.reason}}))
        return {"ok": False, "status": "denied", "reason": d.reason}
    if scope != "ONCE":
        ttl = SCOPE_TTL.get(scope)
        if scope == "TIME_BOUND":
            ttl = max(0.25, min(float(b.get("ttl_hours", 1)), 24 * 30)) * 3600
        store.add_grant(tool, scope, task_id if scope == "TASK" else None, {"destination": d.destination}, ttl,
                        note=ap["summary"].get("title", ""))
    store.audit("user", "approval.approve", task_id=task_id, resource=tool, risk=ap["risk"], decision=ALLOW,
                result="approved", detail={"approval_id": aid, "scope": scope, "edited": sorted(edited.keys()), "via": via})
    result = await _run(tool, args, task_id, ap["risk"], {"args": _safe_args(tool, args), "approval_id": aid}, decision="APPROVED")
    store.resolve_approval(aid, "approved", scope, result, args)
    asyncio.create_task(notify_runtime("/internal/approval_resolved", {
        "approval_id": aid, "task_id": task_id, "call_id": ap["call_id"], "decision": "approved", "result": result}))
    return {"ok": True, "status": "approved", "result": result}


# ================================================================== user API: grants
@app.get("/sentinel/api/grants", dependencies=[Depends(ui_auth)])
async def grants():
    return {"grants": store.active_grants()}


@app.delete("/sentinel/api/grants/{gid}", dependencies=[Depends(ui_auth)])
async def revoke(gid: str):
    store.revoke_grant(gid)
    store.audit("user", "grant.revoke", resource=gid, decision="REVOKE", result="success")
    return {"ok": True}


# ================================================================== user API: connections & credentials
def _conn_view(name: str) -> dict:
    c = store.connection(name)
    c["has_credential"] = store.has_secret(f"cred_{name}_1")
    c["credential_handle"] = f"cred_{name}_1" if c["has_credential"] else ""
    if name == "gmail":
        c["accounts"] = [{k: a[k] for k in ("id", "email", "display_name", "ready")} for a in mailboxes.accounts(store)]
        c["default"] = mailboxes.default_id(store)
        c["has_credential"] = bool(mailboxes.ready_accounts(store))
    return c


@app.get("/sentinel/api/connections", dependencies=[Depends(ui_auth)])
async def connections():
    return {"connections": [_conn_view(n) for n in CONNECTORS],
            "tools": {k: {"connector": v["connector"], "capability": v["capability"], "risk": v["risk"]} for k, v in TOOLS.items()}}


@app.put("/sentinel/api/connections/{name}", dependencies=[Depends(ui_auth)])
async def save_conn(name: str, req: Request):
    if name not in CONNECTORS:
        raise HTTPException(404)
    b = await req.json()
    cfg = b.get("config")
    if cfg is not None:
        cfg = {k: v for k, v in cfg.items() if k not in ("app_password", "bot_token", "password", "token", "workspace", "bot_id",
                                                          "team", "user", "user_id", "token_type")}
        if name == "browser":
            for k in ("blocked_domains", "allowed_domains"):
                if k in cfg and isinstance(cfg[k], str):
                    cfg[k] = [x.strip().lower() for x in cfg[k].replace("\n", ",").split(",") if x.strip()]
    c = store.save_connection(name, cfg, b.get("permissions"), b.get("enabled"))
    store.audit("user", "connection.update", resource=name, result="success",
                detail={"permissions": c["permissions"], "enabled": c["enabled"]})
    return _conn_view(name)


@app.post("/sentinel/api/connections/gmail/credential", dependencies=[Depends(ui_auth)])
async def gmail_cred(req: Request):
    b = await req.json()
    email_addr = str(b.get("email", "")).strip()
    pw = str(b.get("app_password", "")).replace(" ", "").strip()
    if "@" not in email_addr or len(pw) < 12:
        raise HTTPException(400, "请填写邮箱地址和 16 位应用专用密码 (App Password)")
    from app.sentinel.gmail import Gmail, GmailError
    g = Gmail(email_addr, pw, display_name=str(b.get("display_name", "")))
    try:
        info = await asyncio.to_thread(g.test)
    except GmailError as e:
        store.audit("user", "credential.gmail.set", resource="gmail", result="failed", detail={"error": str(e)[:300]})
        raise HTTPException(400, str(e))
    acc = mailboxes.save_account(store, email_addr, pw, str(b.get("display_name", "")))
    store.audit("user", "credential.gmail.set", resource="gmail", result="success", detail={"email": email_addr, "account": acc["id"]})
    return {"ok": True, "test": info, "account": acc["id"], "connection": _conn_view("gmail")}


@app.post("/sentinel/api/connections/gmail/test", dependencies=[Depends(ui_auth)])
async def gmail_test(req: Request):
    try:
        b = await req.json()
    except Exception:
        b = {}
    try:
        g = actions.gmail_client(store, b.get("account"))
        return {"ok": True, "account": g.email, "test": await asyncio.to_thread(g.test)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.delete("/sentinel/api/connections/gmail/accounts/{aid}", dependencies=[Depends(ui_auth)])
async def gmail_remove(aid: str):
    acc = mailboxes.find(store, aid)
    mailboxes.remove_account(store, aid)
    store.audit("user", "credential.gmail.delete", resource="gmail", result="success",
                detail={"account": aid, "email": acc["email"] if acc else ""})
    return _conn_view("gmail")


@app.post("/sentinel/api/connections/gmail/default", dependencies=[Depends(ui_auth)])
async def gmail_default(req: Request):
    b = await req.json()
    try:
        mailboxes.set_default(store, str(b.get("account", "")))
    except KeyError:
        raise HTTPException(404, "account not found")
    store.audit("user", "connection.gmail.default", resource="gmail", result="success", detail={"account": b.get("account")})
    return _conn_view("gmail")


@app.post("/sentinel/api/connections/telegram/credential", dependencies=[Depends(ui_auth)])
async def telegram_cred(req: Request):
    b = await req.json()
    tok, chat = str(b.get("bot_token", "")).strip(), str(b.get("chat_id", "")).strip()
    tok = tok or (store.get_secret("cred_telegram_1") or {}).get("bot_token", "")
    if not tok or not chat:
        raise HTTPException(400, "需要 bot token 和 chat id")
    if not re.fullmatch(r"-?\d{3,20}", chat):
        raise HTTPException(400, "chat id 应该是一串数字（可点「检测 Chat ID」自动获取）")
    old = (store.get_secret("cred_telegram_1") or {}).get("bot_token", "")
    store.put_secret("telegram", {"bot_token": tok})
    store.save_connection("telegram", {"chat_id": chat}, permissions={"notify": True, "control": True}, enabled=True)
    if old != tok:  # new bot: forget the old update offset / cached username
        store.kv_set("tg_offset", 0)
        if bot:
            bot.status["username"] = ""
    try:
        await actions.telegram_send(store, "✅ Locius 已连接 Telegram。直接给我发消息就能布置任务，发 /help 查看用法。\n"
                                           "Connected — message me to give Locius a task, /help for commands.")
    except ActionError as e:
        store.audit("user", "credential.telegram.set", resource="telegram", result="failed", detail={"error": str(e)})
        raise HTTPException(400, str(e))
    store.audit("user", "credential.telegram.set", resource="telegram", result="success")
    return {"ok": True, "connection": _conn_view("telegram")}


@app.get("/sentinel/api/telegram/status", dependencies=[Depends(ui_auth)])
async def telegram_status():
    st = dict(bot.status) if bot else {}
    st["configured"] = bool(bot and bot.config())
    st["tracking"] = len(bot.tracked) if bot else 0
    return st


@app.post("/sentinel/api/connections/telegram/detect", dependencies=[Depends(ui_auth)])
async def telegram_detect(req: Request):
    """Find the chat id: the user sends /start to the bot, then we read who wrote to it."""
    b = await req.json()
    tok = str(b.get("bot_token", "")).strip() or (store.get_secret("cred_telegram_1") or {}).get("bot_token", "")
    if not tok:
        raise HTTPException(400, "请先填写 bot token")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{os.environ.get('TELEGRAM_API', 'https://api.telegram.org')}/bot{tok}/getUpdates", params={"timeout": 0})
    data = r.json() if r.content else {}
    if not data.get("ok"):
        raise HTTPException(400, f"Telegram: {data.get('description') or r.status_code}")
    chats = {}
    for up in data.get("result") or []:
        ch = ((up.get("message") or {}).get("chat")) or {}
        if ch.get("type") == "private":
            chats[str(ch["id"])] = " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x) or ch.get("username", "")
    return {"chats": [{"chat_id": k, "name": v} for k, v in chats.items()]}


# ------------------------------------------------------------------ Notion / Slack
@app.post("/sentinel/api/connections/notion/credential", dependencies=[Depends(ui_auth)])
async def notion_cred(req: Request):
    from app.sentinel.notion import Notion, NotionError
    b = await req.json()
    token = str(b.get("token", "")).strip()
    if not re.fullmatch(r"(secret_|ntn_)[A-Za-z0-9]{20,80}", token):
        raise HTTPException(400, "请粘贴 Notion 集成令牌（以 ntn_ 或 secret_ 开头）(integration token)")
    n = Notion(token)
    try:
        me = await asyncio.to_thread(n.me)
        found = await asyncio.to_thread(n.search, "", "", 5)
    except NotionError as e:
        store.audit("user", "credential.notion.set", resource="notion", result="failed", detail={"error": str(e)[:300]})
        raise HTTPException(400, str(e))
    finally:
        n.close()
    ws = (me.get("bot") or {}).get("workspace_name") or ""
    store.put_secret("notion", {"token": token})
    store.save_connection("notion", {"workspace": ws, "bot_id": me.get("id", "")}, enabled=True)
    store.audit("user", "credential.notion.set", resource="notion", result="success", detail={"workspace": ws})
    return {"ok": True, "workspace": ws, "bot": me.get("name", ""), "visible": [f["title"] for f in found]}


@app.post("/sentinel/api/connections/slack/credential", dependencies=[Depends(ui_auth)])
async def slack_cred(req: Request):
    from app.sentinel.slack import Slack, SlackError
    b = await req.json()
    token = str(b.get("token", "")).strip()
    if not re.fullmatch(r"xox[bp]-[A-Za-z0-9-]{10,200}", token):
        raise HTTPException(400, "请粘贴 Slack 令牌：机器人令牌 xoxb-… 或用户令牌 xoxp-… (bot or user token)")
    sl = Slack(token)
    try:
        info = await asyncio.to_thread(sl.auth_test)
        chans = await asyncio.to_thread(sl.channels, 50)
    except SlackError as e:
        store.audit("user", "credential.slack.set", resource="slack", result="failed", detail={"error": str(e)[:300]})
        raise HTTPException(400, str(e))
    finally:
        sl.close()
    store.put_secret("slack", {"token": token})
    store.save_connection("slack", {k: info.get(k, "") for k in ("team", "user", "user_id", "bot_id", "token_type")}, enabled=True)
    store.audit("user", "credential.slack.set", resource="slack", result="success", detail={"team": info.get("team"), "type": info["token_type"]})
    return {"ok": True, **info, "channels": len(chans), "member_of": [c["name"] for c in chans if c["is_member"]][:20]}


# ------------------------------------------------------------------ triggers (runtime polls these)
@app.get("/internal/watch/sources", dependencies=[Depends(runtime_auth)])
async def watch_sources():
    return {"sources": watchers.SOURCES}


@app.post("/internal/watch", dependencies=[Depends(runtime_auth)])
async def watch(req: Request):
    b = await req.json()
    src = str(b.get("source", ""))
    info = watchers.SOURCES.get(src)
    if not info:
        return {"error": f"unknown source {src}", "events": [], "cursor": b.get("cursor")}
    conn = store.connection(info["connector"])
    if not conn["enabled"] or not conn["permissions"].get("read", False):
        return {"error": f"{info['connector']} 未启用或读取权限已关闭 (connector disabled / read permission off)",
                "events": [], "cursor": b.get("cursor")}
    try:
        res = await asyncio.to_thread(watchers.poll, store, src, dict(b.get("params") or {}), b.get("cursor"))
    except watchers.WatchError as e:
        return {"error": str(e), "events": [], "cursor": b.get("cursor")}
    if res["events"]:
        store.audit("sentinel", "trigger.events", resource=src, result="success",
                    detail={"count": len(res["events"]), "injection": res["injection"]})
    return res


@app.post("/internal/taint", dependencies=[Depends(runtime_auth)])
async def taint(req: Request):
    """Runtime tells Sentinel that a task starts with external (trigger) data in its context."""
    b = await req.json()
    lvl = str(b.get("taint", "CONFIDENTIAL"))
    ctx = store.update_task_ctx(str(b.get("task_id", "")), taint=lvl if lvl in guard.LEVELS else "CONFIDENTIAL",
                                injection=[str(x)[:40] for x in (b.get("injection") or [])][:10] or None)
    return {"ok": True, "ctx": ctx}


# ------------------------------------------------------------------ MCP servers
def _mcp_view(srv: dict) -> dict:
    v = {k: srv.get(k) for k in ("id", "name", "url", "transport", "enabled", "data_class", "auth", "header_name",
                                 "server_info", "protocol", "instructions", "last_sync", "last_error")}
    v["has_credential"] = store.has_secret(mcp_hub.handle(srv["id"]))
    v["tools"] = [{**{k: t.get(k) for k in ("name", "title", "description", "kind", "guessed", "mode", "status", "flags",
                                             "annotations", "previous_description")},
                   "llm_name": mcp_hub.tool_name(srv["id"], t["name"])} for t in srv.get("tools") or []]
    return v


def _mcp_err(e: Exception):
    raise HTTPException(400, str(e))


@app.get("/sentinel/api/mcp/servers", dependencies=[Depends(ui_auth)])
async def mcp_list():
    return {"servers": [_mcp_view(x) for x in mcp_hub.servers(store)]}


@app.post("/sentinel/api/mcp/servers", dependencies=[Depends(ui_auth)])
async def mcp_add(req: Request):
    b = await req.json()
    try:
        srv = await mcp_hub.add_server(store, name=str(b.get("name", "")), url=str(b.get("url", "")),
                                       auth_type=str(b.get("auth_type", "none")), token=str(b.get("token", "")),
                                       header_name=str(b.get("header_name", "")),
                                       data_class=str(b.get("data_class", "CONFIDENTIAL")))
    except mcp_hub.HubError as e:
        store.audit("user", "mcp.add", resource=str(b.get("url", ""))[:200], result="failed", detail={"error": str(e)[:300]})
        _mcp_err(e)
    store.audit("user", "mcp.add", resource=srv["id"], result="success",
                detail={"url": srv["url"], "transport": srv["transport"], "tools": [t["name"] for t in srv["tools"]],
                        "flagged": [t["name"] for t in srv["tools"] if t["flags"]]})
    return _mcp_view(srv)


@app.post("/sentinel/api/mcp/servers/{sid}/refresh", dependencies=[Depends(ui_auth)])
async def mcp_refresh(sid: str):
    try:
        r = await mcp_hub.refresh(store, sid)
    except mcp_hub.HubError as e:
        _mcp_err(e)
    store.audit("user", "mcp.refresh", resource=sid, result="success", detail={"diff": r["diff"]})
    return {**_mcp_view(r["server"]), "diff": r["diff"]}


@app.post("/sentinel/api/mcp/servers/{sid}/test", dependencies=[Depends(ui_auth)])
async def mcp_test(sid: str):
    try:
        return await mcp_hub.test_server(store, sid)
    except mcp_hub.HubError as e:
        _mcp_err(e)


@app.put("/sentinel/api/mcp/servers/{sid}", dependencies=[Depends(ui_auth)])
async def mcp_update(sid: str, req: Request):
    b = await req.json()
    try:
        if b.get("token") is not None or b.get("auth_type") is not None:
            srv = mcp_hub.server(store, sid) or {}
            at = str(b.get("auth_type", srv.get("auth", "none")))
            headers = mcp_hub.build_headers(at, str(b.get("token", "")), str(b.get("header_name", srv.get("header_name", ""))))
            if headers:
                store.put_secret("mcp", {"headers": headers}, handle=mcp_hub.handle(sid))
            elif at == "none":
                store.delete_secret(mcp_hub.handle(sid))
            mcp_hub._update(store, sid, lambda x: {**x, "auth": at if headers or at == "none" else x.get("auth"),
                                                   "header_name": str(b.get("header_name", x.get("header_name", "")))})
            await mcp_hub._drop(sid)
        srv = mcp_hub.set_server(store, sid, enabled=b.get("enabled"), data_class=b.get("data_class"), name=b.get("name"))
    except mcp_hub.HubError as e:
        _mcp_err(e)
    store.audit("user", "mcp.update", resource=sid, result="success",
                detail={k: b[k] for k in ("enabled", "data_class", "name", "auth_type") if k in b})
    return _mcp_view(srv)


@app.put("/sentinel/api/mcp/servers/{sid}/tools/{tname}", dependencies=[Depends(ui_auth)])
async def mcp_tool(sid: str, tname: str, req: Request):
    b = await req.json()
    try:
        srv = mcp_hub.set_tool(store, sid, tname, mode=b.get("mode"), accept=bool(b.get("accept")))
    except mcp_hub.HubError as e:
        _mcp_err(e)
    store.audit("user", "mcp.tool", resource=f"{sid}/{tname}", result="success",
                detail={k: b[k] for k in ("mode", "accept") if k in b})
    return _mcp_view(srv)


@app.delete("/sentinel/api/mcp/servers/{sid}", dependencies=[Depends(ui_auth)])
async def mcp_delete(sid: str):
    await mcp_hub.remove_server(store, sid)
    store.audit("user", "mcp.remove", resource=sid, result="success")
    return {"ok": True}


@app.delete("/sentinel/api/connections/{name}/credential", dependencies=[Depends(ui_auth)])
async def delete_cred(name: str):
    if name == "gmail":
        for a in mailboxes.accounts(store):
            mailboxes.remove_account(store, a["id"])
    store.delete_secret(f"cred_{name}_1")
    if name in ("notion", "slack"):
        store.save_connection(name, enabled=False)
    store.audit("user", f"credential.{name}.delete", resource=name, result="success")
    return {"ok": True}


# ================================================================== user API: audit
@app.get("/sentinel/api/audit", dependencies=[Depends(ui_auth)])
async def audit_list(task_id: str | None = None, limit: int = 200, before: int | None = None, actor: str | None = None):
    return {"events": store.audit_list(task_id, min(limit, 1000), before, actor)}


@app.get("/sentinel/api/audit/verify", dependencies=[Depends(ui_auth)])
async def audit_verify():
    return store.audit_verify()


# ================================================================== user API: browser view & takeover
@app.get("/sentinel/api/browser/state", dependencies=[Depends(ui_auth)])
async def b_state():
    try:
        return await actions.broker("GET", "/state", timeout=10)
    except ActionError as e:
        return {"mode": "offline", "error": str(e)}


@app.get("/sentinel/api/browser/screenshot", dependencies=[Depends(ui_auth)])
async def b_shot(task_id: str | None = None):
    try:
        img = await actions.broker("GET", "/screenshot" + (f"?task_id={task_id}" if task_id else ""), timeout=15)
    except ActionError:
        return Response(status_code=204)
    if not isinstance(img, (bytes, bytearray)):
        return Response(status_code=204)
    return Response(img, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/sentinel/api/browser/takeover", dependencies=[Depends(ui_auth)])
async def b_takeover(req: Request):
    b = await req.json()
    st = await actions.broker("POST", "/user/takeover", {"task_id": b.get("task_id")}, timeout=200)
    store.audit("user", "browser.takeover", task_id=st.get("takeover_task", ""), resource=st.get("url", ""), result="success")
    return st


@app.post("/sentinel/api/browser/release", dependencies=[Depends(ui_auth)])
async def b_release():
    r = await actions.broker("POST", "/user/release", {}, timeout=20)
    rel = r.get("released") or {}
    store.audit("user", "browser.release", task_id=rel.get("task_id") or "", result="success")
    await notify_runtime("/internal/takeover_ended", {"task_id": rel.get("task_id") or "",
                                                      "requested_task": (rel.get("requested") or {}).get("task_id", "")})
    return r


@app.post("/sentinel/api/browser/view", dependencies=[Depends(ui_auth)])
async def b_view(req: Request):
    return await actions.broker("POST", "/user/view", await req.json(), timeout=10)


@app.post("/sentinel/api/browser/input", dependencies=[Depends(ui_auth)])
async def b_input(req: Request):
    b = await req.json()
    r = await actions.broker("POST", "/user/input", b, timeout=60)
    # never log what the user typed during takeover (may be a password)
    if b.get("type") in ("navigate",):
        store.audit("user", "browser.user_navigate", resource=guard.domain_of(str(b.get("url", ""))), result="success")
    return r


# ================================================================== front door
@app.get("/sentinel/api/health")
async def health():
    return {"ok": True, "version": VERSION}


HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
       "proxy-authorization", "proxy-authenticate", "x-persona-runtime"}


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    if request.method not in ("GET", "HEAD") and request.headers.get("x-persona-ui") != "1":
        raise HTTPException(403, "UI header required")
    url = f"{RUNTIME_URL}/api/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    body = await request.body()
    rq = proxy_client.build_request(request.method, url, params=request.query_params, headers=headers, content=body)
    try:
        r = await proxy_client.send(rq, stream=True)
    except httpx.HTTPError:
        return JSONResponse({"detail": "Agent Runtime 暂不可用 (runtime unavailable)"}, status_code=503)
    resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in HOP and k.lower() != "content-encoding"}

    async def gen():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
    return StreamingResponse(gen(), status_code=r.status_code, headers=resp_headers)


def _asset_ver() -> str:
    """Version tag for cache-busting: changes whenever app.js/app.css change (e.g. after a hot patch)."""
    m = 0.0
    for f in ("app.js", "app.css", "i18n.js"):
        try:
            m = max(m, os.path.getmtime(os.path.join(WEB_DIR, f)))
        except OSError:
            pass
    return f"{VERSION}.{int(m)}"


@app.get("/")
async def index(request: Request):
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if host and not host.startswith(("127.", "localhost", "0.0.0.0")):
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
        base = f"{proto}://{host}"
        if store.kv_get("public_base") != base:
            store.kv_set("public_base", base)
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    v = _asset_ver()
    for f in ("app.js", "app.css", "i18n.js"):
        html = html.replace(f'static/{f}"', f'static/{f}?v={v}"')
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def _static_revalidate(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"  # always revalidate (cheap 304 via ETag)
    return resp


if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_exc(req, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(ActionError)
async def action_exc(req, exc: ActionError):
    code = 423 if exc.status == "paused" else 403 if ("拦截" in str(exc) or "禁止" in str(exc) or "blocked" in str(exc)) else 400
    return JSONResponse({"detail": str(exc)}, status_code=code)
