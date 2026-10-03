"""DialMCP as a phone line: calls to US / Canadian numbers from the user's own verified number.

DialMCP (https://dialmcp.com) is a hosted MCP server: `place_call` starts a call handled by its own voice agent
(which says it is an AI calling for the user and that the call is recorded), `get_call` long-polls the status and,
at the end, returns a structured resolution + transcript + recording, `end_call` hangs up. OMuse signs in once with
OAuth 2.1 (mcp_oauth); the tokens stay in the vault as `cred_phone_dialmcp`.

The agent never sees these MCP tools directly: it keeps using phone_call / phone_call_status / phone_hangup, so the
same policy applies to every line (approval for every call, number rules, daily limit). phone.py picks the line.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

from app.common.util import truncate
from app.sentinel import mcp_oauth
from app.sentinel.mcp_client import MCPError, connect

DEFAULT_URL = os.environ.get("DIALMCP_URL", "https://mcp.dialmcp.com/mcp")
HANDLE = "cred_phone_dialmcp"
CONNECTOR = "phone"
TERMINAL = {"completed", "no_answer", "busy", "voicemail", "failed", "canceled", "cancelled", "declined_recording", "opted_out"}
NANP = re.compile(r"\+1[2-9]\d{2}[2-9]\d{6}")
MAX_MINUTES = 10

_session = None
_lock = asyncio.Lock()


class DialError(Exception):
    def __init__(self, msg: str, code: str = ""):
        super().__init__(msg)
        self.code = code


def url(store) -> str:
    return str(store.connection("phone")["config"].get("dialmcp_url") or DEFAULT_URL)


def ready(store) -> bool:
    return mcp_oauth.connected(store, HANDLE)


def account(store) -> dict:
    return (store.get_secret(HANDLE) or {}).get("account") or {}


def check_number(n: str, store) -> str:
    """'' if DialMCP can call normalized number `n`, else the reason."""
    if not NANP.fullmatch(n):
        return "DialMCP 只能拨打美国和加拿大的号码（+1 开头的 10 位号码）(DialMCP calls US / Canadian numbers only)"
    if n == account(store).get("phone"):
        return "不能拨打你自己的外呼号码 (that is your own caller-ID number)"
    return ""


async def _drop():
    global _session
    c, _session = _session, None
    if c:
        try:
            await c.close()
        except Exception:
            pass


async def _client(store, force_refresh: bool = False):
    global _session
    async with _lock:
        if _session is not None and not force_refresh:
            return _session
        await _drop()
        try:
            headers = await mcp_oauth.auth_headers(store, HANDLE, force_refresh=force_refresh)
        except mcp_oauth.OAuthError as e:
            raise DialError(f"DialMCP 授权失效，请在「连接 → 电话」重新登录 (sign in to DialMCP again): {e}", "auth")
        try:
            _session = await connect(url(store), headers)
        except MCPError as e:
            if e.auth and not force_refresh:
                raise
            raise DialError(f"连不上 DialMCP (cannot reach DialMCP): {e}", "auth" if e.auth else "network")
        return _session


def _parse(res: dict) -> dict:
    if isinstance(res.get("structuredContent"), dict):
        return res["structuredContent"]
    text = "\n".join(str(c.get("text", "")) for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") == "text")
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else {"result": d}
    except ValueError:
        return {"text": text}


async def call_tool(store, name: str, args: dict, timeout: float = 75) -> dict:
    for attempt in range(2):
        try:
            c = await _client(store, force_refresh=attempt > 0)
            res = await c.call_tool(name, args, timeout=timeout)
            break
        except MCPError as e:
            await _drop()
            if attempt == 0 and (e.auth or e.code in (404, None)):
                continue        # expired token or a stale session: refresh / reconnect once
            raise DialError(f"DialMCP {name} 调用失败 (failed): {e}", "auth" if e.auth else "network")
    data = _parse(res)
    if res.get("isError"):
        msg = data.get("message") or data.get("error_description") or data.get("text") or json.dumps(data, ensure_ascii=False)
        raise DialError(f"DialMCP: {truncate(str(msg), 600)}", str(data.get("error") or ""))
    return data


async def whoami(store) -> dict:
    d = await call_tool(store, "list_calls", {"limit": 1}, timeout=30)
    u = d.get("user") or {}
    return {"name": str(u.get("name") or "")[:80], "phone": str(u.get("phone") or "")[:20]}


async def place(store, call: dict, owner: str) -> dict:
    objective = call["purpose"]
    if call.get("language"):
        objective += f"\nSpeak {call['language']}."
    ctx = []
    if owner:
        ctx.append(f"You are calling on behalf of {owner}.")
    if call.get("may_share"):
        ctx.append("Facts you may share if asked: " + call["may_share"])
    ctx.append("Never pay, never read out card numbers, passwords, one-time codes or ID numbers, and never agree to "
               "anything beyond the objective; say the account holder will follow up instead.")
    args = {"to": call["to"], "objective": truncate(objective, 2000), "context": truncate(" ".join(ctx), 2000),
            "max_duration_minutes": max(1, min(int(call["max_minutes"]), MAX_MINUTES))}
    if call.get("callee_name"):
        args["callee_name"] = truncate(call["callee_name"], 200)
    return await call_tool(store, "place_call", args, timeout=60)


async def get(store, remote_id: str, wait: int = 45) -> dict:
    return await call_tool(store, "get_call", {"call_id": remote_id, "wait_seconds": max(0, min(int(wait), 50))},
                           timeout=wait + 30)


async def end(store, remote_id: str) -> dict:
    return await call_tool(store, "end_call", {"call_id": remote_id}, timeout=40)


# ------------------------------------------------------------------ DialMCP state -> OMuse call record
_OUTCOME = {"achieved": "done", "partially_achieved": "partly_done", "not_achieved": "not_done", "no_conversation": "not_done"}


def _transcript(raw) -> list[dict]:
    out = []
    if isinstance(raw, str):
        for ln in raw.splitlines():
            m = re.match(r"\s*(?:\[[^\]]*\]\s*)?([^:]{1,30}):\s*(.+)", ln)
            if m:
                who = "omuse" if re.search(r"agent|assistant|\bai\b|caller|dialmcp|omuse", m.group(1), re.I) else "them"
                out.append({"who": who, "text": truncate(m.group(2).strip(), 2000), "t": 0})
            elif ln.strip():
                out.append({"who": "them", "text": truncate(ln.strip(), 2000), "t": 0})
    elif isinstance(raw, list):
        for x in raw:
            if not isinstance(x, dict):
                continue
            role = str(x.get("role") or x.get("speaker") or x.get("who") or "")
            text = str(x.get("text") or x.get("content") or x.get("message") or "").strip()
            if not text:
                continue
            t = x.get("t") or x.get("offset_seconds") or x.get("time") or 0
            try:
                t = round(float(t), 1)
            except (TypeError, ValueError):
                t = 0
            who = "omuse" if re.search(r"agent|assistant|\bai\b|bot|caller", role, re.I) else "them"
            out.append({"who": who, "text": truncate(text, 2000), "t": t})
    return out[-400:]


def apply(call: dict, d: dict, now: float) -> bool:
    """Update `call` from a get_call/place_call result. -> True when the call is over."""
    st = str(d.get("status") or "").lower()
    for k in ("listen_url", "recording_url"):
        if d.get(k):
            call[k] = str(d[k])[:500]
    if d.get("recording_status"):
        call["recording_status"] = str(d["recording_status"])[:30]
    tr = _transcript(d.get("transcript"))
    if tr:
        call["transcript"] = tr
    res = d.get("resolution") if isinstance(d.get("resolution"), dict) else None
    if res:
        call["resolution"] = {k: res.get(k) for k in ("outcome", "summary", "commitments", "follow_ups", "key_facts") if k in res}
        parts = [str(res.get("summary") or "").strip()]
        for k, label in (("commitments", "Commitments"), ("follow_ups", "Follow-ups"), ("key_facts", "Key facts")):
            v = res.get(k) or []
            if v:
                parts.append(f"{label}: " + "; ".join(str(x) if not isinstance(x, dict) else json.dumps(x, ensure_ascii=False)
                                                     for x in v[:12]))
        call["summary"] = truncate("\n".join(p for p in parts if p), 3000)
        call["outcome"] = _OUTCOME.get(str(res.get("outcome") or ""), str(res.get("outcome") or "")[:30])
    if st not in TERMINAL:
        if re.search(r"progress|connect|answer|hold|talk|live|speaking", st) and call["status"] == "dialing":
            call["status"] = "connected"
            call["answered_at"] = call.get("answered_at") or now
        return False
    if call.get("answered_at") is None and st in ("completed", "voicemail", "declined_recording", "opted_out", "canceled",
                                                  "cancelled"):
        call["answered_at"] = call.get("answered_at") or call.get("created_at")
    if st in ("no_answer", "busy"):
        call["status"] = "no_answer"
        call["hangup_cause"] = call.get("hangup_cause") or st.replace("_", " ")
    elif st == "failed":
        call["status"] = "failed"
        call["error"] = call.get("error") or truncate(str(d.get("error") or d.get("failure_reason") or "call failed"), 300)
    else:
        call["status"] = "ended"
        if st == "voicemail":
            call["outcome"] = call.get("outcome") or "voicemail"
        elif st in ("declined_recording", "opted_out"):
            call["outcome"] = "refused"
            call["hangup_cause"] = st.replace("_", " ")
        elif st in ("canceled", "cancelled"):
            call["hangup_cause"] = "canceled by the user" if d.get("ended_by") == "user" else "canceled"
    call["remote_status"] = st
    return True
