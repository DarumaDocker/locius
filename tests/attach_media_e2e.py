"""0.2.20: the agent saves photos/videos from a page and sends them into the chat as one gallery; the user attaches
files (＋ button) and the agent reads them together with the message, and can look at them again later."""
import io
import json
import sys
import time
import zipfile

import httpx

B = "http://127.0.0.1:8080"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=120, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:800])
    if not cond:
        fails.append(name)


def chat(msg, conv=None, attachments=None):
    r = c.post(B + "/api/chat", json={"message": msg, "conversation_id": conv, "attachments": attachments or []}, headers=H)
    r.raise_for_status()
    return r.json()


def wait(tid, states=("COMPLETED", "FAILED"), timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = c.get(f"{B}/api/tasks/{tid}").json()
        if t["status"] in states:
            return t
        time.sleep(0.5)
    return t


def upload(name, data):
    return c.put(f"{B}/api/upload", params={"name": name}, content=data, headers=H)


def conv_msgs(cid):
    return c.get(f"{B}/api/conversations/{cid}").json()["messages"]


# ------------------------------------------------------------------ agent → user: photos and a video in the chat
r = chat("MEDIASHOP find OMG best sellers and send me the photos")
t = wait(r["task_id"])
check("media task completes", t["status"] == "COMPLETED", (t["status"], t.get("error"), t["events"][-4:]))
cards = [json.loads(m["content"]) for m in conv_msgs(r["conversation_id"]) if m["role"] == "system"]
gal = [x for x in cards if x.get("type") == "files"]
check("one gallery card with 3 items", gal and len(gal[0]["items"]) == 3, cards)
if gal:
    mimes = sorted(i["mime"] for i in gal[0]["items"])
    check("two photos and the video", mimes == ["image/jpeg", "image/png", "video/mp4"], mimes)
    for it in gal[0]["items"]:
        rr = c.get(B + "/api/files/raw", params={"path": it["path"]})
        check(f"saved file served inline: {it['name']}", rr.status_code == 200 and len(rr.content) == it["size"]
              and "inline" in rr.headers.get("content-disposition", ""), rr.status_code)
    check("files live under media/", all(i["path"].startswith("media/") for i in gal[0]["items"]))
snap = next((e for e in t["events"] if e["type"] == "tool_result" and e["data"]["name"] == "browser_navigate"), None)
prev = snap["data"]["preview"] if snap else ""
check("snapshot gives photos and the video a ref, but not tiny icons", "(photo)" in prev and "(video)" in prev
      and "tiny icon" not in prev, prev[:800])

# ------------------------------------------------------------------ user → agent: attachments
r = upload("x.exe", b"MZ")
check("executables are refused", r.status_code == 400, r.text)
r = c.put(f"{B}/api/upload", params={"name": "a.txt"}, content=b"hi")
check("upload needs the UI header", r.status_code == 403, r.status_code)

buf = io.BytesIO()
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("word/document.xml", f'<w:document {W}><w:body><w:p><w:r><w:t>Quarterly report: revenue +12%</w:t></w:r></w:p></w:body></w:document>')
docx = upload("Q3 report.docx", buf.getvalue()).json()
png = upload("red.png", open("tests/pages/lipstick.png", "rb").read()).json()
md = upload("notes.md", "# Notes\nhello-markdown".encode()).json()
check("uploads stored under uploads/", all(x.get("path", "").startswith("uploads/") for x in (docx, png, md)), (docx, png, md))
check("upload reports the kind", (docx.get("kind"), png.get("kind"), md.get("kind")) == ("docx", "image", "text"), (docx, png, md))

r = chat("ATTACHTEST compare these", attachments=[docx["path"], png["path"], md["path"]])
t = wait(r["task_id"])
check("attachments read together with the message", "ATTACHOK docx=True img=True md=True" in (t.get("result") or ""), t.get("result"))
um = [m for m in conv_msgs(r["conversation_id"]) if m["role"] == "user"][-1]
check("the user message keeps its attachments", len(((um.get("meta") or {}).get("attachments") or [])) == 3, um)
check("attachment reads are on the timeline", sum(1 for e in t["events"] if e["type"] == "attachment_read") == 3)

r2 = chat("ATTACHFOLLOW what colour was the picture?", conv=r["conversation_id"])
t = wait(r2["task_id"])
check("a later message can look at an earlier attachment", "FOLLOWOK" in (t.get("result") or "") and "VISION-SEEN" in t["result"],
      t.get("result"))

vid = upload("clip.mp4", open("tests/pages/promo.mp4", "rb").read()).json()
r = chat("ATTACHTEST what happens in this video", attachments=[vid["path"]])
t = wait(r["task_id"])
first = (t.get("transcript") or [{}, {}])[1].get("content", "") if t.get("transcript") else ""
check("video attachment is looked at (frames)", "img=True" in (t.get("result") or ""), t.get("result"))

r = c.post(B + "/api/chat", json={"message": "", "attachments": [png["path"]]}, headers=H)
check("attachments alone can be sent", r.status_code == 200, r.text)
r = c.post(B + "/api/chat", json={"message": "x", "attachments": ["../runtime.db"]}, headers=H)
check("only uploaded files can be attached", r.status_code == 400, r.text)

print("\nFAILED:" if fails else "\nALL PASSED", fails)
sys.exit(1 if fails else 0)
