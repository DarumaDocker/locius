"""Phone calls end to end: setup checks, number rules, approval, dialling, the Telnyx<->Realtime bridge, DTMF,
hang-up, transcript to the agent, and the public entrance's security (one-time tokens, signed webhooks)."""
import asyncio
import base64
import json
import sys
import time

import httpx

B = "http://127.0.0.1:8080"
S = "http://127.0.0.1:8080/sentinel/api"
V = "http://127.0.0.1:8083"
F = "http://127.0.0.1:8095"
H = {"X-Persona-UI": "1"}
RT = {"X-Persona-Runtime": "rt-test"}
c = httpx.Client(timeout=120, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:1500])
    if not cond:
        fails.append(name)


c.post(F + "/reset")
# ------------------------------------------------------------------ setup
cfg = {"owner_name": "Lucas Lu", "from_number": "+1 979 347 1777", "connection_id": "conn-1", "public_url": V,
       "telnyx_api_key": "KEYtest", "openai_api_key": "sk-test", "allowed_prefixes": "+65, +1", "max_minutes": 5, "daily_limit": 2}
r = c.post(S + "/connections/phone/credential", json=cfg, headers=H).json()
check("setup: every check passes (Telnyx key, number on the app, OpenAI key, public URL reachable)",
      r.get("ready") and all(v == "ok" for v in r["checks"].values()), r)
conn = [x for x in c.get(S + "/connections", headers=H).json()["connections"] if x["name"] == "phone"][0]
check("keys are not shown back to the UI", "sk-test" not in json.dumps(conn) and "KEYtest" not in json.dumps(conn), conn)
cat = c.get(B + "/internal/catalog", headers=RT).json()
check("the agent now has phone_call / phone_call_status", {"phone_call", "phone_call_status"} <= {t["function"]["name"] for t in cat["tools"]})


def act(tool, args, task="t-phone"):
    return c.post(B + "/internal/act", json={"tool": tool, "args": args, "task_id": task, "call_id": "x"}, headers=RT).json()


r = act("phone_call", {"to": "911", "purpose": "Please help me with something important"})
check("emergency numbers are never called", r["status"] == "denied" and "emergency" in r["reason"], r)
r = act("phone_call", {"to": "+44 20 7946 0000", "purpose": "Ask about opening hours today"})
check("countries outside the allowed list are refused", r["status"] == "denied" and "+65" in r["reason"], r)
r = act("phone_call", {"to": "+1 900 555 0100", "purpose": "Ask about opening hours today"})
check("premium-rate numbers are refused", r["status"] == "denied" and "premium" in r["reason"], r)
r = act("phone_call", {"to": "+65 6123 4567", "purpose": "hi"})
check("a call needs a real purpose", r["status"] == "denied" and "purpose" in r["reason"], r)
r = act("phone_call", {"to": "+65 6123 4567", "purpose": "Ask whether they are open on Sunday", "may_share": "Name: Lucas"})
check("a valid call always waits for approval, showing number, caller ID and what may be shared",
      r["status"] == "approval_required" and ["拨打 To", "+6561234567"] in r["summary"]["fields"]
      and any(f[0].startswith("可以告诉对方") and "Lucas" in f[1] for f in r["summary"]["fields"]), r)
c.post(f"{S}/approvals/{r['approval_id']}/resolve", json={"decision": "deny"}, headers=H)

# ------------------------------------------------------------------ the agent makes a call
t = c.post(B + "/api/chat", json={"message": "PHONECALL book Aurora for two"}, headers=H).json()
tid = t["task_id"]
for _ in range(60):
    task = c.get(f"{B}/api/tasks/{tid}").json()
    if task["status"] == "WAITING_APPROVAL":
        break
    time.sleep(0.5)
check("the agent's call waits for approval", task["status"] == "WAITING_APPROVAL", task["status"])
aps = [a for a in c.get(S + "/approvals?status=pending", headers=H).json()["approvals"] if a["task_id"] == tid]
check("approval card shows the brief as editable text", aps and aps[0]["summary"]["body"].startswith("Book a table")
      and "purpose" in aps[0]["summary"]["editable"], aps[:1])
c.post(f"{S}/approvals/{aps[0]['id']}/resolve", json={"decision": "approve", "scope": "ONCE"}, headers=H)
for _ in range(120):
    task = c.get(f"{B}/api/tasks/{tid}").json()
    if task["status"] in ("COMPLETED", "FAILED"):
        break
    time.sleep(0.5)
check("task completes with the call's outcome", task["status"] == "COMPLETED" and "CALL DONE" in (task.get("result") or ""),
      (task["status"], task.get("result")))
st = c.get(F + "/state").json()
d = st["dials"][-1] if st["dials"] else {}
check("Telnyx dial: from/to, bidirectional PCMU stream to a one-time /voice/stream URL, hard time limit",
      d.get("to") == "+6561234567" and d.get("from") == "+19793471777" and d.get("connection_id") == "conn-1"
      and "/voice/stream/" in d.get("stream_url", "") and d.get("stream_bidirectional_mode") == "rtp"
      and d.get("stream_bidirectional_codec") == "PCMU" and d.get("time_limit_secs") == 5 * 60 + 15, d)
sess = st["session"] or {}
check("Realtime session: u-law in/out, the brief, the owner's name, AI disclosure, only end_call/press_keys tools",
      sess.get("audio", {}).get("input", {}).get("format", {}).get("type") == "audio/pcmu"
      and sess.get("audio", {}).get("output", {}).get("format", {}).get("type") == "audio/pcmu"
      and "Book a table for 2" in sess.get("instructions", "") and "Lucas Lu" in sess.get("instructions", "")
      and "AI assistant" in sess.get("instructions", "")
      and sorted(x["name"] for x in sess.get("tools", [])) == ["end_call", "press_keys"], sess)
check("caller audio reached the model", st["appends"] >= 5, st["appends"])
check("the model's voice was played into the call", st["played"] >= 3, st["played"])
check("barge-in clears queued audio when they start talking", st["clears"] >= 1, st["clears"])
check("menu navigation pressed 1 through Telnyx", st["dtmf"] == ["1"] and st["tool_outputs"] and "pressed 1" in st["tool_outputs"][0], st)
check("OMuse hung up after the goodbye", st["hangups"], st)
res = task.get("result") or ""
check("the agent got the transcript and summary (reference A12)", "A12" in res and "Aurora Restaurant" in res and "Them:" in res, res)
calls = c.get(S + "/phone/calls", headers=H).json()["calls"]
check("the call is listed as ended with outcome done", calls and calls[0]["status"] == "ended" and calls[0]["outcome"] == "done", calls[:1])
check("the other side's 'IGNORE YOUR TASK' only shows up as transcript data (no second call was dialled)",
      len(st["dials"]) == 1, st["dials"])
ev = [e for e in task["events"] if e["type"] == "tool_result" and e["data"]["name"] == "phone_call_status"]
check("call status reaches the agent as untrusted content", ev and "untrusted_content" in ev[-1]["data"]["preview"], ev[-1:])


# ------------------------------------------------------------------ the public entrance
async def ws_try(path):
    import websockets
    try:
        async with websockets.connect("ws://127.0.0.1:8083" + path, open_timeout=5) as w:
            await w.send(json.dumps({"event": "connected"}))
            await asyncio.wait_for(w.recv(), timeout=3)
            return "open"
    except Exception as e:
        return type(e).__name__ + ":" + str(getattr(e, "code", "") or getattr(getattr(e, "rcvd", None), "code", ""))


tok = d["stream_url"].rsplit("/", 1)[-1]
check("an unknown stream token is refused", "open" != asyncio.run(ws_try("/voice/stream/not-a-token")))
check("a used stream token can't be replayed", "open" != asyncio.run(ws_try(f"/voice/stream/{tok}")))
check("nothing but /voice/* is served on the public port",
      c.get(V + "/sentinel/api/connections").status_code == 404 and c.get(V + "/api/tasks").status_code == 404
      and c.get(V + "/voice/health").text.startswith("omuse-voice"))
check("webhooks are ignored until a Telnyx public key is set",
      c.post(V + "/voice/webhook", json={"data": {"event_type": "call.hangup"}}).json().get("ignored"))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
sk = Ed25519PrivateKey.generate()
pk = base64.b64encode(sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
c.post(S + "/connections/phone/credential", json={"telnyx_public_key": pk, "telnyx_api_key": "", "openai_api_key": ""}, headers=H)
body = json.dumps({"data": {"event_type": "call.answered", "payload": {"client_state": ""}}}).encode()
ts = str(int(time.time()))
good = base64.b64encode(sk.sign(f"{ts}|".encode() + body)).decode()
bad = base64.b64encode(Ed25519PrivateKey.generate().sign(f"{ts}|".encode() + body)).decode()
check("a forged webhook signature is rejected", c.post(V + "/voice/webhook", content=body, headers={
    "telnyx-signature-ed25519": bad, "telnyx-timestamp": ts, "content-type": "application/json"}).status_code == 401)
check("a correctly signed webhook is accepted", c.post(V + "/voice/webhook", content=body, headers={
    "telnyx-signature-ed25519": good, "telnyx-timestamp": ts, "content-type": "application/json"}).status_code == 200)
old = str(int(time.time()) - 3600)
old_sig = base64.b64encode(sk.sign(f"{old}|".encode() + body)).decode()
check("an old (replayed) webhook is rejected", c.post(V + "/voice/webhook", content=body, headers={
    "telnyx-signature-ed25519": old_sig, "telnyx-timestamp": old, "content-type": "application/json"}).status_code == 401)

# ------------------------------------------------------------------ no answer + daily limit
r = c.post(S + "/phone/test_call", json={"to": "+65 9900 0000"}, headers=H).json()
check("a test call from the Connections page dials without an approval card", r.get("status") == "dialing", r)
r2 = act("phone_call", {"to": "+65 6123 4567", "purpose": "Ask whether they are open on Sunday"})
check("daily limit: the 3rd call of the day is refused", r2["status"] == "denied" and "limit" in r2["reason"], r2)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
