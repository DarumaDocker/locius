"""0.2.17: vault fills approved one by one (value never reaches the agent, the UI, the audit or vision),
write-time memory sorting (profile → pending, one-off → recent) and the memory tidy preview → apply flow."""
import json
import sys
import time

import httpx

B = "http://127.0.0.1:8080"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=60, trust_env=False)
fails = []
SECRET = "KF8812345678"


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:800])
    if not cond:
        fails.append(name)


def chat(msg):
    r = c.post(B + "/api/chat", json={"message": msg}, headers=H)
    r.raise_for_status()
    return r.json()["task_id"]


def wait(tid, states, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = c.get(f"{B}/api/tasks/{tid}").json()
        if t["status"] in states:
            return t
        time.sleep(0.5)
    return t


# ------------------------------------------------------------------ vault
for it in c.get(B + "/sentinel/api/vault", headers=H).json()["items"]:
    c.delete(B + "/sentinel/api/vault/" + it["id"], headers=H)
r = c.post(B + "/sentinel/api/vault", headers=H, json={"kind": "membership", "label": "KrisFlyer", "domains": "shop.test",
                                                        "values": {"number": SECRET, "name": "LU LIANG"}})
check("vault item saved", r.status_code == 200, r.text)
lst = c.get(B + "/sentinel/api/vault", headers=H).text
check("vault list never returns values", SECRET not in lst and "•••• 5678" in lst, lst[:300])
check("vault writes need the UI header", c.post(B + "/sentinel/api/vault", json={"kind": "other", "label": "x", "values": {"value": "12345"}}).status_code == 403)

tid = chat("VAULTFILL fill my membership number")
t = wait(tid, {"WAITING_APPROVAL", "COMPLETED", "FAILED"})
check("vault fill waits for approval", t["status"] == "WAITING_APPROVAL", t.get("error") or t["events"][-3:])
aps = [a for a in c.get(B + "/sentinel/api/approvals?status=pending", headers=H).json()["approvals"] if a["task_id"] == tid]
check("one approval for the fill", len(aps) == 1, aps)
if aps:
    ap = aps[0]
    s = json.dumps(ap, ensure_ascii=False)
    check("approval card shows label + masked value, not the number", "KrisFlyer" in s and "•••• 5678" in s and SECRET not in s, s[:600])
    r = c.post(f"{B}/sentinel/api/approvals/{ap['id']}/resolve", headers=H,
               json={"decision": "approve", "scope": "PERMANENT"}).json()   # scope is forced to ONCE
    check("approved fill executed", (r.get("result") or {}).get("status") == "ok", r)
    check("no standing grant for vault fills",
          not [g for g in c.get(B + "/sentinel/api/grants", headers=H).json()["grants"] if g["tool"] == "browser_fill_secret"])
t = wait(tid, {"COMPLETED", "FAILED"})
check("task completes", t["status"] == "COMPLETED", t["status"])
everything = json.dumps(t, ensure_ascii=False)
check("the agent never saw the number (results, snapshot, transcript)", SECRET not in everything, everything[-1200:])
look = [e for e in t["events"] if e["type"] == "tool_result" and e["data"]["name"] == "browser_look"]
check("vision refused on a page holding a vault value", look and "保险箱" in look[0]["data"]["preview"], look)
audit = c.get(B + "/sentinel/api/audit?limit=300", headers=H).text
check("audit never contains the number", SECRET not in audit)
it = c.get(B + "/sentinel/api/vault", headers=H).json()["items"][0]
check("use counted", it["uses"] == 1, it)

# ------------------------------------------------------------------ write-time sorting
c.put(B + "/api/profile", json={"key": "phone", "value": ""}, headers=H)
tid = chat("PROFILEFACT my new mobile is +65 9000 1111 and I have a dentist booking on Friday")
wait(tid, {"COMPLETED", "FAILED"})
time.sleep(3)
m = c.get(B + "/api/memory").json()
check("profile detail queued, not applied", any(p["key"] == "phone" and p["value"] == "+65 9000 1111" for p in m["pending"])
      and "phone" not in m["profile"], m["pending"])
check("one-off detail goes to recent", any("dentist" in f["fact"] for f in m["recent"]), [f["fact"] for f in m["recent"]])
p = next(p for p in m["pending"] if p["key"] == "phone")
c.post(f"{B}/api/profile/pending/{p['id']}", json={"accept": True}, headers=H)
check("accepted suggestion updates the profile", c.get(B + "/api/memory").json()["profile"].get("phone") == "+65 9000 1111")

# ------------------------------------------------------------------ tidy: preview, then apply exactly that
c.post(B + "/api/memory", json={"fact": "Opened zipair and clicked search", "category": "other"}, headers=H)
c.post(B + "/api/memory", json={"fact": "Lucas likes quiet hotels!", "category": "preference"}, headers=H)
c.post(B + "/api/memory", json={"fact": "Lucas likes the quiet hotels", "category": "preference"}, headers=H)
before = c.get(B + "/api/memory").json()


def run_job(body):
    r = c.post(B + "/api/memory/consolidate", json=body, headers=H)
    assert r.status_code == 200, r.text
    for _ in range(120):
        j = c.get(B + "/api/memory").json()
        if not j["job"].get("running"):
            return j
        time.sleep(0.5)
    return j


j = run_job({"dry_run": True})
prev = j["runs"][0]
check("preview stored", prev["kind"] == "dry_run" and prev["lines"], prev)
check("preview changes nothing", len(j["facts"]) == len(before["facts"]), (len(j["facts"]), len(before["facts"])))
rep = c.get(f"{B}/api/memory/runs/{prev['id']}").json()["report"]
check("preview lists the merge and the demotion", rep["plan"]["local"]["dups"] and rep["plan"]["llm"]["demote"], rep["plan"])
j = run_job({"from_run": prev["id"]})
facts = [f["fact"] for f in j["facts"]]
check("apply merged the duplicate", sum("quiet hotels" in f for f in facts) == 1, facts)
check("apply demoted the process note", any("Opened zipair" in f["fact"] for f in j["recent"]) and not any("Opened zipair" in f for f in facts))
check("preview marked applied", any(r["id"] == prev["id"] and r["applied"] for r in j["runs"]), j["runs"][:2])
r = c.post(B + "/api/memory/consolidate", json={"from_run": prev["id"]}, headers=H)
time.sleep(1)
check("a preview applies only once", "已执行" in (c.get(B + "/api/memory").json()["job"].get("error") or ""), c.get(B + "/api/memory").json()["job"])

print("\nFAILED:" if fails else "\nALL PASSED", fails)
sys.exit(1 if fails else 0)
