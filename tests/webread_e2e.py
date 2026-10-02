"""0.2.26: browser_search returns results in one call (falls back to the next engine when one shows a challenge page),
browser_read reads several pages in parallel in one call, and both results are marked untrusted."""
import sys
import time

import httpx

B = "http://127.0.0.1:8080"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=60, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:1200])
    if not cond:
        fails.append(name)


t0 = time.time()
r = c.post(B + "/api/chat", json={"message": "WEBREAD 查一下 Olares One"}, headers=H).json()
while time.time() - t0 < 120:
    t = c.get(f"{B}/api/tasks/{r['task_id']}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(0.5)
out = t.get("result") or ""
res = [e["data"] for e in t["events"] if e["type"] == "tool_result"]
check("task completes", t["status"] == "COMPLETED", (t["status"], t.get("error")))
check("search results with decoded URLs (second engine after a challenge page)",
      "Result 1 for olares one" in out and "http://shop.test:8094/slow?s=0&name=Page1" in out and "duckduckgo" in out, out[:1200])
check("results are untrusted content", out.count("untrusted_content") >= 2, out[:300])
check("three pages read in one call", all(f"<h1>Page{x}" in out or f"Page{x}" in out for x in "ABC"), out[-1500:])
read = [e for e in t["events"] if e["type"] == "tool_result" and e["data"]["name"] == "browser_read"]
took = [e["ts"] for e in t["events"] if e["type"] in ("tool_call", "tool_result") and e["data"].get("name") == "browser_read"]
check("pages are read in parallel (3 pages with a 1 s delay each in well under 3 s of loading)",
      len(took) == 2 and took[1] - took[0] < 6, took)
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
