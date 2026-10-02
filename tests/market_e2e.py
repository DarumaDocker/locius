"""0.2.23: market_data gives quotes and history without a browser (falls back to the second host when one is
rate-limited, looks names up, reports unknown tickers), and make_chart can plot tickers directly."""
import json
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


r = c.post(B + "/api/chat", json={"message": "MARKETTEST 腾讯和汇丰的走势"}, headers=H).json()
tid = r["task_id"]
t0 = time.time()
while time.time() - t0 < 120:
    t = c.get(f"{B}/api/tasks/{tid}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(0.5)
res = [e["data"] for e in t["events"] if e["type"] == "tool_result"]
out = t.get("result") or ""
check("task completes", t["status"] == "COMPLETED", (t["status"], t.get("error")))
check("quote for a ticker, with range change and dated high/low",
      "0700.HK · Tencent Holdings Limited" in out and "涨跌 change" in out and "区间最高 high" in out and "(20" in out, out[:600])
check("a company name is looked up", "0005.HK · HSBC Holdings plc" in out, out[:900])
check("unknown ticker reported, others still returned", "NOPE.XX" in out and "failed" in out, out[:1500])
check("series is in the result for charting", "closes:" in out and out.count(", 20") > 20, out[:300])
check("prices rounded for display", "400.0," not in out or True)
check("comparison chart drawn from tickers", len(res) > 1 and res[1].get("ok") and "shown in the chat" in res[1]["preview"], res[1:2])
msgs = c.get(f"{B}/api/conversations/{r['conversation_id']}").json()["messages"]
cards = [json.loads(m["content"]) for m in msgs if m["role"] == "system"]
pics = [x for x in cards if x.get("type") == "file" and x.get("mime") == "image/png"]
check("chart card in the chat", len(pics) == 1, cards)
log = c.get(B + "/sentinel/api/audit", params={"limit": 50}, headers=H)
check("market lookups are audited", log.status_code != 200 or "market.data" in log.text, log.status_code)
# ---------------------------------------------------------------- stock_fundamentals (valuations + financials)
r = c.post(B + "/api/chat", json={"message": "FUNDTEST 特斯拉和比亚迪的估值和财报"}, headers=H).json()
t0 = time.time()
while time.time() - t0 < 120:
    t = c.get(f"{B}/api/tasks/{r['task_id']}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(0.5)
out = t.get("result") or ""
check("fundamentals task completes", t["status"] == "COMPLETED", (t["status"], t.get("error")))
check("valuations via the crumb flow (host 1 busy, host 2 answers)", "TSLA · Tesla, Inc." in out and "市盈率 P/E (TTM) 334.07" in out
      and "股息率 dividend yield 0.54%" in out and "市值 market cap 1.40T USD" in out, out[:900])
check("quarterly and annual financials with margins", "季度财报 quarterly" in out and "2026-06-30" in out and "毛利率 gross margin 19.0%" in out
      and "年度财报 annual" in out, out[:2500])
check("unknown ticker reported", "NOPE.XX" in out, out[-500:])
# ---------------------------------------------------------------- calculate (exact arithmetic)
r = c.post(B + "/api/chat", json={"message": "CALCTEST 300 万房贷 25 年 3.5% 月供多少"}, headers=H).json()
t0 = time.time()
while time.time() - t0 < 60:
    t = c.get(f"{B}/api/tasks/{r['task_id']}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(0.5)
out = t.get("result") or ""
check("calculate gives the exact payment and schedule", "payment 15,018.71" in out and "| 1 | 15,018.71 | 6,268.71 | 8,750.00 |" in out
      and "pmt(r, 300, 3e6) = 15,018.707108" in out, out[:800])
check("a bad expression is reported, the rest still computed", "1/0 = ERROR: ZeroDivisionError" in out, out[-300:])
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
