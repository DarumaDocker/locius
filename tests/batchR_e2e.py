"""Batch R (browser robustness, no detection evasion) end to end, local stack:
1. a bot-wall page is detected → the task is told to switch source / request takeover (not a retry loop)
2. the blocked site is remembered as a site habit (to confirm), so next time OMuse goes another way
3. the Trust page browser metric counts the block
"""
import sys
import time

import httpx

B, LLM = "http://127.0.0.1:8080", "http://127.0.0.1:8090"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=90, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:500], flush=True)
    if not cond:
        fails.append(name)


def wait_task(tid, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = c.get(f"{B}/api/tasks/{tid}").json()
        if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return t
        time.sleep(1)
    return t


tid = c.post(B + "/api/chat", json={"message": "BLOCKSITE open the shop"}, headers=H).json()["task_id"]
t = wait_task(tid)
ev = t["events"]
check("the bot wall is detected (site_blocked event)", any(e["type"] == "site_blocked" for e in ev), [e["type"] for e in ev])
calls = c.get(LLM + "/calls_for", params={"marker": "BLOCKSITE"}).json()
seen = " ".join(x["tools"] for x in calls)
check("the task is told to switch source / request a takeover, not keep retrying",
      "SITE BLOCKED" in seen and ("browser_request_takeover" in seen or "其他" in seen or "another source" in seen), seen[:300])

mem = c.get(B + "/api/memory", headers=H).json()
site = [f for f in mem["facts"] if f["domain"] == "site" and "opentable" in f["fact"] and f["status"] == "pending"]
check("the blocked site is saved as a site habit to confirm", site,
      [f["fact"][:60] for f in mem["facts"] if f["domain"] == "site"])

m = c.get(B + "/api/metrics?days=7", headers=H).json()["metrics"]["browser"]
check("the Trust page browser metric counts the block", m["blocked"] >= 1 and any(r["blocked"] for r in m["sites"]), m)

print()
print(("%d FAILED: %s" % (len(fails), fails)) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
