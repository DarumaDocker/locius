"""Docker image, end to end with a real model: sign in through the gate, then have the agent open a web page, take a
screenshot (browser_look) and describe it. Needs a running container with a vision-capable model:

  OMUSE_URL=http://127.0.0.1:8080 OMUSE_PASSWORD=... python3 tests/docker_browser_e2e.py [screenshot.jpg]

The model endpoint and its key are the container's own (OMUSE_MODEL_URL / OMUSE_MODEL / OMUSE_MODEL_API_KEY)."""
import os
import sys
import time

import httpx

B = os.environ.get("OMUSE_URL", "http://127.0.0.1:8080").rstrip("/")
AUTH = (os.environ.get("OMUSE_USER", "omuse"), os.environ["OMUSE_PASSWORD"])
PAGE = os.environ.get("E2E_PAGE", "https://example.com/")
EXPECT = os.environ.get("E2E_EXPECT", "Example Domain")
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=120, trust_env=False, auth=AUTH)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:1200])
    if not cond:
        fails.append(name)


check("no password, no entry", httpx.get(B + "/", trust_env=False).status_code == 401)
check("UI loads after sign-in", c.get(B + "/").status_code == 200)
tm = c.post(B + "/api/settings/test-model", headers=H).json()
check("model endpoint answers (API key accepted)", tm.get("ok") is True, tm)

t0 = time.time()
r = c.post(B + "/api/chat", headers=H, json={"message": (
    f"Open {PAGE} with browser_navigate. Then call browser_look to take a screenshot of the page and tell me what "
    "it shows: the heading, the text and any links. Answer in English.")}).json()
t = {}
while time.time() - t0 < 300:
    t = c.get(f"{B}/api/tasks/{r['task_id']}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED") or t.get("waiting"):
        break
    time.sleep(1)
calls = [e["data"].get("name") for e in t.get("events", []) if e["type"] == "tool_call"]
vision = [e["data"] for e in t.get("events", []) if e["type"] == "vision"]
out = t.get("result") or ""
check("task completes without approvals", t.get("status") == "COMPLETED", (t.get("status"), t.get("error"), t.get("waiting")))
check("agent opened the page", "browser_navigate" in calls, calls)
check("agent took a screenshot (browser_look)", "browser_look" in calls, calls)
check("vision model described the screenshot", bool(vision) and EXPECT.lower() in vision[-1].get("answer", "").lower(), vision)
check("final answer describes the page", EXPECT.lower() in out.lower(), out)

shot = c.get(f"{B}/sentinel/api/browser/screenshot", params={"task_id": r["task_id"]})
check("live-view screenshot is a JPEG", shot.status_code == 200 and shot.content[:3] == b"\xff\xd8\xff", (shot.status_code, len(shot.content)))
if len(sys.argv) > 1 and shot.status_code == 200:
    open(sys.argv[1], "wb").write(shot.content)
st = c.get(B + "/sentinel/api/browser/state").json()
check("browser is on the page, headed", PAGE.split("//")[1].split("/")[0] in st.get("url", "") and st.get("headless") is False, st)

print(f"\n--- agent's description ({time.time() - t0:.0f}s, tools: {calls}) ---\n{out}\n")
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
