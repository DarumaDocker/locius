"""0.2.23: several tasks using the browser at once. Each task has its own page AND its own lock (a slow page load in one
task no longer stalls the others), a takeover pauses only the task being taken over, the live view does not flip
between two busy tasks, user input goes to the taken-over page, and finished tasks' pages are recycled."""
import sys
import threading
import time

import httpx

BR = "http://127.0.0.1:8082"
HB = {"X-Browser-Token": "bt-test"}
c = httpx.Client(timeout=90, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:800])
    if not cond:
        fails.append(name)


def act(task, action, **kw):
    r = c.post(f"{BR}/agent/{action}", json={"task_id": task, **kw}, headers=HB)
    return r


SLOW = "http://shop.test:8094/slow?s=8&name=SlowA"
FAST = "http://shop.test:8094/slow?s=0&name=FastB"
c.post(f"{BR}/user/view", json={"task_id": "", "pin": False}, headers=HB)   # no view pinned by an earlier run
for x in c.get(f"{BR}/state", headers=HB).json().get("tasks", []):   # pages left by earlier tests: those tasks are over
    act(x["task_id"], "release")
act("pA", "navigate", url=FAST)          # both tasks have a page already
act("pB", "navigate", url=FAST)
c.post(f"{BR}/user/release", json={}, headers=HB)

# 1. a slow load in task A does not hold task B
done = {}
th = threading.Thread(target=lambda: done.setdefault("a", act("pA", "navigate", url=SLOW)))
th.start()
time.sleep(1)
t0 = time.time()
rb = act("pB", "navigate", url=FAST)
fast_took = time.time() - t0
check("task B navigates while task A's page is still loading", rb.status_code == 200 and fast_took < 5, (rb.status_code, fast_took))
st = c.get(f"{BR}/state", headers=HB).json()
check("live view stays on the busy task A instead of jumping to B", st["view_task"] == "pA", st["view_task"])
th.join()
check("task A's slow page loaded", done["a"].status_code == 200 and "SlowA" in done["a"].text, done["a"].text[:200])

# 2. a takeover of A pauses A only
r = c.post(f"{BR}/user/takeover", json={"task_id": "pA"}, headers=HB)
check("takeover of task A", r.status_code == 200 and r.json()["takeover_task"] == "pA", r.text[:200])
check("task A is paused during its takeover", act("pA", "snapshot").status_code == 423)
rb = act("pB", "navigate", url=FAST)
check("task B keeps working during A's takeover", rb.status_code == 200, rb.text[:200])
st = c.get(f"{BR}/state", headers=HB).json()
check("the live view stays on the taken-over page", st["view_task"] == "pA" and "SlowA" in st["url"], (st["view_task"], st["url"]))
c.post(f"{BR}/user/input", json={"type": "click", "x": 10, "y": 10}, headers=HB)
st = c.get(f"{BR}/state", headers=HB).json()
check("user input went to task A's page, not to B's", "SlowA" in st["url"], st["url"])
c.post(f"{BR}/user/release", json={}, headers=HB)
check("task A resumes after release", act("pA", "snapshot").status_code == 200)

# 3. the user pins the view
c.post(f"{BR}/user/view", json={"task_id": "pB"}, headers=HB)
act("pA", "snapshot")
st = c.get(f"{BR}/state", headers=HB).json()
check("a view the user picked is not moved by other tasks", st["view_task"] == "pB", st["view_task"])

# 4. finished tasks are recycled first, running ones kept
for i in range(9):
    act(f"pX{i}", "navigate", url=FAST)
    act(f"pX{i}", "release")
act("pC", "navigate", url=FAST)
st = c.get(f"{BR}/state", headers=HB).json()
ids = {x["task_id"] for x in st["tasks"]}
check("pages of finished tasks are recycled when there are many", len(ids) <= 9 and {"pA", "pB", "pC"} <= ids, sorted(ids))
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
