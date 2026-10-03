"""MCP connector framework: remote MCP servers as OMuse connectors.

Every MCP server the user adds becomes a connector "mcp:<sid>". Its tools are published into the Sentinel
catalog as `mcp_<sid>__<tool>` so the planner/executor can call them; every call still goes through
Sentinel's policy (auto / ask / off per tool, taint & injection escalation), the approval dialog, and the
audit log. Credentials (bearer token / custom header) live in the vault as `cred_mcp_<sid>`; the runtime
and the LLM never see them.

Defences against malicious or compromised servers:
* tool definitions are pinned (sha256). If a server silently changes a tool's description or schema
  ("rug pull"), the tool is hidden until the user reviews and accepts the change;
* new tools that appear later are hidden until the user enables them;
* tool descriptions are scanned for prompt-injection and such tools start disabled (tool poisoning);
* all tool results are wrapped as untrusted content and scanned; the task gets the server's data class
  as taint, so later outbound actions are checked for data egress;
* server->client requests (sampling/elicitation) are refused by the client.

Config lives in the "mcp" connection: {"servers": [ {id, name, url, transport, enabled, data_class, tools: [...]} ]}
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from app.common.util import truncate
from app.sentinel import guard, mcp_oauth
from app.sentinel.catalog import TOOLS
from app.sentinel.mcp_client import MCPError, check_server_url, connect

CONN = "mcp"
MODES = ("auto", "ask", "off")
DATA_CLASSES = ("PUBLIC", "PERSONAL", "CONFIDENTIAL")
MAX_SERVERS = 20
MAX_TOOLS = 128
_READ_PREFIX = re.compile(r"^(get|list|search|read|fetch|query|find|describe|lookup|retrieve|view|show|count|check|"
                          r"browse|resolve|download|inspect|explain|summari[sz]e)(_|-|$|[A-Z])", re.I)

_sessions: dict[str, object] = {}
_locks: dict[str, asyncio.Lock] = {}


class HubError(Exception):
    pass


# ------------------------------------------------------------------ config helpers
def servers(store) -> list[dict]:
    return list(store.connection(CONN)["config"].get("servers") or [])


def server(store, sid: str) -> dict | None:
    return next((s for s in servers(store) if s["id"] == sid), None)


def handle(sid: str) -> str:
    return f"cred_mcp_{sid}"


def _save(store, srvs: list[dict]):
    store.save_connection(CONN, {"servers": srvs}, enabled=True)
    sync_catalog(store)


def _update(store, sid: str, fn):
    srvs = servers(store)
    for i, s in enumerate(srvs):
        if s["id"] == sid:
            srvs[i] = fn(dict(s)) or srvs[i]
            _save(store, srvs)
            return srvs[i]
    raise HubError("找不到这个 MCP 服务器 (server not found)")


def slugify(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:16].strip("_")
    if not base or not base[0].isalpha():
        base = "srv" + (base[:12] if base else "")
    sid, n = base, 2
    while sid in taken:
        sid = f"{base[:14]}{n}"
        n += 1
    return sid


def tool_name(sid: str, tname: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", tname)
    full = f"mcp_{sid}__{clean}"
    if len(full) > 64:
        h = hashlib.sha1(tname.encode()).hexdigest()[:6]
        full = f"mcp_{sid}__{clean[:64 - len(sid) - 14]}_{h}"
    return full


def build_headers(auth_type: str, token: str, header_name: str = "") -> dict:
    token = (token or "").strip()
    if auth_type in ("", "none") or not token:
        return {}
    if any(c in token for c in "\r\n"):
        raise HubError("令牌里不能有换行 (token must be a single line)")
    if auth_type == "bearer":
        return {"Authorization": token if token.lower().startswith("bearer ") else f"Bearer {token}"}
    if auth_type == "header":
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header_name or ""):
            raise HubError("请求头名称只能包含字母、数字和横线，例如 X-API-Key (invalid header name)")
        if header_name.lower() in ("host", "content-type", "content-length", "accept", "mcp-session-id", "cookie"):
            raise HubError("不能使用这个请求头名称 (reserved header)")
        return {header_name: token}
    raise HubError("未知的认证方式 unknown auth type")


# ------------------------------------------------------------------ tool definitions
def _clean_text(s: str, limit: int) -> str:
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]", "", str(s or ""))
    return truncate(s.strip(), limit)


def _clean_schema(schema) -> dict:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    s = {k: v for k, v in schema.items() if k not in ("$schema", "$id")}
    s["type"] = "object"
    if not isinstance(s.get("properties"), dict):
        s["properties"] = {}
    if len(json.dumps(s, default=str)) > 12000:  # huge schemas: keep top-level fields only
        props = {k: {kk: vv for kk, vv in (v if isinstance(v, dict) else {}).items() if kk in ("type", "description", "enum")}
                 for k, v in list(s["properties"].items())[:40]}
        for v in props.values():
            if "description" in v:
                v["description"] = truncate(str(v["description"]), 200)
        s = {"type": "object", "properties": props, "required": [r for r in s.get("required") or [] if r in props]}
    return s


def def_hash(t: dict) -> str:
    core = {"name": t.get("name"), "description": t.get("description", ""), "inputSchema": t.get("inputSchema") or {},
            "annotations": t.get("annotations") or {}}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()[:24]


def classify(t: dict) -> dict:
    """-> {kind: read|write|destructive, guessed: bool}. Annotations are hints from the server: we use them to
    choose sensible defaults, but the user decides and every call is still policed."""
    ann = t.get("annotations") or {}
    if ann.get("readOnlyHint") is True:
        return {"kind": "read", "guessed": False}
    if "readOnlyHint" not in ann and _READ_PREFIX.match(t.get("name", "")):
        return {"kind": "read", "guessed": True}
    if ann.get("destructiveHint") is False:
        return {"kind": "write", "guessed": "destructiveHint" not in ann}
    return {"kind": "destructive" if "destructiveHint" in ann or ann.get("readOnlyHint") is False else "write",
            "guessed": "destructiveHint" not in ann}


def _tool_record(raw: dict) -> dict:
    desc = _clean_text(raw.get("description") or raw.get("title") or "", 1200)
    cls = classify(raw)
    flags = guard.scan_injection(f"{raw.get('name', '')}\n{desc}\n{json.dumps(raw.get('inputSchema') or {}, default=str)[:6000]}")
    mode = "auto" if cls["kind"] == "read" else "ask"
    if flags:
        mode = "off"
    return {"name": str(raw["name"])[:128], "title": _clean_text(raw.get("title") or (raw.get("annotations") or {}).get("title") or "", 120),
            "description": desc, "input_schema": _clean_schema(raw.get("inputSchema")),
            "annotations": {k: v for k, v in (raw.get("annotations") or {}).items() if k.endswith("Hint")},
            "kind": cls["kind"], "guessed": cls["guessed"], "hash": def_hash(raw), "approved_hash": def_hash(raw),
            "mode": mode, "status": "ok", "flags": flags}


def _merge_tools(old: list[dict], fresh: list[dict], first: bool) -> tuple[list[dict], dict]:
    """Pin definitions: unchanged tools keep the user's mode; changed ones are held for review; new ones start hidden."""
    by_name = {t["name"]: t for t in old}
    out, diff = [], {"added": [], "changed": [], "removed": []}
    for raw in fresh[:MAX_TOOLS]:
        rec = _tool_record(raw)
        prev = by_name.pop(rec["name"], None)
        if prev is None:
            if not first:
                rec["status"] = "new"
                diff["added"].append(rec["name"])
            out.append(rec)
            continue
        rec["mode"] = prev.get("mode", rec["mode"]) if not rec["flags"] else "off"
        rec["approved_hash"] = prev.get("approved_hash") or prev.get("hash")
        if prev.get("status") == "new":
            rec["status"] = "new"
        elif rec["hash"] != rec["approved_hash"]:
            rec["status"] = "changed"
            rec["previous_description"] = prev.get("previous_description") or prev.get("description", "")
            diff["changed"].append(rec["name"])
        out.append(rec)
    diff["removed"] = sorted(by_name)
    return out, diff


# ------------------------------------------------------------------ catalog
def sync_catalog(store) -> None:
    """Rebuild the MCP part of the shared TOOLS dict from the stored config."""
    for k in [k for k, v in TOOLS.items() if str(v.get("connector", "")).startswith("mcp:")]:
        TOOLS.pop(k, None)
    for s in servers(store):
        if not s.get("enabled", True):
            continue
        for t in s.get("tools") or []:
            if t.get("mode") == "off" or t.get("status") != "ok":
                continue
            name = tool_name(s["id"], t["name"])
            read = t.get("kind") == "read"
            risk = "high" if t.get("mode") == "ask" else ("low" if read else "medium")
            label = f"{t['title']} — " if t.get("title") else ""
            TOOLS[name] = {
                "connector": f"mcp:{s['id']}", "capability": "read" if read else "write",
                "operation": "read" if read else "write", "risk": risk,
                "data_class": s.get("data_class") or "CONFIDENTIAL",
                "description": f"[MCP · {s['name']}] {label}{t.get('description') or t['name']}"
                               + ("" if read else " (changes data in " + s["name"] + ")"),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                "mcp": {"server": s["id"], "tool": t["name"], "mode": t.get("mode"), "kind": t.get("kind")},
            }


def tool_info(store, tool: str) -> tuple[dict | None, dict | None]:
    t = TOOLS.get(tool) or {}
    m = t.get("mcp")
    if not m:
        return None, None
    s = server(store, m["server"])
    if not s:
        return None, None
    rec = next((x for x in s.get("tools") or [] if x["name"] == m["tool"]), None)
    return s, rec


def status(store) -> dict:
    out = []
    for s in servers(store):
        tools = [t for t in s.get("tools") or [] if t.get("status") == "ok" and t.get("mode") != "off"]
        out.append({"id": s["id"], "name": s["name"], "enabled": s.get("enabled", True), "tools": len(tools),
                    "prefix": f"mcp_{s['id']}__", "instructions": truncate(s.get("instructions", ""), 400)})
    return {"ready": any(x["enabled"] and x["tools"] for x in out), "servers": out}


# ------------------------------------------------------------------ sessions
async def _drop(sid: str):
    c = _sessions.pop(sid, None)
    if c:
        try:
            await c.close()
        except Exception:
            pass


async def _headers(store, s: dict, force_refresh: bool = False) -> dict:
    """Stored static headers, or a fresh OAuth bearer token (refreshed when near expiry or after a 401)."""
    if s.get("auth") == "oauth":
        try:
            return await mcp_oauth.auth_headers(store, handle(s["id"]), force_refresh=force_refresh)
        except mcp_oauth.OAuthError as e:
            raise MCPError(f"OAuth 授权失效，请在「连接 → MCP」重新登录 (sign in again): {e}", auth=True)
    return (store.get_secret(handle(s["id"])) or {}).get("headers") or {}


async def _session(store, s: dict, force_refresh: bool = False):
    lock = _locks.setdefault(s["id"], asyncio.Lock())
    async with lock:
        c = _sessions.get(s["id"])
        if c is not None and not force_refresh:
            return c
        headers = await _headers(store, s, force_refresh)
        c = await connect(s["url"], headers, s.get("transport") or "auto")
        _sessions[s["id"]] = c
        return c


async def _discover(url: str, headers: dict, transport: str = "auto") -> tuple[object, list[dict]]:
    c = await connect(url, headers, transport)
    try:
        if c.capabilities and "tools" not in c.capabilities:
            tools = []
        else:
            tools = await c.list_tools(MAX_TOOLS)
        return c, tools
    except Exception:
        await c.close()
        raise


# ------------------------------------------------------------------ user operations
async def add_server(store, *, name: str, url: str, auth_type: str = "none", token: str = "", header_name: str = "",
                     data_class: str = "CONFIDENTIAL", oauth: dict | None = None) -> dict:
    name = _clean_text(name, 40)
    if not name:
        raise HubError("请给这个服务器起个名字 (name required)")
    srvs = servers(store)
    if len(srvs) >= MAX_SERVERS:
        raise HubError(f"最多 {MAX_SERVERS} 个 MCP 服务器 (limit reached)")
    try:
        url = check_server_url(url)
    except MCPError as e:
        raise HubError(str(e))
    if any(s["url"] == url for s in srvs):
        raise HubError("这个地址已经添加过了 (already added)")
    headers = {"Authorization": f"Bearer {oauth['access_token']}"} if oauth else build_headers(auth_type, token, header_name)
    try:
        client, raw_tools = await _discover(url, headers)
    except MCPError as e:
        raise HubError(f"连接失败 connection failed：{e}")
    sid = slugify(name, {s["id"] for s in srvs})
    tools, _ = _merge_tools([], raw_tools, first=True)
    rec = {"id": sid, "name": name, "url": url, "transport": client.transport, "enabled": True,
           "data_class": data_class if data_class in DATA_CLASSES else "CONFIDENTIAL",
           "auth": "oauth" if oauth else (auth_type if headers else "none"), "header_name": header_name if auth_type == "header" else "",
           "server_info": {k: str(v)[:80] for k, v in (client.server_info or {}).items() if k in ("name", "version", "title")},
           "protocol": client.protocol_version, "instructions": _clean_text(client.instructions, 1500),
           "tools": tools, "last_sync": time.time(), "last_error": "", "created_at": time.time()}
    if oauth:
        mcp_oauth.save(store, handle(sid), "mcp", oauth)
    elif headers:
        store.put_secret("mcp", {"headers": headers}, handle=handle(sid))
    await _drop(sid)
    _sessions[sid] = client
    _save(store, srvs + [rec])
    return rec


async def refresh(store, sid: str) -> dict:
    s = server(store, sid)
    if not s:
        raise HubError("找不到这个 MCP 服务器 (server not found)")
    await _drop(sid)
    try:
        headers = await _headers(store, s)
        client, raw = await _discover(s["url"], headers, s.get("transport") or "auto")
    except MCPError as e:
        _update(store, sid, lambda x: {**x, "last_error": str(e)[:300]})
        raise HubError(f"刷新失败 refresh failed：{e}")
    _sessions[sid] = client
    diff: dict = {}

    def upd(x):
        nonlocal diff
        x["tools"], diff = _merge_tools(x.get("tools") or [], raw, first=False)
        x["transport"] = client.transport
        x["last_sync"], x["last_error"] = time.time(), ""
        x["instructions"] = _clean_text(client.instructions, 1500)
        return x
    rec = _update(store, sid, upd)
    return {"server": rec, "diff": diff}


def set_server(store, sid: str, *, enabled=None, data_class=None, name=None) -> dict:
    def upd(x):
        if enabled is not None:
            x["enabled"] = bool(enabled)
        if data_class in DATA_CLASSES:
            x["data_class"] = data_class
        if name:
            x["name"] = _clean_text(name, 40) or x["name"]
        return x
    return _update(store, sid, upd)


def set_tool(store, sid: str, tname: str, *, mode: str | None = None, accept: bool = False) -> dict:
    if mode is not None and mode not in MODES:
        raise HubError("mode 只能是 auto / ask / off")

    def upd(x):
        tools = [dict(t) for t in x.get("tools") or []]
        t = next((t for t in tools if t["name"] == tname), None)
        if not t:
            raise HubError("找不到这个工具 (tool not found)")
        if mode is not None:
            t["mode"] = mode
        if accept or (mode is not None and t.get("status") == "new" and mode != "off"):
            t["status"] = "ok"
            t["approved_hash"] = t["hash"]
            t.pop("previous_description", None)
        x["tools"] = tools
        return x
    return _update(store, sid, upd)


async def remove_server(store, sid: str) -> None:
    await _drop(sid)
    store.delete_secret(handle(sid))
    _save(store, [s for s in servers(store) if s["id"] != sid])


async def test_server(store, sid: str) -> dict:
    s = server(store, sid)
    if not s:
        raise HubError("找不到这个 MCP 服务器 (server not found)")
    await _drop(sid)
    try:
        c = await _session(store, s)
        await c.request("ping", {}, timeout=15)
    except MCPError as e:
        await _drop(sid)
        _update(store, sid, lambda x: {**x, "last_error": str(e)[:300]})
        raise HubError(str(e))
    _update(store, sid, lambda x: {**x, "last_error": ""})
    return {"ok": True, "server_info": getattr(c, "server_info", {}), "transport": c.transport}


# ------------------------------------------------------------------ execution
def _format_content(res: dict) -> str:
    parts = []
    for c in res.get("content") or []:
        if not isinstance(c, dict):
            continue
        ty = c.get("type")
        if ty == "text":
            parts.append(str(c.get("text", "")))
        elif ty in ("image", "audio"):
            parts.append(f"[{ty} omitted: {c.get('mimeType', '')}]")
        elif ty == "resource":
            r = c.get("resource") or {}
            parts.append(str(r.get("text")) if r.get("text") is not None else f"[resource {r.get('uri', '')} {r.get('mimeType', '')}]")
        elif ty == "resource_link":
            parts.append(f"[link] {c.get('name', '')} {c.get('uri', '')}")
    text = "\n".join(p for p in parts if p)
    if res.get("structuredContent") is not None and len(text) < 200:
        text = (text + "\n" if text else "") + json.dumps(res["structuredContent"], ensure_ascii=False, default=str)
    return truncate(text, 30000)


async def call(store, tool: str, args: dict, task_id: str) -> dict:
    from app.sentinel.actions import ActionError  # local import: actions imports this module
    s, rec = tool_info(store, tool)
    if not s or not rec:
        raise ActionError("这个 MCP 工具已不存在 (tool no longer available)")
    if not s.get("enabled", True):
        raise ActionError(f"MCP 服务器「{s['name']}」已停用 (server disabled)")
    try:
        try:
            c = await _session(store, s)
            res = await c.call_tool(rec["name"], args or {}, timeout=180)
        except MCPError as e:
            if not (e.auth and s.get("auth") == "oauth"):
                raise
            await _drop(s["id"])           # the access token expired early or was revoked: refresh once and retry
            c = await _session(store, s, force_refresh=True)
            res = await c.call_tool(rec["name"], args or {}, timeout=180)
    except MCPError as e:
        await _drop(s["id"])
        raise ActionError(f"MCP「{s['name']}」调用失败 call failed：{e}")
    text = _format_content(res)
    flags = guard.scan_injection(text)
    store.update_task_ctx(task_id, taint=s.get("data_class") or "CONFIDENTIAL", injection=flags or None)
    if res.get("isError"):
        raise ActionError(f"MCP 工具返回错误 tool error：{truncate(text, 1500)}")
    return guard.wrap_untrusted(f"MCP {s['name']} · {rec['name']}", text or "(empty result)",
                                meta={"server": s["name"], "tool": rec["name"]})
