"""The agent can hand a file to the user: a download card in the chat (web UI), and the file itself on Telegram when
the conversation came from Telegram. Active content (HTML) is never rendered on the app's own origin."""
import asyncio, os, sys, time
import httpx
from playwright.async_api import async_playwright

B = "http://127.0.0.1:8080"
TG = "http://127.0.0.1:8091"
H = {"X-Persona-UI": "1"}
WS = "/tmp/claude-0/persona-test/workspace"
c = httpx.Client(timeout=60, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else info)
    if not cond:
        fails.append(name)


def wait(tid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = c.get(f"{B}/api/tasks/{tid}").json()
        if t["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return t
        time.sleep(0.5)
    return t


# 1 web chat: file card message + download endpoint
r = c.post(B + "/api/chat", json={"message": "SENDFILE make me a brief"}, headers=H).json()
t = wait(r["task_id"])
check("task completed", t["status"] == "COMPLETED", (t["status"], t.get("error")))
conv = c.get(f"{B}/api/conversations/{r['conversation_id']}").json()
files = [m for m in conv["messages"] if m["role"] == "system" and '"type": "file"' in m["content"]]
check("file card posted in the conversation", len(files) == 1 and "reports/brief.md" in files[0]["content"], conv["messages"][-3:])
d = c.get(B + "/api/files/raw", params={"path": "reports/brief.md", "download": 1})
check("download returns the file as an attachment", d.status_code == 200 and "Hello from Locius" in d.text
      and "attachment" in d.headers.get("content-disposition", "") and "brief.md" in d.headers.get("content-disposition", ""),
      (d.status_code, dict(d.headers)))
i = c.get(B + "/api/files/raw", params={"path": "reports/brief.md"})
check("open (inline) still works for plain files", i.status_code == 200 and "inline" in i.headers.get("content-disposition", ""),
      dict(i.headers))
check("not sent to Telegram (web chat)", not c.get(TG + "/_log").json().get("docs"), c.get(TG + "/_log").json().get("docs"))

# 2 HTML in the workspace is never rendered on the app origin
os.makedirs(WS + "/downloads", exist_ok=True)
open(WS + "/downloads/evil.html", "w").write("<script>fetch('/api/settings')</script>")
e = c.get(B + "/api/files/raw", params={"path": "downloads/evil.html"})
check("HTML is forced to download + sandboxed", "attachment" in e.headers.get("content-disposition", "")
      and "sandbox" in e.headers.get("content-security-policy", "") and "html" not in e.headers.get("content-type", ""),
      dict(e.headers))
bad = c.get(B + "/api/files/raw", params={"path": "../../etc/passwd"})
check("path traversal refused", bad.status_code == 404, bad.status_code)


# 3 the web UI renders the card with a working download link
async def ui():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1280, "height": 800})
        await pg.goto(f"{B}/#chat/{r['conversation_id']}")
        await pg.wait_for_selector(".filecard", timeout=15000)
        info = await pg.evaluate("""() => { const c = document.querySelector('.filecard');
            const a = c.querySelector('a[download]'); return {text: c.innerText, href: a && a.getAttribute('href'), dl: a && a.download}; }""")
        check("UI shows the file card", "brief.md" in info["text"] and "today's brief" in info["text"], info)
        async with pg.expect_download() as dl:
            await pg.click(".filecard a[download]")
        f = await dl.value
        path = await f.path()
        check("clicking Download saves the file", f.suggested_filename == "brief.md" and "Hello from Locius" in open(path).read(),
              f.suggested_filename)
        await b.close()
asyncio.run(ui())

# 4 Telegram conversation: the file itself goes to the owner's chat
st = c.get(B + "/sentinel/api/telegram/status", headers=H)
tg_on = st.status_code == 200 and (st.json().get("running") or st.json().get("status", {}).get("running"))
if not tg_on:
    print("SKIP telegram part (bot not configured — run after telegram_e2e)")
else:
    c.post(TG + "/_push", json={"message": {"message_id": 9, "chat": {"id": 555, "type": "private", "first_name": "Lucas"}, "text": "/new"}})
    time.sleep(2)
    c.post(TG + "/_push", json={"message": {"message_id": 10, "chat": {"id": 555, "type": "private", "first_name": "Lucas"},
                                            "text": "SENDFILE 把简报发给我"}})
    t0, docs = time.time(), []
    while time.time() - t0 < 60 and not docs:
        docs = c.get(TG + "/_log").json().get("docs") or []
        time.sleep(0.5)
    check("file delivered to the Telegram owner chat", docs and docs[0]["name"] == "brief.md" and docs[0]["chat_id"] == "555"
          and "Hello from Locius" in docs[0]["head"] and "简报" in (docs[0]["caption"] or ""), docs)

print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
