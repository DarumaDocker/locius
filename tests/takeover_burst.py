"""Several tasks wait for the user to take over the browser (e.g. ZipAir blocking automated browsing). Handing the
browser back for ONE of them must not wake the others: each woken task asked for a takeover again at once, which showed
up as a burst of "take over the browser" pop-ups. Also checks that the other tasks' requests stay visible."""
import json
import sys
import threading
import time

import httpx

B = "http://127.0.0.1:8080"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=60, trust_env=False)
fails = []
events = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:800])
    if not cond:
        fails.append(name)


def listen():
    with httpx.Client(timeout=None, trust_env=False) as s, s.stream("GET", B + "/api/stream") as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:]))
                except ValueError:
                    pass


threading.Thread(target=listen, daemon=True).start()
time.sleep(1)


def chat(msg):
    r = c.post(B + "/api/chat", json={"message": msg}, headers=H)
    r.raise_for_status()
    return r.json()["task_id"]


def task(tid):
    return c.get(f"{B}/api/tasks/{tid}").json()


def wait(tid, states, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = task(tid)
        if t["status"] in states:
            return t
        time.sleep(0.4)
    return t


ids = [chat(f"TAKEOVERLOOP zipair {k}") for k in "abc"]
for tid in ids:
    wait(tid, {"WAITING_EXTERNAL", "FAILED", "COMPLETED"})
check("three tasks wait for a takeover", all(task(t)["status"] == "WAITING_EXTERNAL" for t in ids), [task(t)["status"] for t in ids])
st = c.get(B + "/sentinel/api/browser/state", headers=H).json()
check("the browser lists all three open requests", {r["task_id"] for r in st.get("requests", [])} == set(ids), st.get("requests"))

before = len([e for e in events if e.get("kind") == "takeover_requested"])
c.post(B + "/sentinel/api/browser/takeover", json={"task_id": ids[0]}, headers=H)
c.post(B + "/sentinel/api/browser/release", json={}, headers=H)
time.sleep(6)
burst = [e for e in events if e.get("kind") == "takeover_requested"][before:]
check("hand-back wakes only the task that was taken over (no burst of takeover requests)",
      [e["task_id"] for e in burst] == [ids[0]], [e["task_id"] for e in burst])
check("the other two keep waiting", all(task(t)["status"] == "WAITING_EXTERNAL" for t in ids[1:]), [task(t)["status"] for t in ids])
st = c.get(B + "/sentinel/api/browser/state", headers=H).json()
check("their requests are still shown in the browser view", {r["task_id"] for r in st.get("requests", [])} >= set(ids[1:]),
      st.get("requests"))

# untargeted takeover (no task picked) answers the newest request only
c.post(B + "/sentinel/api/browser/takeover", json={}, headers=H)
taken = c.get(B + "/sentinel/api/browser/state", headers=H).json().get("takeover_task")
c.post(B + "/sentinel/api/browser/release", json={}, headers=H)
time.sleep(5)
woke = [t for t in ids if task(t)["status"] != "WAITING_EXTERNAL"]
check("an untargeted takeover resumes just the newest requester", woke == [taken], (woke, taken))

# clean up: hand the browser back for each remaining task until all are finished
for _ in range(6):
    open_ = [t for t in ids if task(t)["status"] == "WAITING_EXTERNAL"]
    if not open_:
        break
    c.post(B + "/sentinel/api/browser/takeover", json={"task_id": open_[0]}, headers=H)
    c.post(B + "/sentinel/api/browser/release", json={}, headers=H)
    wait(open_[0], {"COMPLETED", "FAILED", "WAITING_EXTERNAL"}, 10)
    time.sleep(2)
check("every task finishes after its own hand-backs", all(task(t)["status"] == "COMPLETED" for t in ids), [task(t)["status"] for t in ids])

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
