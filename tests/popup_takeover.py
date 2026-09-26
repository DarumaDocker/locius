"""Test case: user takeover + 'Sign in with Google'-style popup (window.open) must show in the live view,
accept the user's input, and return to the opener page when the popup closes. Needs broker on :8082 and pages on :8099."""
import sys, time, httpx
B = "http://127.0.0.1:8082"
H = {"X-Browser-Token": "bt-test"}
PAGE = "http://shop.test:8099/popup.html"
c = httpx.Client(timeout=60, trust_env=False)
fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else info); (None if cond else fails.append(name))
def st(): return c.get(B + "/state", headers=H).json()
def inp(**kw): r = c.post(B + "/user/input", json=kw, headers=H); r.raise_for_status(); time.sleep(0.6)

print("health", c.get(B + "/health").json())
snap = c.post(B + "/agent/navigate", json={"task_id": "T1", "url": PAGE}, headers=H).json()
check("agent opened login page", "Continue with Google" in snap.get("snapshot", ""), snap)
c.post(B + "/user/takeover", json={"task_id": "T1"}, headers=H)
inp(type="click", x=220, y=130)                 # "Continue with Google"
time.sleep(1.0)
s = st()
check("live view follows the Google popup", "popup_login.html" in s["url"] and s["popup"], s)
check("popup opener recorded", "popup.html" in s.get("popup_opener_url", ""), s)
shot = c.get(B + "/screenshot", headers=H)
check("screenshot of popup", shot.status_code == 200 and len(shot.content) > 1000)
inp(type="click", x=250, y=120)                 # email field
inp(type="text", text="lucas@example.com")
inp(type="click", x=160, y=200)                 # Next -> posts to opener and closes
time.sleep(1.0)
s = st()
check("back on opener after popup closes", s["url"].endswith("popup.html") and not s["popup"], s)
r = c.post(B + "/agent/snapshot", json={"task_id": "T1"}, headers=H)
check("agent blocked during takeover (423)", r.status_code == 423, r.status_code)
c.post(B + "/user/release", json={}, headers=H)
snap = c.post(B + "/agent/snapshot", json={"task_id": "T1"}, headers=H).json()
check("agent sees signed-in result", "Signed in as lucas@example.com" in snap.get("snapshot", ""), snap.get("snapshot", "")[:300])
# agent-initiated popup also followed
snap = c.post(B + "/agent/navigate", json={"task_id": "T2", "url": PAGE}, headers=H).json()
import re
ref = re.search(r"\[(e\d+)\] button \"Continue with Google", snap["snapshot"]).group(1)
snap = c.post(B + "/agent/click", json={"task_id": "T2", "ref": ref}, headers=H).json()
check("agent click follows popup", "popup_login.html" in snap.get("url", ""), snap.get("url"))
print(f"\n{len(fails)} failures", fails)
sys.exit(1 if fails else 0)
