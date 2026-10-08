"""Roadmap batch 2 end to end (local stack, tests/run_local.sh): personal context v1.
1. memory sorted into domains; entities with aliases / relation
2. a request gets the facts it needs (size, the shop's habits) grouped by domain, with the don't-ask-again rule —
   the old selection (legacy) misses them for the same Chinese request
3. a site's habits appear when the browser lands on that site
4. something OMuse learns by itself waits for the user's OK (still used, marked) and the user can confirm it
5. the Memory page shows the domains, the learned list and the "try it" preview (screenshot)
6. the mailbox health check
"""
import os
import sys
import time

import httpx

B, LLM = "http://127.0.0.1:8080", "http://127.0.0.1:8090"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=90, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:600], flush=True)
    if not cond:
        fails.append(name)


def wait_task(tid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = c.get(f"{B}/api/tasks/{tid}").json()
        if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return t
        time.sleep(1)
    return t


def add(fact, domain=""):
    return c.post(B + "/api/memory", json={"fact": fact, "domain": domain}, headers=H).json()


def mem():
    return c.get(B + "/api/memory", headers=H).json()


def preview(q, mode="v1"):
    return [f["fact"] for f in c.get(B + "/api/context/preview", params={"q": q, "mode": mode}, headers=H).json()["facts"]]


# ---- 1. domains + entities
add("我身高177，体重76公斤，鞋子42码，运动鞋43码。")
add("shop.test 网站支持访客结账；电话号码填 8 位，不要加 +65。")
add("The user prefers business class seats.")
add("The user prefers dining at Hunan cuisine restaurants.")
add("The user is interested in news regarding AI and robotics.")
add("The user prefers metal pens with an aesthetic design.")
add("用户要求：以后每次说「准备出差」，先查邮件里的航班和酒店，再生成一页行程 PDF。")
r = c.post(B + "/api/memory", json={"fact": "Mei collects jazz vinyl records.", "domain": "person", "entity": "Mei"}, headers=H).json()
m = mem()
dom = {f["fact"][:6]: f["domain"] for f in m["facts"]}
check("domains: size → preference, shop habit → site, standing instruction → rule",
      dom.get("我身高177") == "preference" and dom.get("shop.t") == "site" and dom.get("用户要求：以") == "rule", dom)
check("7 domains listed for the Memory page", [d["key"] for d in m["domains"]] == ["person", "preference", "place", "account", "site", "rule", "work"])
mei = next((e for e in m["entities"] if e["name"] == "Mei"), None)
check("a person named by a fact becomes an entity", mei and mei["type"] == "person", m["entities"])
c.put(f"{B}/api/entities/{mei['id']}", json={"aliases": "梅梅", "relation": "太太"}, headers=H)
check("alias / relation let a different wording find the person", any("Mei" in f for f in preview("帮我给太太挑个生日礼物")),
      preview("帮我给太太挑个生日礼物"))

# ---- 2. the request gets what it needs; legacy did not
q = "帮我在 shop.test 挑一双跑鞋，选好尺码加入购物车"
v1, old = preview(q), preview(q, "legacy")
check("v1 context has the shoe size and the shop's checkout habit", any("43码" in f for f in v1) and any("shop.test" in f for f in v1), v1)
check("v1 context leaves out unrelated facts (pens, cabin class, cuisine)",
      not any(k in " ".join(v1) for k in ("metal pens", "business class", "Hunan")), v1)
check("legacy selection (before batch 2) hands unrelated facts to a shoe task",
      sum(k in " ".join(old) for k in ("metal pens", "business class", "Hunan", "AI and robotics")) >= 3, old)
check("trigger rule only when its phrase is said", not any("准备出差" in f for f in preview("帮我订机票去东京"))
      and any("准备出差" in f for f in preview("准备出差，下周去东京")))

tid = c.post(B + "/api/chat", json={"message": "CTXCHECK " + q}, headers=H).json()["task_id"]
t = wait_task(tid)
calls = c.get(LLM + "/calls_for", params={"marker": "CTXCHECK"}).json()
sysp = calls[0]["system"] if calls else ""
check("executor prompt carries the grouped context and the don't-ask-again rule",
      "[偏好 Preferences]" in sysp and "43码" in sysp and "do NOT ask the user again" in sysp, sysp[-1500:])
ev = [e for e in t["events"] if e["type"] == "context_used"]
check("the task shows which memories it used (timeline)", ev and any("43码" in f["fact"] for f in ev[0]["data"]["facts"]), ev)

# ---- 3. site habits when the browser lands there
tid = c.post(B + "/api/chat", json={"message": "SITECHECK open the page"}, headers=H).json()["task_id"]
t = wait_task(tid)
calls = c.get(LLM + "/calls_for", params={"marker": "SITECHECK"}).json()
seen = " ".join(x["tools"] for x in calls)
check("landing on shop.test shows what OMuse learned there", "你以前在 shop 学到的" in seen and "8 位" in seen, seen[:800])
check("timeline records it", any(e["type"] == "context_site" for e in t["events"]))

# ---- 4. learned → pending → confirmed
n0 = len([f for f in mem()["facts"] if f["status"] == "pending"])
tid = c.post(B + "/api/chat", json={"message": "LEARNSITE shop.test checkout"}, headers=H).json()["task_id"]
wait_task(tid)
pend = [f for f in mem()["facts"] if f["status"] == "pending"]
check("a habit OMuse learned itself waits for the user's OK", len(pend) == n0 + 1 and pend[0]["domain"] == "site", pend)
check("…but is used meanwhile, marked unconfirmed", any("guest checkout" in f for f in preview("在 shop.test 买个水壶")))
c.put(f"{B}/api/memory/{pend[0]['id']}", json={"status": "active"}, headers=H)
check("the user confirms it on the Memory page", next(f for f in mem()["facts"] if f["id"] == pend[0]["id"])["status"] == "active")
tid = c.post(B + "/api/chat", json={"message": "REMEMBER this"}, headers=H).json()["task_id"]
wait_task(tid)
check("something the user asked to remember is kept right away", any(f["fact"] == "Lucas likes quiet hotels" and f["status"] == "active" for f in mem()["facts"]))

# ---- 5b. the fast personal-context probe runs end to end (plan + statement, nothing executed)
r = c.post(B + "/api/golden/run", json={"suite": "context_probe", "context": "legacy"}, headers=H).json()
t0 = time.time()
while time.time() - t0 < 120:
    runs = c.get(B + "/api/golden/runs?limit=1", headers=H).json()["runs"]
    if runs and runs[0]["status"] != "running":
        break
    time.sleep(2)
run = c.get(B + "/api/golden/runs?limit=1", headers=H).json()["runs"][0]
check("context probe scores all 10 requests without running any tool", run["trigger"] == "manual:context-legacy" or
      (run["trigger"] == "probe:context-legacy" and run["status"] == "done" and len(run["results"]) == 10
       and all("plan" in x and "context" in x for x in run["results"])), run)

# ---- 6. mailbox health
st = c.post(B + "/api/health/check", json={}, headers=H).json()
comps = st.get("components") or {}
check("health check runs (mail watched when a mailbox is connected)", {"model", "sentinel"} <= set(comps), comps)

print()
print(("%d FAILED: %s" % (len(fails), fails)) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
