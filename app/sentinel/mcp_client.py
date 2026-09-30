"""Minimal MCP (Model Context Protocol) client used by Sentinel.

Supports the two HTTP transports:
* Streamable HTTP (spec 2025-03-26+): JSON-RPC via POST, reply as JSON or as an SSE stream, optional Mcp-Session-Id.
* Legacy HTTP+SSE (spec 2024-11-05): GET an SSE stream, the first `endpoint` event says where to POST messages;
  replies arrive on the stream.

Only the client features OMuse needs are implemented: initialize, tools/list, tools/call, ping.
Server->client requests (sampling, elicitation, roots) are answered with "method not found" — a third-party
server can never make OMuse' LLM do anything through this channel.
stdio servers are deliberately not supported: Sentinel holds the vault and must not spawn arbitrary commands.
"""
from __future__ import annotations

import asyncio
import json
import ipaddress
from urllib.parse import urljoin, urlparse

import httpx

from app.common.util import VERSION

PROTOCOL_VERSION = "2025-06-18"
MAX_BYTES = 4 * 1024 * 1024
CLIENT_INFO = {"name": "OMuse", "version": VERSION}


class MCPError(Exception):
    def __init__(self, msg: str, *, auth: bool = False, code: int | None = None):
        super().__init__(msg)
        self.auth = auth
        self.code = code


def _private_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host in ("localhost",) or host.endswith((".local", ".localhost", ".internal", ".svc", ".cluster.local", ".lan",
                                               ".home.arpa", ".olares.local")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return "." not in host  # single-label names (k8s services) are internal


def check_server_url(url: str) -> str:
    """Return a normalised URL or raise MCPError. Plain http is only allowed for LAN / in-cluster servers
    so tokens never travel unencrypted over the internet."""
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise MCPError("MCP 服务器地址必须是 http(s):// 开头的网址 (URL must start with http:// or https://)")
    if p.username or p.password:
        raise MCPError("请不要把账号密码写在网址里，改用下面的「认证 Auth」字段 (no credentials in the URL)")
    if p.scheme == "http" and not _private_host(p.hostname):
        raise MCPError("公网 MCP 服务器必须使用 https:// (plain http only for LAN / local servers)")
    return url


def _parse_sse_block(lines: list[str]) -> tuple[str, str]:
    event, data = "message", []
    for ln in lines:
        if ln.startswith(":"):
            continue
        k, _, v = ln.partition(":")
        v = v[1:] if v.startswith(" ") else v
        if k == "event":
            event = v
        elif k == "data":
            data.append(v)
    return event, "\n".join(data)


class _Base:
    def __init__(self, url: str, headers: dict | None = None, timeout: float = 60.0):
        self.url = url
        self.headers = {k: v for k, v in (headers or {}).items() if k and v}
        self.timeout = timeout
        self._id = 0
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.protocol_version = PROTOCOL_VERSION
        self.instructions = ""
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False,
                                         headers={"User-Agent": f"OMuse/{VERSION} (MCP client)"})

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    @staticmethod
    def _check_status(r: httpx.Response):
        if r.status_code in (401, 403):
            raise MCPError(f"认证失败 authentication failed (HTTP {r.status_code})：请检查令牌/请求头 (check token)", auth=True,
                           code=r.status_code)
        if 300 <= r.status_code < 400:
            raise MCPError(f"服务器要求跳转到 {r.headers.get('location', '?')}，请直接填写最终地址 (redirect not followed)",
                           code=r.status_code)
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}", code=r.status_code)

    @staticmethod
    def _result(msg: dict):
        if "error" in msg and msg["error"]:
            e = msg["error"]
            raise MCPError(f"MCP 错误 {e.get('code', '')}: {str(e.get('message', ''))[:300]}", code=e.get("code"))
        return msg.get("result") or {}

    def _answer_server_request(self, msg: dict) -> dict:
        if msg.get("method") == "ping":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported by OMuse"}}

    async def initialize(self) -> dict:
        res = await self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                "clientInfo": CLIENT_INFO})
        self.server_info = res.get("serverInfo") or {}
        self.capabilities = res.get("capabilities") or {}
        self.protocol_version = str(res.get("protocolVersion") or PROTOCOL_VERSION)
        self.instructions = str(res.get("instructions") or "")[:2000]
        await self.notify("notifications/initialized")
        return res

    async def list_tools(self, limit: int = 200) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):
            res = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in (res.get("tools") or []) if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor or len(tools) >= limit:
                break
        return tools[:limit]

    async def call_tool(self, name: str, arguments: dict, timeout: float | None = None) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)

    async def request(self, method: str, params: dict | None = None, timeout: float | None = None):
        raise NotImplementedError

    async def notify(self, method: str, params: dict | None = None):
        raise NotImplementedError

    async def close(self):
        await self._client.aclose()


class StreamableHTTP(_Base):
    transport = "streamable_http"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.session_id = ""

    def _hdrs(self) -> dict:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.server_info:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    async def notify(self, method: str, params: dict | None = None):
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}
        try:
            r = await self._client.post(self.url, json=msg, headers=self._hdrs())
            await r.aclose()
        except httpx.HTTPError:
            pass

    async def _reply(self, msg: dict):
        try:
            r = await self._client.post(self.url, json=msg, headers=self._hdrs())
            await r.aclose()
        except httpx.HTTPError:
            pass

    async def request(self, method: str, params: dict | None = None, timeout: float | None = None, _retry: bool = True):
        rid = self._next_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        try:
            return await asyncio.wait_for(self._request(msg, rid, _retry), timeout or self.timeout)
        except asyncio.TimeoutError:
            raise MCPError(f"MCP 服务器响应超时 timeout ({method})")
        except httpx.HTTPError as e:
            raise MCPError(f"无法连接 MCP 服务器 connection failed: {type(e).__name__}: {str(e)[:200]}")

    async def _request(self, msg: dict, rid: int, retry: bool):
        async with self._client.stream("POST", self.url, json=msg, headers=self._hdrs()) as r:
            if r.status_code == 404 and self.session_id and retry and msg["method"] != "initialize":
                # session expired on the server: start a new one and retry once
                self.session_id = ""
                self.server_info = {}
                await self.initialize()
                return await self.request(msg["method"], msg["params"], _retry=False)
            self._check_status(r)
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype == "text/event-stream":
                buf: list[str] = []
                async for line in r.aiter_lines():
                    if line.strip():
                        buf.append(line.rstrip("\r"))
                        continue
                    if not buf:
                        continue
                    ev, data = _parse_sse_block(buf)
                    buf = []
                    if ev != "message" or not data:
                        continue
                    found = await self._handle(data, rid)
                    if found is not None:
                        return found
                if buf:
                    ev, data = _parse_sse_block(buf)
                    found = await self._handle(data, rid) if data else None
                    if found is not None:
                        return found
                raise MCPError("MCP 服务器提前结束了响应流 (stream ended without a result)")
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise MCPError("MCP 响应过大 (response too large)")
            if not body.strip():
                raise MCPError("MCP 服务器返回了空响应 (empty response)")
            found = await self._handle(body.decode("utf-8", "replace"), rid)
            if found is None:
                raise MCPError("MCP 响应中没有找到对应结果 (no matching response id)")
            return found

    async def _handle(self, data: str, rid: int):
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return None
        for m in (obj if isinstance(obj, list) else [obj]):
            if not isinstance(m, dict):
                continue
            if m.get("id") == rid and ("result" in m or "error" in m):
                return self._result(m)
            if "method" in m and "id" in m:  # server -> client request
                asyncio.create_task(self._reply(self._answer_server_request(m)))
        return None

    async def close(self):
        if self.session_id:
            try:
                await self._client.delete(self.url, headers=self._hdrs(), timeout=5)
            except Exception:
                pass
        await super().close()


class LegacySSE(_Base):
    transport = "sse"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.endpoint = ""
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._closed = False
        self._error: Exception | None = None

    async def _connect(self):
        if self._reader and not self._reader.done():
            await asyncio.wait_for(self._ready.wait(), 20)
            return
        self._ready = asyncio.Event()
        self._error = None
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._ready.wait(), 20)
        except asyncio.TimeoutError:
            raise MCPError("SSE 连接超时：没有收到 endpoint 事件 (no endpoint event)")
        if self._error:
            raise self._error if isinstance(self._error, MCPError) else MCPError(str(self._error))

    async def _read_loop(self):
        try:
            async with self._client.stream("GET", self.url, headers={"Accept": "text/event-stream", **self.headers},
                                           timeout=httpx.Timeout(None, connect=10.0)) as r:
                self._check_status(r)
                if "text/event-stream" not in r.headers.get("content-type", ""):
                    raise MCPError("这个地址不是 MCP SSE 端点 (not an SSE endpoint)")
                buf: list[str] = []
                async for line in r.aiter_lines():
                    if line.strip():
                        buf.append(line.rstrip("\r"))
                        continue
                    if not buf:
                        continue
                    ev, data = _parse_sse_block(buf)
                    buf = []
                    if ev == "endpoint":
                        ep = urljoin(self.url, data.strip())
                        if urlparse(ep).netloc != urlparse(self.url).netloc:
                            raise MCPError("SSE endpoint 指向了其他主机，已拒绝 (endpoint on a different host)")
                        self.endpoint = ep
                        self._ready.set()
                    elif ev == "message" and data:
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        for m in (obj if isinstance(obj, list) else [obj]):
                            if not isinstance(m, dict):
                                continue
                            if "method" in m and "id" in m:
                                asyncio.create_task(self._post(self._answer_server_request(m)))
                            elif m.get("id") in self._pending and not self._pending[m["id"]].done():
                                self._pending[m["id"]].set_result(m)
        except Exception as e:  # noqa: BLE001
            self._error = e if isinstance(e, MCPError) else MCPError(f"SSE 连接断开 stream closed: {type(e).__name__}: {str(e)[:200]}")
        finally:
            self._ready.set()
            err = self._error or MCPError("SSE 连接已关闭 (stream closed)")
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(err)
            self._reader = None if self._closed else self._reader

    async def _post(self, msg: dict):
        r = await self._client.post(self.endpoint, json=msg, headers={"Content-Type": "application/json", **self.headers})
        self._check_status(r)

    async def notify(self, method: str, params: dict | None = None):
        try:
            await self._connect()
            await self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})
        except Exception:
            pass

    async def request(self, method: str, params: dict | None = None, timeout: float | None = None):
        try:
            await self._connect()
            rid = self._next_id()
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            try:
                await self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
                msg = await asyncio.wait_for(fut, timeout or self.timeout)
            finally:
                self._pending.pop(rid, None)
            return self._result(msg)
        except asyncio.TimeoutError:
            raise MCPError(f"MCP 服务器响应超时 timeout ({method})")
        except httpx.HTTPError as e:
            raise MCPError(f"无法连接 MCP 服务器 connection failed: {type(e).__name__}: {str(e)[:200]}")

    async def close(self):
        self._closed = True
        if self._reader:
            self._reader.cancel()
        await super().close()


async def connect(url: str, headers: dict | None = None, transport: str = "auto", timeout: float = 60.0) -> _Base:
    """Open and initialise a session. transport: auto | streamable_http | sse."""
    url = check_server_url(url)
    if transport == "auto" and urlparse(url).path.rstrip("/").endswith("/sse"):
        transport = "sse"
    if transport in ("auto", "streamable_http"):
        c = StreamableHTTP(url, headers, timeout)
        try:
            await c.initialize()
            return c
        except MCPError as e:
            await c.close()
            if transport == "streamable_http" or e.auth or e.code not in (400, 404, 405, 406, 415):
                raise
    c = LegacySSE(url, headers, timeout)
    try:
        await c.initialize()
        return c
    except Exception:
        await c.close()
        raise
