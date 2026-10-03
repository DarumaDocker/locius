"""OAuth 2.1 for remote MCP servers (MCP authorization spec 2025-06-18), e.g. DialMCP.

Flow (all on Sentinel; tokens never reach the runtime or the LLM):
1. discover(): ask the MCP server (401 + WWW-Authenticate `resource_metadata`, or the well-known Protected Resource
   Metadata URL) which authorization server protects it, then read that server's metadata (RFC 8414 / OIDC).
2. Dynamic Client Registration (RFC 7591) as a public client ("none" auth) with OMuse's own callback URL.
3. Authorization Code + PKCE (S256) in the user's browser, with `resource` = the MCP server (RFC 8707) so the
   token is only good for that server. `state` is single-use and expires after 15 minutes.
4. The callback exchanges the code; access + refresh tokens go into the vault under a handle. auth_headers()
   refreshes them shortly before they expire (or when the server answers 401).

Every URL taken from metadata goes through check_server_url (https only on the public internet).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx

from app.common.util import VERSION
from app.sentinel.mcp_client import MCPError, check_server_url

FLOW_TTL = 15 * 60
CLIENT_NAME = "OMuse"
_FLOWS: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


class OAuthError(Exception):
    pass


def _safe(url: str) -> str:
    try:
        return check_server_url(url)
    except MCPError as e:
        raise OAuthError(f"授权服务器地址不安全 (unsafe OAuth URL {url[:120]}): {e}")


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _www_auth_param(header: str, key: str) -> str:
    import re
    m = re.search(rf'{key}\s*=\s*"([^"]+)"', header or "") or re.search(rf"{key}\s*=\s*([^,\s]+)", header or "")
    return m.group(1) if m else ""


async def _get_json(c: httpx.AsyncClient, url: str) -> dict | None:
    try:
        r = await c.get(_safe(url), headers={"Accept": "application/json"})
    except OAuthError:
        raise
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


async def discover(server_url: str) -> dict:
    """-> {resource, issuer, authorization_endpoint, token_endpoint, registration_endpoint, scopes}"""
    server_url = _safe(server_url)
    p = urlparse(server_url)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
        prm_urls = []
        try:   # an unauthenticated MCP request should answer 401 and point at its resource metadata
            r = await c.post(server_url, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": CLIENT_NAME, "version": VERSION}}},
                headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
            u = _www_auth_param(r.headers.get("www-authenticate", ""), "resource_metadata")
            if u:
                prm_urls.append(u)
        except httpx.HTTPError:
            pass
        path = p.path.rstrip("/")
        if path:
            prm_urls.append(f"{_origin(server_url)}/.well-known/oauth-protected-resource{path}")
        prm_urls.append(f"{_origin(server_url)}/.well-known/oauth-protected-resource")
        prm = None
        for u in prm_urls:
            prm = await _get_json(c, u)
            if prm and prm.get("authorization_servers"):
                break
            prm = None
        issuer = str((prm or {}).get("authorization_servers", [_origin(server_url)])[0]).rstrip("/")
        ip = urlparse(_safe(issuer))
        ipath = ip.path.rstrip("/")
        cands = ([f"{_origin(issuer)}/.well-known/oauth-authorization-server{ipath}",
                  f"{_origin(issuer)}/.well-known/openid-configuration{ipath}", f"{issuer}/.well-known/openid-configuration"]
                 if ipath else [f"{issuer}/.well-known/oauth-authorization-server", f"{issuer}/.well-known/openid-configuration"])
        meta = None
        for u in cands:
            meta = await _get_json(c, u)
            if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                break
            meta = None
        if not meta:   # 2025-03-26 servers without metadata: default endpoints at the issuer
            meta = {"authorization_endpoint": f"{issuer}/authorize", "token_endpoint": f"{issuer}/token",
                    "registration_endpoint": f"{issuer}/register"}
        methods = meta.get("code_challenge_methods_supported")
        if methods and "S256" not in methods:
            raise OAuthError("授权服务器不支持 PKCE S256 (server does not support PKCE S256)")
    scopes = (prm or {}).get("scopes_supported") or []
    return {"resource": str((prm or {}).get("resource") or server_url), "issuer": issuer,
            "authorization_endpoint": _safe(meta["authorization_endpoint"]), "token_endpoint": _safe(meta["token_endpoint"]),
            "registration_endpoint": _safe(meta["registration_endpoint"]) if meta.get("registration_endpoint") else "",
            "scopes": [str(s) for s in scopes if isinstance(s, str)][:20]}


async def register(meta: dict, redirect_uri: str) -> dict:
    if not meta.get("registration_endpoint"):
        raise OAuthError("这个服务器不支持自动注册客户端 (no dynamic client registration)")
    body = {"client_name": CLIENT_NAME, "redirect_uris": [redirect_uri], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none", "software_id": "omuse", "software_version": VERSION}
    if meta.get("scopes"):
        body["scope"] = " ".join(meta["scopes"])
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.post(meta["registration_endpoint"], json=body)
        except httpx.HTTPError as e:
            raise OAuthError(f"注册客户端失败 (client registration failed): {e}")
    if r.status_code not in (200, 201):
        raise OAuthError(f"注册客户端失败 (client registration failed): HTTP {r.status_code} {r.text[:200]}")
    d = r.json()
    if not d.get("client_id"):
        raise OAuthError("注册客户端失败：没有 client_id (no client_id)")
    return {"client_id": d["client_id"], "client_secret": d.get("client_secret") or "",
            "auth_method": d.get("token_endpoint_auth_method") or ("client_secret_post" if d.get("client_secret") else "none")}


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def begin(server_url: str, redirect_uri: str, purpose: str, extra: dict | None = None) -> dict:
    """Start the browser login. -> {auth_url, state}. `purpose` + `extra` come back from complete()."""
    meta = await discover(server_url)
    client = await register(meta, redirect_uri)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    for k in [k for k, v in _FLOWS.items() if v["expires_at"] < time.time()]:
        _FLOWS.pop(k, None)
    _FLOWS[state] = {"server_url": server_url, "redirect_uri": redirect_uri, "meta": meta, "client": client,
                     "verifier": verifier, "purpose": purpose, "extra": extra or {}, "expires_at": time.time() + FLOW_TTL}
    q = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256", "resource": meta["resource"]}
    if meta.get("scopes"):
        q["scope"] = " ".join(meta["scopes"])
    sep = "&" if "?" in meta["authorization_endpoint"] else "?"
    return {"auth_url": meta["authorization_endpoint"] + sep + urlencode(q), "state": state}


def _client_auth(client: dict, form: dict) -> dict:
    headers = {"Accept": "application/json"}
    if client.get("client_secret") and client.get("auth_method") == "client_secret_basic":
        tok = base64.b64encode(f"{client['client_id']}:{client['client_secret']}".encode()).decode()
        headers["Authorization"] = f"Basic {tok}"
    elif client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    return headers


async def _token(token_endpoint: str, form: dict, client: dict) -> dict:
    headers = _client_auth(client, form)
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.post(token_endpoint, data=form, headers=headers)
        except httpx.HTTPError as e:
            raise OAuthError(f"换取令牌失败 (token request failed): {e}")
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code != 200 or not d.get("access_token"):
        raise OAuthError(f"换取令牌失败 (token request failed): HTTP {r.status_code} "
                         f"{d.get('error', '')} {d.get('error_description', '') or r.text[:200]}".strip())
    return d


def _record(meta: dict, client: dict, tok: dict, old: dict | None = None) -> dict:
    exp = tok.get("expires_in")
    return {"client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
            "auth_method": client.get("auth_method", "none"), "token_endpoint": meta["token_endpoint"],
            "resource": meta["resource"], "issuer": meta.get("issuer", ""),
            "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token") or (old or {}).get("refresh_token", ""),
            "expires_at": time.time() + float(exp) if exp else 0, "scope": tok.get("scope", "")}


async def complete(state: str, code: str, error: str = "") -> tuple[dict, dict]:
    """Finish the login from the callback. -> (flow, oauth record to store in the vault)."""
    f = _FLOWS.pop(state or "", None)
    if not f or f["expires_at"] < time.time():
        raise OAuthError("登录已过期或无效，请重新开始 (login expired or invalid — start again)")
    if error:
        raise OAuthError(f"授权被拒绝或失败 (authorization failed): {error}")
    if not code:
        raise OAuthError("缺少授权码 (missing code)")
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": f["redirect_uri"],
            "client_id": f["client"]["client_id"], "code_verifier": f["verifier"], "resource": f["meta"]["resource"]}
    tok = await _token(f["meta"]["token_endpoint"], form, f["client"])
    return f, _record(f["meta"], f["client"], tok)


async def refresh(store, handle: str) -> dict:
    sec = store.get_secret(handle) or {}
    o = sec.get("oauth") or {}
    if not o.get("refresh_token"):
        raise OAuthError("授权已失效，请重新登录 (session expired — sign in again)")
    form = {"grant_type": "refresh_token", "refresh_token": o["refresh_token"], "client_id": o["client_id"],
            "resource": o.get("resource", "")}
    tok = await _token(o["token_endpoint"], form, {"client_id": o["client_id"], "client_secret": o.get("client_secret", ""),
                                                   "auth_method": o.get("auth_method", "none")})
    meta = {"token_endpoint": o["token_endpoint"], "resource": o.get("resource", ""), "issuer": o.get("issuer", "")}
    rec = _record(meta, {"client_id": o["client_id"], "client_secret": o.get("client_secret", ""),
                         "auth_method": o.get("auth_method", "none")}, tok, o)
    store.put_secret(sec.get("connector", "oauth"), {**sec, "oauth": rec}, handle=handle)
    return rec


async def auth_headers(store, handle: str, force_refresh: bool = False) -> dict:
    """Bearer header for the server behind `handle`, refreshing the token when it is (nearly) expired."""
    lock = _locks.setdefault(handle, asyncio.Lock())
    async with lock:
        o = (store.get_secret(handle) or {}).get("oauth") or {}
        if not o.get("access_token"):
            raise OAuthError("还没有登录 (not signed in)")
        if force_refresh or (o.get("expires_at") and o["expires_at"] - time.time() < 60):
            o = await refresh(store, handle)
        return {"Authorization": f"Bearer {o['access_token']}"}


def save(store, handle: str, connector: str, rec: dict, **extra):
    store.put_secret(connector, {"connector": connector, "oauth": rec, **extra}, handle=handle)


def connected(store, handle: str) -> bool:
    return bool(((store.get_secret(handle) or {}).get("oauth") or {}).get("access_token"))


def check_redirect(redirect_uri: str, host: str, path_suffix: str) -> str:
    """The callback must be OMuse's own address (the host the browser is using) and the fixed callback path."""
    p = urlparse(str(redirect_uri or ""))
    if p.scheme not in ("http", "https") or not p.netloc or p.query or p.fragment:
        raise OAuthError("回调地址无效 (invalid redirect URI)")
    if p.netloc.lower() != (host or "").split(",")[0].strip().lower():
        raise OAuthError("回调地址必须是 OMuse 自己的网址 (redirect URI must be this OMuse address)")
    if not p.path.endswith(path_suffix):
        raise OAuthError("回调地址路径不对 (wrong redirect path)")
    if p.scheme == "http" and p.hostname not in ("localhost", "127.0.0.1"):
        try:
            check_server_url(redirect_uri)
        except MCPError as e:
            raise OAuthError(str(e))
    return f"{p.scheme}://{p.netloc}{p.path}"
