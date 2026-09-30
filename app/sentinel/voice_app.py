"""The ONLY public surface of OMuse: Telnyx's media stream and webhooks for phone calls.

Served by Sentinel on its own port (VOICE_PORT, default 8083) behind the public "phone" entrance, so nothing else of
Sentinel/OMuse becomes reachable without login. Media streams need a one-time token that exists only while a call
you approved is being dialled; webhooks are only trusted when they carry a valid Telnyx signature.
"""
from __future__ import annotations

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse

from app.sentinel import phone

app = FastAPI(title="OMuse voice", docs_url=None, redoc_url=None, openapi_url=None)
STATE: dict = {"store": None}


@app.get("/voice/health")
async def health():
    return PlainTextResponse("omuse-voice ok")


@app.websocket("/voice/stream/{token}")
async def stream(ws: WebSocket, token: str):
    await phone.bridge(STATE["store"], ws, token)


@app.post("/voice/webhook")
async def webhook(req: Request):
    store = STATE["store"]
    body = await req.body()
    pk = (phone.keys(store) or {}).get("telnyx_public_key", "")
    if not pk:
        return JSONResponse({"ok": True, "ignored": "no public key configured"})
    if not phone.verify_webhook(pk, req.headers.get("telnyx-signature-ed25519", ""), req.headers.get("telnyx-timestamp", "0"), body):
        store.audit("sentinel", "phone.webhook_rejected", result="denied", detail={"len": len(body)})
        return JSONResponse({"ok": False}, status_code=401)
    try:
        import json
        phone.on_webhook(store, json.loads(body or b"{}"))
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    return JSONResponse({"ok": True})


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def nothing_else(path: str):
    return PlainTextResponse("not found", status_code=404)
