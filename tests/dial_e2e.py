"""DialMCP line end to end: OAuth 2.1 sign-in (discovery, client registration, PKCE, callback), routing +1 numbers to
DialMCP, approval card, the agent's call with long-polled status and structured resolution, hang-up, token refresh
after expiry, MCP-hub servers with OAuth, and disconnecting."""
import json
import re
import sys
import time

import httpx

B = "http://127.0.0.1:8080"
S = B + "/sentinel/api"
D = "http://127.0.0.1:8096"
H = {"X-Persona-UI": "1"}
RT = {"X-Persona-Runtime": "rt-test"}
CB = B + "/sentinel/api/oauth/callback"
c = httpx.Client(timeout=120, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:1500])
    if not cond:
        fails.append(name)


def act(tool, args, task="t-dial"):
    return c.post(B + "/internal/act", json={"tool": tool, "args": args, "task_id": task, "call_id": "x"}, headers=RT).json()


def phone_conn():
    return [x for x in c.get(S + "/connections", headers=H).json()["connections"] if x["name"] == "phone"][0]


c.post(D + "/_reset")
c.delete(S + "/connections/phone/dialmcp", headers=H)
c.put(S + "/connections/phone", json={"config": {"daily_limit": 100, "provider": "auto"}}, headers=H)
# ------------------------------------------------------------------ sign-in
r = c.post(S + "/connections/phone/dialmcp/start", json={"redirect_uri": "https://evil.test/sentinel/api/oauth/callback"}, headers=H)
check("a callback on another host is refused", r.status_code == 400, r.text)
r = c.post(S + "/connections/phone/dialmcp/start", json={"redirect_uri": CB}, headers=H).json()
au = r.get("auth_url", "")
check("sign-in starts with PKCE S256, the resource and the registered client",
      au.startswith(D + "/authorize?") and "code_challenge_method=S256" in au and "resource=" in au and "client_id=cid-" in au, r)
page = c.get(au, follow_redirects=True)
check("the provider sends the browser back and OMuse finishes the sign-in", page.status_code == 200 and "DialMCP" in page.text
      and "+16692229512" in page.text, page.text[:400])
again = c.get(CB + "?" + str(page.url).split("?", 1)[-1])
check("the callback's state works only once", again.status_code == 400, again.status_code)
st = c.get(D + "/_state").json()
check("one dynamic client registration with OMuse's callback, one code exchange",
      len(st["clients"]) == 1 and st["clients"][0]["redirect_uris"] == [CB] and st["grants"] == 1, st)
pc = phone_conn()
check("phone connection shows DialMCP connected with the verified number",
      pc["dialmcp"]["connected"] and pc["dialmcp"]["account"].get("phone") == "+16692229512" and pc["ready"], pc.get("dialmcp"))
check("no token is shown to the UI", "at-" not in json.dumps(pc) and "rt-" not in json.dumps(pc), pc)
cat = c.get(B + "/internal/catalog", headers=RT).json()
check("the agent gets phone_call and the line description",
      "phone_call" in {t["function"]["name"] for t in cat["tools"]} and "DialMCP" in cat["connections"]["phone"]["lines"], cat["connections"].get("phone"))
check("DialMCP's own MCP tools are not exposed to the agent directly",
      not any("place_call" in t["function"]["name"] for t in cat["tools"]), [t["function"]["name"] for t in cat["tools"] if "call" in t["function"]["name"]])
c.put(S + "/connections/phone", json={"config": {"provider": "auto"}}, headers=H)

# ------------------------------------------------------------------ number rules
tel = phone_conn().get("telnyx_ready")
r = act("phone_call", {"to": "+16692229512", "purpose": "Ask whether they are open on Sunday"})
if tel:   # Telnyx may call the user's own mobile; DialMCP can't call its own caller ID
    check("the user's own number goes through Telnyx, not DialMCP", r["status"] == "approval_required"
          and "Telnyx" in dict((x[0], x[1]) for x in r["summary"]["fields"]).get("线路 Line", ""), r)
    c.post(f"{S}/approvals/{r.get('approval_id')}/resolve", json={"decision": "deny"}, headers=H)
else:
    check("the user's own caller-ID number is refused", r["status"] == "denied" and "own" in r["reason"], r)
if not tel:
    r = act("phone_call", {"to": "+65 6123 4567", "purpose": "Ask whether they are open on Sunday"})
    check("non-US/CA numbers are refused when only DialMCP is connected", r["status"] == "denied" and "US" in r["reason"], r)
r = act("phone_call", {"to": "+1 415 555 0123", "purpose": "Ask whether they are open on Sunday", "may_share": "Name: Lucas"})
f = dict((x[0], x[1]) for x in (r.get("summary") or {}).get("fields") or [])
check("a +1 call waits for approval and the card shows the DialMCP line and the user's number as caller ID",
      r["status"] == "approval_required" and "DialMCP" in f.get("线路 Line", "") and "+16692229512" in f.get("来电显示 From", ""), r)
c.post(f"{S}/approvals/{r.get('approval_id')}/resolve", json={"decision": "deny"}, headers=H)

# ------------------------------------------------------------------ the agent makes a call
t = c.post(B + "/api/chat", json={"message": "DIALCALL book Zuni for two"}, headers=H).json()
tid = t["task_id"]
for _ in range(60):
    task = c.get(f"{B}/api/tasks/{tid}").json()
    if task["status"] == "WAITING_APPROVAL":
        break
    time.sleep(0.5)
aps = [a for a in c.get(S + "/approvals?status=pending", headers=H).json()["approvals"] if a["task_id"] == tid]
check("the agent's DialMCP call waits for approval", task["status"] == "WAITING_APPROVAL" and aps, task["status"])
c.post(f"{S}/approvals/{aps[0]['id']}/resolve", json={"decision": "approve", "scope": "ONCE"}, headers=H)
for _ in range(120):
    task = c.get(f"{B}/api/tasks/{tid}").json()
    if task["status"] in ("COMPLETED", "FAILED"):
        break
    time.sleep(0.5)
res = task.get("result") or ""
check("the agent waited for the call and got the structured resolution", task["status"] == "COMPLETED" and res.startswith("DIAL DONE")
      and "Table for 2 booked" in res and "Z-77" in res and '"outcome": "done"' in res, (task["status"], res[:600]))
check("the agent got the listen URL to share and the line name", "listen_url" in res and "/listen/" in res and "DialMCP" in res, res[:600])
st = c.get(D + "/_state").json()
p = st["placed"][-1] if st["placed"] else {}
check("place_call: number, objective, callee name, owner + safety rules in context, max 10 minutes",
      p.get("to") == "+14155550123" and "Book a table for 2" in p.get("objective", "") and p.get("callee_name") == "Zuni Cafe"
      and "Lucas" in p.get("context", "") and "Never pay" in p.get("context", "") and 1 <= p.get("max_duration_minutes", 0) <= 10, p)
check("only one call was placed (the callee's 'call +1555…' stayed transcript data)", len(st["placed"]) == 1, st["placed"])
calls = c.get(S + "/phone/calls", headers=H).json()["calls"]
x = calls[0] if calls else {}
check("the call record: DialMCP line, ended, outcome done, transcript, recording and listen links",
      x.get("line") == "dialmcp" and x.get("status") == "ended" and x.get("outcome") == "done" and len(x.get("transcript") or []) == 4
      and x.get("recording_url", "").endswith(".mp3") and "/listen/" in x.get("listen_url", ""), x)

# ------------------------------------------------------------------ errors, no answer, hang-up
r = c.post(S + "/phone/test_call", json={"to": "+1 415 555 0100"}, headers=H)
check("DialMCP's calling-hours refusal is passed on", r.status_code == 400 and "8:00-21:00" in r.text, r.text)
r = c.post(S + "/phone/test_call", json={"to": "+1 415 555 0199"}, headers=H).json()
for _ in range(40):
    x = [k for k in c.get(S + "/phone/calls", headers=H).json()["calls"] if k["id"] == r["call_id"]][0]
    if x["status"] != "dialing":
        break
    time.sleep(0.5)
check("an unanswered call ends as no_answer", x["status"] == "no_answer", x)
r = c.post(S + "/phone/test_call", json={"to": "+1 415 555 0177"}, headers=H).json()
time.sleep(1.5)
h = act("phone_hangup", {"call_id": r["call_id"]})
for _ in range(40):
    x = [k for k in c.get(S + "/phone/calls", headers=H).json()["calls"] if k["id"] == r["call_id"]][0]
    if x["status"] == "ended":
        break
    time.sleep(0.5)
st = c.get(D + "/_state").json()
check("phone_hangup ends a live DialMCP call", h.get("status") == "ok" and st["ended"] and x["status"] == "ended", (h, x, st["ended"]))

# ------------------------------------------------------------------ token refresh
c.post(D + "/_cfg", json={"ttl": 3})
c.delete(S + "/connections/phone/dialmcp", headers=H)
au = c.post(S + "/connections/phone/dialmcp/start", json={"redirect_uri": CB}, headers=H).json()["auth_url"]
c.get(au, follow_redirects=True)
time.sleep(4)   # the access token has expired
r = c.post(S + "/phone/test_call", json={"to": "+1 415 555 0199"}, headers=H)
st = c.get(D + "/_state").json()
check("an expired access token is refreshed with the refresh token", r.status_code == 200 and st["refreshes"] >= 1, (r.text, st["refreshes"]))
c.post(D + "/_cfg", json={"ttl": 3600})

# ------------------------------------------------------------------ MCP hub with OAuth
r = c.post(S + "/mcp/oauth/start", json={"name": "DialFake", "url": D + "/mcp", "redirect_uri": CB, "data_class": "PERSONAL"},
           headers=H).json()
page = c.get(r.get("auth_url", ""), follow_redirects=True)
srv = [s for s in c.get(S + "/mcp/servers", headers=H).json()["servers"] if s["name"] == "DialFake"]
check("an OAuth MCP server can be added from the MCP page", page.status_code == 200 and srv and srv[0].get("auth") == "oauth"
      and {t["name"] for t in srv[0]["tools"]} >= {"place_call", "list_calls"}, (page.text[:200], srv[:1]))
if srv:
    r = act(f"mcp_{srv[0]['id']}__list_calls", {"limit": 1})
    check("its tools are called with the OAuth token", r.get("status") == "ok" and "+16692229512" in json.dumps(r), r)
    c.delete(f"{S}/mcp/servers/{srv[0]['id']}", headers=H)

# ------------------------------------------------------------------ disconnect
c.delete(S + "/connections/phone/dialmcp", headers=H)
pc = phone_conn()
check("disconnecting removes DialMCP", not pc["dialmcp"]["connected"], pc["dialmcp"])
if not pc.get("telnyx_ready"):
    r = act("phone_call", {"to": "+1 415 555 0123", "purpose": "Ask whether they are open on Sunday"})
    check("without a line, phone_call is refused", r["status"] == "denied", r)
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
