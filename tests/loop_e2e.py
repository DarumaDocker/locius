"""A scheduled run whose model loops on one RSS feed: feed is readable, loop is blocked, step limit -> FAILED + notice."""
import sys, time, httpx

B = "http://127.0.0.1:8080"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=60, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else info)
    if not cond:
        fails.append(name)


s0 = c.get(B + "/api/settings", headers=H).json()["settings"]
c.put(B + "/api/settings", json={"max_steps": 4}, headers=H).raise_for_status()
sch = c.post(B + "/api/schedules", json={"name": "LOOP 早报", "goal": "LOOPFEED read the robotics feed and email it",
                                        "kind": "cron", "spec": "0 6 * * *", "tz": "Asia/Singapore"}, headers=H).json()
sid = (sch.get("schedule") or sch)["id"]
tid = c.post(f"{B}/api/schedules/{sid}/run", headers=H).json()["task_id"]
t0 = time.time()
while time.time() - t0 < 90:
    t = c.get(f"{B}/api/tasks/{tid}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(0.5)
res = [e["data"] for e in t["events"] if e["type"] == "tool_result"]
check("step limit -> FAILED, not COMPLETED", t["status"] == "FAILED" and "步数上限" in (t.get("error") or ""), (t["status"], t.get("error")))
check("summary kept as result", "LOOP SUMMARY" in (t.get("result") or ""), t.get("result"))
check("feed shown as item list", res and "RSS/Atom feed" in res[0]["preview"] and "Robot story 0" in res[0]["preview"], res[:1])
blocked = [r for r in res if "重复调用已拦截" in r["preview"]]
check("identical re-opens blocked", len(blocked) >= 2 and len(res) - len(blocked) <= 3, [r["preview"][:80] for r in res])
ns = c.get(B + "/api/notifications", headers=H).json()
ns = ns.get("notifications", ns) if isinstance(ns, dict) else ns
check("user told the scheduled run did not finish", any("LOOP 早报" in n.get("title", "") and "没有做完" in n.get("title", "") for n in ns),
      [n.get("title") for n in ns][:5])
sc = [x for x in c.get(B + "/api/schedules").json()["schedules"] if x["id"] == sid][0]
check("schedule card shows last run failed", sc["last_status"] == "FAILED", sc["last_status"])

# same run with the app in English: the failure reason and the notice are English (no Chinese)
c.put(B + "/api/settings", json={"language": "en"}, headers=H).raise_for_status()
tid = c.post(f"{B}/api/schedules/{sid}/run", headers=H).json()["task_id"]
t0 = time.time()
while time.time() - t0 < 90:
    t = c.get(f"{B}/api/tasks/{tid}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(0.5)
cjk = lambda x: any("\u4e00" <= ch <= "\u9fff" for ch in x or "")
check("EN: failure reason in English", t["status"] == "FAILED" and "step limit" in (t.get("error") or "") and not cjk(t.get("error")),
      t.get("error"))
ns = c.get(B + "/api/notifications", headers=H).json()["notifications"]
mine = [n for n in ns if "LOOP 早报" in n.get("title", "") and "step limit" in n.get("title", "")]
check("EN: notice in English", mine and not cjk(mine[0]["title"].replace("LOOP 早报", "")) and not cjk(mine[0]["body"].split("\n")[-1]),
      [(n.get("title"), n.get("body", "")[-120:]) for n in ns[:3]])
c.put(B + "/api/settings", json={"language": s0.get("language", "zh")}, headers=H)

# 0.2.29: too many web calls → told once to wrap up and answer with what it has
c.put(B + "/api/settings", json={"max_steps": 40}, headers=H)
r = c.post(B + "/api/chat", json={"message": "WEBLOOP top 3 Anker power banks"}, headers=H).json()
t0 = time.time()
while time.time() - t0 < 120:
    t = c.get(f"{B}/api/tasks/{r['task_id']}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(0.5)
check("web budget nudge ends a search loop", t["status"] == "COMPLETED" and (t.get("result") or "").startswith("WRAPPED after 18"),
      (t["status"], t.get("result")))
# 0.2.37: figures that no tool produced are sent back for recomputation once
r = c.post(B + "/api/chat", json={"message": "NUMCHECK coffee savings"}, headers=H).json()
t0 = time.time()
while time.time() - t0 < 60:
    t = c.get(f"{B}/api/tasks/{r['task_id']}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(0.5)
ev = [e for e in t.get("events", []) if e["type"] == "number_check"]
check("made-up figures sent back once", t["status"] == "COMPLETED" and (t.get("result") or "").startswith("FIXED")
      and ev and ev[0]["data"]["unsupported"] == ["17,016.64"], (t["status"], t.get("result"), ev))
r = c.post(B + "/api/chat", json={"message": "CTXTEST read three big files"}, headers=H).json()
t0 = time.time()
while time.time() - t0 < 60:
    t = c.get(f"{B}/api/tasks/{r['task_id']}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(0.5)
ev = [e for e in t.get("events", []) if e["type"] == "context_shrunk"]
# (a second run in the same runtime process already knows the limit and shrinks before sending: no event then)
check("context overflow shrinks and retries", t["status"] == "COMPLETED" and (t.get("result") or "").startswith("CTX OK"),
      (t["status"], t.get("result"), t.get("error"), ev))
c.delete(f"{B}/api/schedules/{sid}", headers=H)
c.put(B + "/api/settings", json={"max_steps": s0.get("max_steps", 30)}, headers=H)
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
