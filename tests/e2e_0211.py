"""0.2.11: customer-service chat widgets — a Tidio-style bot in an open shadow root and a Zendesk-style
messaging window in an iframe. The agent must see/label them, type a question (with approval) and read the reply."""
import sys
import time

import httpx

B = "http://127.0.0.1:8080"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=90, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:1500])
    if not cond:
        fails.append(name)


def run(msg, approve=True, timeout=150):
    tid = c.post(B + "/api/chat", json={"message": msg}, headers=H).json()["task_id"]
    t0, approvals = time.time(), []
    while time.time() - t0 < timeout:
        t = c.get(f"{B}/api/tasks/{tid}").json()
        if t["status"] == "WAITING_APPROVAL" and approve:
            for a in c.get(B + "/sentinel/api/approvals?status=pending").json()["approvals"]:
                approvals.append(a)
                c.post(f"{B}/sentinel/api/approvals/{a['id']}/resolve", headers=H, json={"decision": "approve"})
        elif t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return t, approvals
        time.sleep(0.5)
    return t, approvals


def results(t, name):
    return [e["data"] for e in t["events"] if e["type"] == "tool_result" and e["data"]["name"] == name]


c.put(B + "/api/settings", json={"language": "en"}, headers=H)

# ---------------------------------------------------------------- shadow-DOM chat widget (Tidio style)
t, aps = run("SUPPORTCHAT ask the site's chat bot whether there is a free plan")
check("shadow widget: task completes", t["status"] == "COMPLETED", t["status"])
vis = [e["data"] for e in t["events"] if e["type"] == "vision"]
check("browser_look labels the launcher inside the shadow root", vis and "click [e" in vis[0]["answer"], vis[:1])
clicks = results(t, "browser_click")
check("clicking it opens the chat panel (its textbox shows up in the snapshot)",
      clicks and (clicks[0].get("ok") or clicks[0].get("status") == "ok") and "Type your message" in clicks[0]["preview"], clicks[:1])
fnd = results(t, "browser_find")
check("browser_find finds the message box inside the shadow root", fnd and "Type your message" in fnd[0]["preview"], fnd[:1])
check("opening the chat is a normal click; only sending the message asks for approval",
      len(aps) == 1 and aps[0]["tool"] == "browser_type", [(a["tool"], a.get("reason")) for a in aps])
check("the bot's reply is read back", "SUPPORT DONE: Yes — the Free plan costs S$0 and includes 50 Lyra AI conversations" in (t.get("result") or ""),
      t.get("result"))

# ---------------------------------------------------------------- iframe messaging window (Zendesk style)
t, aps = run("SUPPORTIFRAME ask how long delivery takes")
check("iframe widget: task completes", t["status"] == "COMPLETED", t["status"])
vis = [e["data"] for e in t["events"] if e["type"] == "vision"]
check("browser_look labels elements inside the iframe (refs like f1e2)", vis and "click [f" in vis[0]["answer"], vis[:1])
check("typing into the iframe chat asks for approval", len(aps) == 1, aps)
check("the iframe bot's reply is read back", "IFRAME DONE: Zed: Standard delivery takes 3–5 working days." in (t.get("result") or ""),
      t.get("result"))

# ---------------------------------------------------------------- the Tidio case: no refs at all -> vision-only clicking
t, aps = run("BUBBLECHAT ask the chat bot in the corner whether there is a free plan")
check("bubble: task completes", t["status"] == "COMPLETED", t["status"])
loc = results(t, "browser_locate")
check("browser_locate finds the bubble with vision and confirms it (grid -> zoom -> red marker)",
      loc and "FOUND" in loc[0]["preview"] and "browser_click_at" in loc[0]["preview"], loc[:1])
check("it knows what is under the point: a button inside the cross-origin iframe",
      loc and "button" in loc[0]["preview"] and "opentable.test" in loc[0]["preview"], loc[:1])
cat = results(t, "browser_click_at")
check("clicking the bubble at x/y opens the chat (no approval for a plain button)",
      cat and (cat[0].get("ok") or cat[0].get("status") == "ok"), cat[:1])
check("browser_locate finds the unnamed message box once the chat is open", len(loc) > 1 and "FOUND" in loc[1]["preview"], loc[1:2])
check("typing + Enter into the chat asks for approval exactly once",
      len(aps) == 1 and aps[0]["tool"] == "browser_click_at", [(a["tool"], a.get("reason")) for a in aps])
check("approval card shows the message text and that it's inside an iframe",
      aps and "free plan" in str(aps[0].get("summary", {}).get("body", "")) and "iframe" in str(aps[0].get("summary", {})).lower(),
      aps[:1])
check("the bot's reply (inside the iframe) is read back",
      "BUBBLE DONE: Bot: Yes, we have a Free plan with 50 AI conversations a month." in (t.get("result") or ""), t.get("result"))

c.put(B + "/api/settings", json={"language": "zh"}, headers=H)
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
