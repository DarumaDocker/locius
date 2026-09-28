"""Fake Notion (/v1/...) and Slack (/api/...) APIs for tests (port 8094)."""
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
BOT = "bot-user-0000-0000-000000000001"
HUMAN = "human-user-000-0000-000000000002"
DB_ID = "11111111-2222-4333-8444-555555555555"
PAGE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def iso(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:00.000Z")


def rt(t):
    return [{"type": "text", "text": {"content": t}, "plain_text": t, "annotations": {}}]


N = {"pages": {}, "blocks": {}, "log": []}
S = {"messages": {"C1": [], "C2": []}, "posted": []}


def reset():
    N["pages"] = {
        PAGE_ID: {"object": "page", "id": PAGE_ID, "url": "https://www.notion.so/Plan-" + PAGE_ID.replace("-", ""),
                  "parent": {"type": "workspace"}, "archived": False, "created_time": iso(time.time() - 86400),
                  "last_edited_time": iso(time.time() - 86400), "last_edited_by": {"id": HUMAN},
                  "properties": {"title": {"type": "title", "title": rt("Q4 Plan")}}},
    }
    for i, (name, status) in enumerate([("Write launch post", "Doing"), ("Fix login bug", "Done")]):
        pid = f"cccccccc-0000-4000-8000-00000000000{i}"
        N["pages"][pid] = {"object": "page", "id": pid, "url": f"https://www.notion.so/{pid.replace('-', '')}",
                           "parent": {"type": "database_id", "database_id": DB_ID}, "archived": False,
                           "created_time": iso(time.time() - 7200), "last_edited_time": iso(time.time() - 7200),
                           "last_edited_by": {"id": HUMAN},
                           "properties": {"Name": {"type": "title", "title": rt(name)},
                                          "Status": {"type": "status", "status": {"name": status}},
                                          "Due": {"type": "date", "date": {"start": "2026-10-01"}}}}
    N["blocks"] = {PAGE_ID: [{"object": "block", "id": "b1", "type": "heading_2", "has_children": False,
                              "heading_2": {"rich_text": rt("Goals")}},
                             {"object": "block", "id": "b2", "type": "bulleted_list_item", "has_children": False,
                              "bulleted_list_item": {"rich_text": rt("Ship Locius 0.2")}}]}
    N["log"] = []
    S["messages"] = {"C1": [{"ts": "1790000000.000100", "user": "U2", "text": "old message"}], "C2": []}
    S["posted"] = []


reset()
DB = {"object": "database", "id": DB_ID, "title": rt("Tasks"), "url": "https://www.notion.so/" + DB_ID.replace("-", ""),
      "properties": {"Name": {"type": "title"}, "Status": {"type": "status"}, "Due": {"type": "date"}, "Tags": {"type": "multi_select"}}}


def auth(req):
    return req.headers.get("authorization", "") == "Bearer ntn_testtoken1234567890abcdef"


def nerr(code, msg):
    return JSONResponse({"object": "error", "status": code, "message": msg}, status_code=code)


@app.post("/_reset")
async def _reset():
    reset()
    return {"ok": True}


@app.get("/_log")
async def _log():
    return {"notion": N["log"], "slack": S["posted"]}


@app.post("/_notion/edit")
async def n_edit(req: Request):
    b = await req.json()
    p = N["pages"][b["id"]]
    p["last_edited_time"] = iso()
    p["last_edited_by"] = {"id": b.get("by", HUMAN)}
    if b.get("status"):
        p["properties"]["Status"]["status"]["name"] = b["status"]
    return {"ok": True}


@app.post("/_slack/post")
async def s_post(req: Request):
    b = await req.json()
    ts = f"{time.time():.6f}"
    S["messages"].setdefault(b.get("channel", "C1"), []).append({"ts": ts, "user": b.get("user", "U2"), "text": b["text"],
                                                                 **({"bot_id": b["bot_id"]} if b.get("bot_id") else {})})
    return {"ts": ts}


# ------------------------------------------------------------------ Notion
@app.get("/v1/users/me")
async def me(req: Request):
    if not auth(req):
        return nerr(401, "API token is invalid.")
    return {"object": "user", "id": BOT, "name": "Locius", "type": "bot", "bot": {"workspace_name": "Lucas's Notion"}}


@app.post("/v1/search")
async def search(req: Request):
    if not auth(req):
        return nerr(401, "unauthorized")
    b = await req.json()
    q = (b.get("query") or "").lower()
    res = [p for p in N["pages"].values() if q in "".join(x["plain_text"] for x in next(v for v in p["properties"].values() if v["type"] == "title")["title"]).lower()]
    if (b.get("filter") or {}).get("value") == "database":
        res = [DB]
    return {"results": res[: b.get("page_size", 10)], "has_more": False}


@app.get("/v1/pages/{pid}")
async def get_page(pid: str, req: Request):
    if not auth(req):
        return nerr(401, "unauthorized")
    return N["pages"].get(pid) or nerr(404, "not found")


@app.patch("/v1/pages/{pid}")
async def patch_page(pid: str, req: Request):
    b = await req.json()
    p = N["pages"].get(pid)
    if not p:
        return nerr(404, "nf")
    for k, v in (b.get("properties") or {}).items():
        t = next(iter(v))
        p["properties"][k] = {"type": t, t: v[t] if t not in ("title", "rich_text") else rt("".join(x["text"]["content"] for x in v[t]))}
    if "archived" in b:
        p["archived"] = b["archived"]
    p["last_edited_time"], p["last_edited_by"] = iso(), {"id": BOT}
    N["log"].append({"op": "update", "id": pid, "body": b})
    return p


@app.post("/v1/pages")
async def create_page(req: Request):
    b = await req.json()
    pid = str(uuid.uuid4())
    props = {}
    for k, v in b["properties"].items():
        t = next(iter(v))
        props[k] = {"type": t, t: rt("".join(x["text"]["content"] for x in v[t])) if t in ("title", "rich_text") else v[t]}
    parent = b["parent"]
    p = {"object": "page", "id": pid, "url": f"https://www.notion.so/{pid.replace('-', '')}",
         "parent": {"type": "database_id", **parent} if "database_id" in parent else {"type": "page_id", **parent},
         "archived": False, "created_time": iso(), "last_edited_time": iso(), "last_edited_by": {"id": BOT}, "properties": props}
    N["pages"][pid] = p
    N["blocks"][pid] = b.get("children") or []
    N["log"].append({"op": "create", "id": pid, "body": b})
    return p


@app.get("/v1/blocks/{bid}/children")
async def children(bid: str):
    return {"results": N["blocks"].get(bid, []), "has_more": False}


@app.patch("/v1/blocks/{bid}/children")
async def append(bid: str, req: Request):
    b = await req.json()
    N["blocks"].setdefault(bid, []).extend(b["children"])
    N["log"].append({"op": "append", "id": bid, "n": len(b["children"])})
    return {"results": b["children"]}


@app.get("/v1/databases/{did}")
async def get_db(did: str, req: Request):
    if not auth(req):
        return nerr(401, "unauthorized")
    return DB if did == DB_ID else nerr(404, "nf")


@app.post("/v1/databases/{did}/query")
async def q_db(did: str, req: Request):
    b = await req.json()
    rows = [p for p in N["pages"].values() if (p["parent"].get("database_id") == DB_ID)]
    f = b.get("filter") or {}
    if f.get("timestamp") == "last_edited_time":
        after = f["last_edited_time"].get("on_or_after") or f["last_edited_time"].get("after")
        rows = [p for p in rows if p["last_edited_time"] >= after]
    elif f.get("property") == "Status":
        rows = [p for p in rows if p["properties"]["Status"]["status"]["name"] == f["status"]["equals"]]
    rows.sort(key=lambda p: p["last_edited_time"])
    return {"results": rows, "has_more": False}


# ------------------------------------------------------------------ Slack
@app.post("/api/{method}")
async def slack(method: str, req: Request):
    tok = req.headers.get("authorization", "")
    if tok not in ("Bearer xoxb-test-token-123456", "Bearer xoxp-test-token-123456"):
        return {"ok": False, "error": "invalid_auth"}
    try:
        b = await req.json()
    except Exception:
        b = dict(await req.form())
    if method == "auth.test":
        return {"ok": True, "team": "Acme", "team_id": "T1", "user": "locius", "user_id": "UBOT", "bot_id": "BBOT"}
    if method == "conversations.list":
        return {"ok": True, "channels": [{"id": "C1", "name": "general", "is_member": True}, {"id": "C2", "name": "sales", "is_member": False}],
                "response_metadata": {"next_cursor": ""}}
    if method == "conversations.history":
        ch = b.get("channel")
        if ch == "C2":
            return {"ok": False, "error": "not_in_channel"}
        msgs = S["messages"].get(ch, [])
        if b.get("oldest"):
            msgs = [m for m in msgs if float(m["ts"]) > float(b["oldest"])]
        return {"ok": True, "messages": list(reversed(msgs))[: int(b.get("limit", 100))]}
    if method == "conversations.replies":
        return {"ok": True, "messages": [m for m in S["messages"].get(b.get("channel"), []) if m["ts"] == b.get("ts")]}
    if method == "users.info":
        return {"ok": True, "user": {"name": b.get("user"), "profile": {"display_name": {"U2": "John", "UBOT": "locius"}.get(b.get("user"), "")}}}
    if method == "chat.postMessage":
        ts = f"{time.time():.6f}"
        S["posted"].append(b)
        S["messages"].setdefault(b["channel"], []).append({"ts": ts, "user": "UBOT", "bot_id": "BBOT", "text": b["text"]})
        return {"ok": True, "ts": ts, "channel": b["channel"]}
    if method == "search.messages":
        if "xoxb" in tok:
            return {"ok": False, "error": "not_allowed_token_type"}
        return {"ok": True, "messages": {"total": 1, "matches": [{"ts": "1.0", "user": "U2", "text": "found it", "channel": {"id": "C1", "name": "general"}, "permalink": "https://x"}]}}
    return {"ok": False, "error": "unknown_method"}


# ------------------------------------------------------------------ Google OAuth + Calendar (0.2.7)
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

G = {"codes": {}, "events": [], "log": [], "refresh_ok": True}


@app.get("/g/auth")
async def g_auth(client_id: str, redirect_uri: str, state: str, scope: str = "", access_type: str = ""):
    code = "code-" + uuid.uuid4().hex[:8]
    G["codes"][code] = {"client_id": client_id, "redirect_uri": redirect_uri, "scope": scope, "offline": access_type}
    return RedirectResponse(redirect_uri + "?" + urlencode({"code": code, "state": state}), status_code=302)


@app.post("/g/token")
async def g_token(req: Request):
    f = dict(await req.form())
    G["log"].append({"token": {k: v for k, v in f.items() if k != "client_secret"}})
    if f.get("client_secret") != "GOCSPX-test-secret-123":
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    if f.get("grant_type") == "authorization_code":
        c = G["codes"].pop(f.get("code", ""), None)
        if not c or c["redirect_uri"] != f.get("redirect_uri"):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return {"access_token": "gat-1", "refresh_token": "grt-1", "expires_in": 3600}
    if f.get("grant_type") == "refresh_token":
        if not G["refresh_ok"] or f.get("refresh_token") != "grt-1":
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return {"access_token": "gat-1", "expires_in": 3600}
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def gauth(req: Request):
    return req.headers.get("authorization") == "Bearer gat-1"


@app.get("/g/userinfo")
async def g_userinfo(req: Request):
    return {"email": "lucas@example.com"} if gauth(req) else JSONResponse({}, status_code=401)


@app.get("/cal/users/me/settings/timezone")
async def g_tz(req: Request):
    return {"value": "Asia/Singapore"} if gauth(req) else JSONResponse({}, status_code=401)


@app.post("/cal/freeBusy")
async def g_fb(req: Request):
    if not gauth(req):
        return JSONResponse({"error": {"message": "auth"}}, status_code=401)
    busy = [{"start": e["start"]["dateTime"], "end": e["end"]["dateTime"]} for e in G["events"] if "dateTime" in e["start"]]
    return {"calendars": {"primary": {"busy": busy}}}


@app.get("/cal/calendars/{cid}/events")
async def g_list(cid: str, req: Request):
    if not gauth(req):
        return JSONResponse({"error": {"message": "auth"}}, status_code=401)
    return {"items": G["events"]}


@app.post("/cal/calendars/{cid}/events")
async def g_create(cid: str, req: Request):
    if not gauth(req):
        return JSONResponse({"error": {"message": "auth"}}, status_code=401)
    b = await req.json()
    ev = {"id": "ev" + uuid.uuid4().hex[:6], "status": "confirmed", "htmlLink": "https://calendar.google.com/e", **b}
    G["events"].append(ev)
    G["log"].append({"create": b, "sendUpdates": req.query_params.get("sendUpdates")})
    return ev


@app.get("/_g")
async def g_state():
    return {"events": G["events"], "log": G["log"]}


@app.post("/_g/reset")
async def g_reset():
    G.update({"codes": {}, "events": [{"id": "busy1", "summary": "Board meeting", "status": "confirmed",
                                        "start": {"dateTime": "2026-10-03T18:00:00+08:00"}, "end": {"dateTime": "2026-10-03T18:45:00+08:00"}}],
              "log": [], "refresh_ok": True})
    return {"ok": True}


# a bot wall that answers 403 (like Akamai on OpenTable)
@app.get("/wall")
async def wall():
    return HTMLResponse("<html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>"
                        "You don't have permission to access \"http://www.opentable.test/\" on this server.<p>"
                        "Reference #18.4f2d3e17.1790590000.1a2b3c</body></html>", status_code=403)
