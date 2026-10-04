"""Several chats wait for a browser takeover at once. Each task has its own tab (named after its request); the Take over
button, the screenshot and the URL belong to the selected tab, so the user takes over the chat they meant — not whichever
agent touched the browser last (Lucas, 2026-10-04)."""
import sys
import time

RUN = str(int(time.time()) % 10000)

import httpx
from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8080"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=60, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:800])
    if not cond:
        fails.append(name)


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


def state():
    return c.get(B + "/sentinel/api/browser/state", headers=H).json()


a = c.post(B + "/api/chat", json={"message": "TAKEOVERLOOP zipair A" + RUN}, headers=H).json()["task_id"]
b = c.post(B + "/api/chat", json={"message": "TAKEOVERLOOP zipair B" + RUN}, headers=H).json()["task_id"]
for t in (a, b):
    wait(t, {"WAITING_EXTERNAL", "FAILED", "COMPLETED"})
check("both chats wait for a takeover", task(a)["status"] == task(b)["status"] == "WAITING_EXTERNAL", (task(a)["status"], task(b)["status"]))
c.post(B + "/sentinel/api/browser/view", json={"task_id": a}, headers=H)   # the agents last showed chat A's page

with sync_playwright() as p:
    br = p.chromium.launch()
    pg = br.new_page(viewport={"width": 1280, "height": 900})
    pg.goto(B + "/#browser/" + b)
    pg.wait_for_function("document.querySelectorAll('.btab').length >= 2 && document.querySelector('.btab.on')", timeout=20000)
    pg.wait_for_timeout(2500)   # task names load
    tabs = pg.eval_on_selector_all(".btab", "els => els.map(e => [e.textContent, e.title, e.classList.contains('on')])")
    check("one tab per waiting chat, named after its request", len(tabs) >= 2 and any("zipair B" + RUN in x[0] for x in tabs)
          and any("zipair A" + RUN in x[0] for x in tabs), tabs)
    on = [x for x in tabs if x[2]]
    check("the tab of the chat the user came from is selected (not the one the agents showed last)", on and b in on[0][1], on)
    btn = pg.locator("button.take")
    check("the Take over button names that chat", "zipair B" + RUN in btn.inner_text(), btn.inner_text())
    rows = pg.eval_on_selector_all(".breq", "els => els.map(e => [e.textContent, !!e.querySelector('button')])")
    mine = [r for r in rows if "zipair A" + RUN in r[0] or "zipair B" + RUN in r[0]]
    check("every waiting chat is listed with its own Take over button", len(mine) == 2 and all(r[1] for r in mine), rows)
    btn.click()
    pg.wait_for_timeout(1500)
    st = state()
    check("Take over takes over the selected chat", st.get("mode") == "user" and st.get("takeover_task") == b, st)
    r = c.post(B + "/sentinel/api/browser/takeover", json={"task_id": a}, headers=H)
    check("taking over a second chat while one is taken over is refused", r.status_code >= 400 and "hand back" in r.text, (r.status_code, r.text[:200]))
    dis = pg.eval_on_selector_all(".breq button", "els => els.map(e => [e.textContent, e.disabled])")
    check("the other chats' buttons are disabled until hand-back", sum(1 for t, d in dis if d) >= 1, dis)
    pg.locator("button.release").click()
    pg.wait_for_timeout(1500)
    check("hand-back resumes chat B only", state().get("mode") == "agent" and task(a)["status"] == "WAITING_EXTERNAL", (state().get("mode"), task(a)["status"]))
    pg.wait_for_function("[...document.querySelectorAll('.breq')].some(e => e.textContent.includes('zipair A" + RUN + "'))", timeout=15000)
    pg.locator(".breq", has_text="zipair A" + RUN).locator("button").click()
    pg.wait_for_timeout(1500)
    check("the row button takes over exactly that chat", state().get("takeover_task") == a, state())
    c.post(B + "/sentinel/api/browser/release", json={}, headers=H)
    br.close()

for _ in range(6):
    open_ = [t for t in (a, b) if task(t)["status"] == "WAITING_EXTERNAL"]
    if not open_:
        break
    c.post(B + "/sentinel/api/browser/takeover", json={"task_id": open_[0]}, headers=H)
    c.post(B + "/sentinel/api/browser/release", json={}, headers=H)
    time.sleep(2)
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
