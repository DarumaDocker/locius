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
c.put(B + "/api/settings", json={"max_steps": 6}, headers=H).raise_for_status()
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
check("identical re-opens blocked", len(blocked) >= 3 and len(res) - len(blocked) <= 3, [r["preview"][:80] for r in res])
ns = c.get(B + "/api/notifications", headers=H).json()
ns = ns.get("notifications", ns) if isinstance(ns, dict) else ns
check("user told the scheduled run did not finish", any("LOOP 早报" in n.get("title", "") and "没有做完" in n.get("title", "") for n in ns),
      [n.get("title") for n in ns][:5])
sc = [x for x in c.get(B + "/api/schedules").json()["schedules"] if x["id"] == sid][0]
check("schedule card shows last run failed", sc["last_status"] == "FAILED", sc["last_status"])
c.delete(f"{B}/api/schedules/{sid}", headers=H)
c.put(B + "/api/settings", json={"max_steps": s0.get("max_steps", 30)}, headers=H)
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
