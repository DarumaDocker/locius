"""Roadmap batch 3 (ledger) end to end, local stack (tests/run_local.sh):
1. an approved purchase becomes a ledger entry; the order number from the confirmation page moves it to "placed"
2. "cancel that order": orders_list finds it, and the cancel approval names the ledger order
3. a cancel on an order that is not in the ledger is flagged in the approval
4. the Trust page shows the ledger
"""
import asyncio
import sys
import time

import httpx

B, LLM = "http://127.0.0.1:8080", "http://127.0.0.1:8090"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=90, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:700], flush=True)
    if not cond:
        fails.append(name)


def task(tid):
    return c.get(f"{B}/api/tasks/{tid}").json()


def wait(pred, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(1)
    return None


def pending(tid):
    return [a for a in c.get(B + "/sentinel/api/approvals?status=pending").json()["approvals"] if a["task_id"] == tid]


def ledger():
    return c.get(B + "/sentinel/api/ledger", headers=H).json()["orders"]


def resolve(aid, decision):
    return c.post(f"{B}/sentinel/api/approvals/{aid}/resolve", json={"decision": decision}, headers=H).json()


# ---- 1. purchase → ledger
tid = c.post(B + "/api/chat", json={"message": "BUYTEST buy the socks on shop.test"}, headers=H).json()["task_id"]
ap = wait(lambda: pending(tid))
check("one purchase card to approve", ap and ap[0]["tool"] == "purchase_confirm", ap)
resolve(ap[0]["id"], "approve")
t = wait(lambda: (lambda x: x if x["status"] in ("COMPLETED", "FAILED") else None)(task(tid)), 120) or task(tid)
check("purchase completed with no second approval", t["status"] == "COMPLETED" and not pending(tid), t["status"])
L = [o for o in ledger() if o["task_id"] == tid]
check("ledger entry placed with the confirmation page's order number",
      L and L[0]["status"] == "placed" and L[0]["order_number"] == "OM-77821" and L[0]["merchant"] == "shop.test", L)
check("…with amount, delivery and who approved it",
      L and abs(L[0]["total"] - 4.9) < 0.01 and L[0]["currency"] == "SGD" and L[0]["approval_id"] == ap[0]["id"]
      and L[0]["delivery"] == "Home delivery", L)
check("timeline shows the ledger update", any(e["type"] == "ledger" for e in t["events"]))

# ---- 2. cancel through the ledger
tid2 = c.post(B + "/api/chat", json={"message": "CANCELTEST cancel my socks order"}, headers=H).json()["task_id"]
ap2 = wait(lambda: pending(tid2))
calls = c.get(LLM + "/calls_for", params={"marker": "CANCELTEST cancel my socks"}).json()
seen = " ".join(x["tools"] for x in calls)
check("orders_list gives the agent the exact order", "OM-77821" in seen and "shop.test" in seen, seen[:400])
summ = (ap2 or [{}])[0].get("summary") or {}
fields = " ".join(" ".join(map(str, f)) for f in summ.get("fields") or [])
check("cancel approval names the ledger order", ap2 and "账本订单" in fields and "OM-77821" in fields, summ)
if ap2:
    resolve(ap2[0]["id"], "approve")
o = wait(lambda: next((x for x in ledger() if x["order_number"] == "OM-77821" and x["status"] == "cancel_requested"), None), 30)
check("approved cancel is recorded on that order", o and any(h.get("by") == "user" for h in o["history"]), o)

# ---- 3. an order that is not in the ledger
tid3 = c.post(B + "/api/chat", json={"message": "CANCELTEST OTHER cancel the phone case order"}, headers=H).json()["task_id"]
ap3 = wait(lambda: pending(tid3))
summ3 = (ap3 or [{}])[0].get("summary") or {}
check("cancel on an order not in the ledger is flagged", ap3 and "不在 OMuse 的交易账本里" in str(summ3.get("warning")), summ3)
if ap3:
    resolve(ap3[0]["id"], "deny")

# ---- backfill is harmless when everything is in already
r = c.post(B + "/api/ledger/backfill", json={}, headers=H).json()
check("backfill creates nothing twice", r.get("created") == [], r)


# ---- 5. fewer approvals: 3 approved Slack posts to #general → a suggestion; accepted → the 4th asks nothing
RT = {"X-Persona-Runtime": "rt-test"}
c.post("http://127.0.0.1:8094/_reset")
c.post(B + "/sentinel/api/connections/slack/credential", headers=H, json={"token": "xoxb-test-token-123456"})


def act(tool, args, tsk):
    return c.post(B + "/internal/act", headers=RT, json={"task_id": tsk, "call_id": "c" + str(time.time()), "tool": tool, "args": args}).json()


for i in range(3):
    res = act("slack_send_message", {"channel": "#general", "text": f"note {i}"}, f"slack-t{i}")
    if res.get("status") == "approval_required":
        resolve(res["approval_id"], "approve")
sg = c.get(B + "/sentinel/api/grant_suggestions", headers=H).json()["suggestions"]
check("after 3 approvals of the same action, OMuse suggests (does not switch on) a standing approval",
      len(sg) == 1 and sg[0]["tool"] == "slack_send_message" and sg[0]["count"] == 3, sg)
check("purchases and cancels are never suggested", not any(x["tool"] in ("purchase_confirm", "browser_click") for x in sg), sg)
res = act("slack_send_message", {"channel": "#general", "text": "before accepting"}, "slack-t3")
check("…and until accepted it still asks", res.get("status") == "approval_required", res)
if res.get("approval_id"):
    resolve(res["approval_id"], "deny")
if sg:
    c.post(B + "/sentinel/api/grant_suggestions", json={"key": sg[0]["key"], "accept": True, "days": 30}, headers=H)
res = act("slack_send_message", {"channel": "#general", "text": "after accepting"}, "slack-t4")
check("accepted: the same action now goes through without an approval card", res.get("status") == "ok", res)
res = act("slack_send_message", {"channel": "#random", "text": "other channel"}, "slack-t5")
check("…only for that destination (another channel still asks)", res.get("status") == "approval_required", res)

# ---- 4. Trust page
async def ui():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await (await b.new_context(viewport={"width": 1400, "height": 1200})).new_page()
        await pg.goto(B + "/#trust")
        await pg.wait_for_selector("text=OM-77821", timeout=20000)
        await pg.screenshot(path="/tmp/claude-0/sc/ledger_page.png", full_page=True)
        txt = await pg.locator(".card", has_text="Ledger").inner_text()
        await b.close()
        return txt
try:
    txt = asyncio.run(ui())
    check("Trust page lists the order with its status", "OM-77821" in txt and "shop.test" in txt, txt[:300])
except Exception as e:
    check("Trust page lists the order with its status", False, repr(e))

print()
print(("%d FAILED: %s" % (len(fails), fails)) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
