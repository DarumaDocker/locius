"""Minimal fake of the Telegram Bot API for tests (port 8091)."""
import asyncio, itertools
from fastapi import FastAPI, Request

app = FastAPI()
UPDATES, SENT, EDITS, ANSWERS, DOCS = [], [], [], [], []
ids = itertools.count(1000)
upd_ids = itertools.count(1)

@app.post("/_push")
async def push(req: Request):
    b = await req.json(); b["update_id"] = next(upd_ids); UPDATES.append(b); return {"ok": True, "update_id": b["update_id"]}

@app.get("/_log")
async def log():
    return {"sent": SENT, "edits": EDITS, "answers": ANSWERS, "docs": DOCS}

@app.api_route("/bot{token}/{method}", methods=["GET", "POST"])
async def api(token: str, method: str, req: Request):
    if method == "sendDocument":   # multipart upload
        f = await req.form()
        doc = f["document"]
        data = await doc.read()
        DOCS.append({"chat_id": f.get("chat_id"), "caption": f.get("caption"), "name": doc.filename, "size": len(data),
                     "head": data[:80].decode("utf-8", "replace")})
        return {"ok": True, "result": {"message_id": next(ids)}}
    try:
        b = await req.json()
    except Exception:
        b = dict(req.query_params)
    if method == "getMe":
        return {"ok": True, "result": {"id": 1, "username": "persona_test_bot"}}
    if method == "deleteWebhook":
        return {"ok": True, "result": True}
    if method == "getUpdates":
        off = int(b.get("offset", 0) or 0)
        for _ in range(10):
            res = [u for u in UPDATES if u["update_id"] >= off]
            if res or float(b.get("timeout", 0) or 0) == 0:
                return {"ok": True, "result": res}
            await asyncio.sleep(0.3)
        return {"ok": True, "result": []}
    if method == "sendMessage":
        mid = next(ids); SENT.append({**b, "message_id": mid}); return {"ok": True, "result": {"message_id": mid}}
    if method == "editMessageText":
        EDITS.append(b); return {"ok": True, "result": True}
    if method == "answerCallbackQuery":
        ANSWERS.append(b); return {"ok": True, "result": True}
    return {"ok": False, "description": f"unknown method {method}"}
