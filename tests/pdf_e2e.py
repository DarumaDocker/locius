"""Local PDF export: Chinese text, tables and local images end up in a real PDF, made on this machine with no network
(remote images / scripts in the source never load), and the agent hands the PDF over with send_file."""
import base64, io, os, sys, time
import httpx
from pypdf import PdfReader

B = "http://127.0.0.1:8080"
H = {"X-Persona-UI": "1"}
T = "/tmp/claude-0/persona-test"
WS = T + "/workspace"
c = httpx.Client(timeout=90, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else info)
    if not cond:
        fails.append(name)


def wait(tid, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = c.get(f"{B}/api/tasks/{tid}").json()
        if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return t
        time.sleep(0.5)
    return t


# a tiny local image the report embeds (1x1 red PNG)
os.makedirs(WS + "/reports", exist_ok=True)
open(WS + "/reports/chart.png", "wb").write(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="))
open(WS + "/reports/page.html", "w").write('<html><body><h1>HTML 报告</h1><script>document.body.innerHTML="HACKED"</script>'
                                             '<img src="http://shop.test:8099/track-html.png"></body></html>')

r = c.post(B + "/api/chat", json={"message": "MAKEPDF 把报告做成 PDF 发给我"}, headers=H).json()
t = wait(r["task_id"])
check("task completed", t["status"] == "COMPLETED", (t["status"], t.get("error"), t.get("result")))
pdf_path = WS + "/reports/cn.pdf"
check("PDF written next to the source", os.path.isfile(pdf_path), os.listdir(WS + "/reports"))
if os.path.isfile(pdf_path):
    data = open(pdf_path, "rb").read()
    rd = PdfReader(io.BytesIO(data))
    txt = "".join(p.extract_text() or "" for p in rd.pages)
    check("real PDF with Chinese text", data[:5] == b"%PDF-" and "未来学院" in txt and "腾讯" in txt and "WorkBuddy" in txt, txt[:200])
    check("local image embedded", any(len(p.images) for p in rd.pages), [len(p.images) for p in rd.pages])
    check("page numbers in footer", "1 / 1" in txt or "1 /" in txt, txt[-80:])
conv = c.get(f"{B}/api/conversations/{r['conversation_id']}").json()
check("PDF handed over as a file card", any('"type": "file"' in m["content"] and "cn.pdf" in m["content"]
                                            for m in conv["messages"] if m["role"] == "system"), conv["messages"][-3:])
ev = c.get(B + "/sentinel/api/audit?limit=50").json()["events"]
check("audited as pdf.render", any(e["action"] == "pdf.render" and e.get("result") == "success" for e in ev))

# HTML source: printed with JS off and network blocked
rr = c.post(B + "/internal/render_pdf", json={"source": "reports/page.html"}, headers={"X-Persona-Runtime": "rt-test"})
check("HTML source printed", rr.status_code == 200 and rr.json().get("path") == "reports/page.pdf", rr.text[:200])
if rr.status_code == 200:
    txt = "".join(p.extract_text() or "" for p in PdfReader(WS + "/reports/page.pdf").pages)
    check("scripts in the source did not run", "HTML" in txt and "HACKED" not in txt, txt[:200])
time.sleep(1)
log = open(T + "/pages.log").read()
check("no network requests while printing", "track-md.png" not in log and "track-html.png" not in log,
      [l for l in log.splitlines() if "track" in l])
bad = c.post(B + "/internal/render_pdf", json={"source": "../../etc/passwd"}, headers={"X-Persona-Runtime": "rt-test"})
check("paths outside the workspace refused", "error" in bad.json(), bad.text[:200])
noauth = c.post(B + "/internal/render_pdf", json={"source": "reports/cn.md"})
check("internal endpoint needs the runtime token", noauth.status_code in (401, 403), noauth.status_code)

print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
