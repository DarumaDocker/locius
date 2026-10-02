"""0.2.23: make_chart draws charts/diagrams locally (SVG -> PNG in the browser container, offline) and shows them
in the chat; a bad spec gets an actionable error; a chart can be embedded in a PDF."""
import sys
import time

import httpx

B = "http://127.0.0.1:8080"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=60, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:900])
    if not cond:
        fails.append(name)


r = c.post(B + "/api/chat", json={"message": "CHARTTEST 画一下腾讯走势和报销流程"}, headers=H).json()
tid = r["task_id"]
t0 = time.time()
while time.time() - t0 < 120:
    t = c.get(f"{B}/api/tasks/{tid}").json()
    if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(0.5)
res = [e["data"] for e in t["events"] if e["type"] == "tool_result"]
check("task completes", t["status"] == "COMPLETED", (t["status"], t.get("error")))
check("line chart rendered and shown", res and res[0].get("ok") and "shown in the chat" in res[0]["preview"], res[:1])
check("bad spec explained", len(res) > 1 and res[1].get("ok") is False and "labels 有 2 个" in res[1]["preview"], res[1:2])
check("flow chart saved but not sent", len(res) > 2 and "not sent" in res[2]["preview"], res[2:3])
check("PDF with the chart made", len(res) > 3 and res[3].get("ok") and "chart_report.pdf" in res[3]["preview"], res[3:4])
msgs = c.get(f"{B}/api/conversations/{r['conversation_id']}").json()["messages"]
import json
cards = [json.loads(m["content"]) for m in msgs if m["role"] == "system"]
pics = [x for x in cards if x.get("type") == "file" and x.get("mime") == "image/png"]
check("exactly one chart card in the chat", len(pics) == 1 and pics[0]["path"].startswith("charts/"), cards)
if pics:
    png = c.get(B + "/api/files/raw", params={"path": pics[0]["path"]}).content
    check("a real PNG, large enough to be a chart", png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20000, len(png))
pdf = c.get(B + "/api/files/raw", params={"path": "reports/chart_report.pdf"}).content
check("PDF embeds the picture", pdf[:4] == b"%PDF" and b"/Image" in pdf, len(pdf))
ev = [e for e in t["events"] if e["type"] == "chart"]
check("chart events on the timeline", len(ev) == 2, ev)
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
