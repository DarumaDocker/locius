"""Browser Broker: a restricted API over a persistent Chromium (Playwright).

The agent never gets raw CDP or JS execution. It can only navigate, read an
accessibility-style snapshot and act on element refs. Sentinel is the only caller
(shared BROWSER_TOKEN). The user can take over at any time; while the user has
control, every agent call is rejected with 423.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from playwright.async_api import async_playwright

from app.common.util import token_ok
from app.sentinel.guard import check_url, host_is_private

TOKEN = os.environ.get("BROWSER_TOKEN", "")
PROFILE_DIR = os.environ.get("BROWSER_PROFILE", "/bprofile")
WORKSPACE = os.path.realpath(os.environ.get("WORKSPACE", "/workspace"))
# Default: a real (headed) Chromium on a virtual display (Xvfb). Headless browsers are flagged by Google & co.
# ("This browser may not be secure"), which breaks "Sign in with Google" during a user takeover.
HEADLESS_ENV = os.environ.get("BROWSER_HEADLESS", "")          # "1" force headless, "0" force headed, "" auto
VIEWPORT = {"width": 1280, "height": 800}
UA_ENV = os.environ.get("BROWSER_UA", "")
UA_HEADLESS = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
BLOCKED_EXT = {".exe", ".msi", ".dmg", ".pkg", ".app", ".bat", ".cmd", ".com", ".scr", ".sh", ".ps1", ".vbs", ".jar",
               ".apk", ".deb", ".rpm", ".iso", ".dll", ".so"}
MAX_DOWNLOAD = 200 * 1024 * 1024
SNAPSHOT_JS = open(os.path.join(os.path.dirname(__file__), "snapshot.js"), encoding="utf-8").read()
FEED_JS = open(os.path.join(os.path.dirname(__file__), "feed.js"), encoding="utf-8").read()
BLOCK_JS = open(os.path.join(os.path.dirname(__file__), "block.js"), encoding="utf-8").read()
FIND_JS = open(os.path.join(os.path.dirname(__file__), "find.js"), encoding="utf-8").read()
MARKS_JS = open(os.path.join(os.path.dirname(__file__), "marks.js"), encoding="utf-8").read()
GRID_JS = open(os.path.join(os.path.dirname(__file__), "grid.js"), encoding="utf-8").read()
AT_JS = open(os.path.join(os.path.dirname(__file__), "at.js"), encoding="utf-8").read()


def format_feed(feed: dict, url: str) -> str:
    items = feed.get("items") or []
    out = [f"📰 RSS/Atom feed「{feed.get('feed') or url}」— {len(items)} 条 items (newest first as published). "
           "这就是完整内容，不需要重复打开 This is the whole feed; no need to open it again."]
    for i, it in enumerate(items, 1):
        out.append(f"{i}. {it.get('title') or '(no title)'}" + (f"  [{it['date']}]" if it.get("date") else ""))
        if it.get("link"):
            out.append(f"   {it['link']}")
        if it.get("summary"):
            out.append(f"   {it['summary']}")
    return "\n".join(out)


class Broker:
    def __init__(self):
        self.pw = None
        self.ctx = None
        self.pages: dict[str, object] = {}       # task_id -> page
        self.frame_maps: dict[str, dict] = {}    # task_id -> {prefix: frame}
        self.nav_status: dict[str, int] = {}     # task_id -> HTTP status of the last navigation
        self.view_task: str = ""
        self.mode = "agent"                      # agent | user
        self.takeover_reason = ""
        self.takeover_task = ""
        self.requests: dict[str, dict] = {}      # task_id -> {"task_id","reason","ts"}: tasks that asked the user for help
        self.downloads: list[dict] = []
        self.lock = asyncio.Lock()
        self.input_lock = asyncio.Lock()
        self.last_used: dict[str, float] = {}
        self.parent: dict = {}                    # popup page -> opener page (e.g. "Sign in with Google" windows)
        self.xvfb = None
        self.headless = True

    async def start(self):
        if HEADLESS_ENV == "1":
            self.headless = True
        elif HEADLESS_ENV == "0" or os.environ.get("DISPLAY"):
            self.headless = False
        else:
            self.headless = not self._start_xvfb()   # must happen before the Playwright driver starts (env)
        self.pw = await async_playwright().start()
        os.makedirs(PROFILE_DIR, exist_ok=True)
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(PROFILE_DIR, lock))
            except FileNotFoundError:
                pass
        kw = {"env": dict(os.environ)}
        ua = UA_ENV or (UA_HEADLESS if self.headless else "")
        if ua:
            kw["user_agent"] = ua
        self.ctx = await self.pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=self.headless, viewport=VIEWPORT, accept_downloads=True, locale="en-US",
            ignore_default_args=["--enable-automation"],
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
                  "--window-size=1280,900", "--no-first-run", "--no-default-browser-check", "--password-store=basic"],
            **kw,
        )
        print(f"[browser] chromium started headless={self.headless} display={os.environ.get('DISPLAY', '')}", flush=True)
        await self.ctx.route("**/*", self._egress_filter)
        self.ctx.on("page", self._on_page)
        for p in self.ctx.pages:
            self._on_page(p)

    def _start_xvfb(self) -> bool:
        if not shutil.which("Xvfb"):
            return False
        disp = os.environ.get("BROWSER_DISPLAY", ":99")
        n = disp.lstrip(":")
        for f in (f"/tmp/.X{n}-lock", f"/tmp/.X11-unix/X{n}"):
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            self.xvfb = subprocess.Popen(["Xvfb", disp, "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-ac"],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        for _ in range(50):
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                os.environ["DISPLAY"] = disp
                return True
            if self.xvfb.poll() is not None:
                return False
            time.sleep(0.1)
        return False

    async def stop(self):
        try:
            await self.ctx.close()
        finally:
            await self.pw.stop()
            if self.xvfb:
                self.xvfb.terminate()

    # ------------------------------------------------------------ guards
    async def _egress_filter(self, route):
        url = route.request.url
        try:
            p = urlparse(url)
            if p.scheme in ("http", "https", "ws", "wss") and host_is_private(p.hostname or ""):
                return await route.abort("blockedbyclient")
        except Exception:
            return await route.abort("blockedbyclient")
        return await route.continue_()

    def _on_page(self, page):
        page.on("download", lambda d: asyncio.ensure_future(self._on_download(d)))
        page.on("popup", lambda pop, parent=page: self._on_popup(parent, pop))

    def _on_popup(self, parent, pop):
        """A window opened by a task's page (e.g. "Sign in with Google") becomes that task's current page, so the
        agent and the user's live view follow it; when it closes, the task goes back to the page that opened it."""
        for tid, p in list(self.pages.items()):
            if p is parent:
                self.parent[pop] = parent
                self.pages[tid] = pop
                pop.on("close", lambda _=None, tid=tid, pop=pop: self._on_popup_closed(tid, pop))
                break

    def _on_popup_closed(self, tid, pop):
        parent = self.parent.pop(pop, None)
        while parent is not None and parent.is_closed():
            parent = self.parent.pop(parent, None)
        if self.pages.get(tid) is pop and parent is not None:
            self.pages[tid] = parent

    async def _on_download(self, d):
        name = re.sub(r"[^\w.\-() 一-鿿]", "_", d.suggested_filename or "download")[:120]
        base = os.path.join(WORKSPACE, "downloads")
        quarantine = os.path.join(base, ".quarantine")
        os.makedirs(quarantine, exist_ok=True)
        tmp = os.path.join(quarantine, f"{int(time.time())}_{name}")
        rec = {"name": name, "url": d.url, "ts": time.time(), "status": "downloading"}
        self.downloads.append(rec)
        try:
            await d.save_as(tmp)
            size = os.path.getsize(tmp)
            ext = os.path.splitext(name)[1].lower()
            rec["size"] = size
            if ext in BLOCKED_EXT or size > MAX_DOWNLOAD:
                rec["status"] = "blocked"
                rec["reason"] = "可执行文件或文件过大，已隔离 (executable or too large; kept in quarantine)"
                rec["path"] = os.path.relpath(tmp, WORKSPACE)
            else:
                dest = os.path.join(base, name)
                if os.path.exists(dest):
                    stem, e = os.path.splitext(name)
                    dest = os.path.join(base, f"{stem}_{int(time.time())}{e}")
                shutil.move(tmp, dest)
                rec["status"] = "ok"
                rec["path"] = os.path.relpath(dest, WORKSPACE)
        except Exception as e:
            rec["status"] = "failed"
            rec["reason"] = str(e)[:200]

    def check_agent(self):
        if self.mode == "user":
            raise HTTPException(423, "用户正在接管浏览器，Agent 已暂停 (user takeover in progress)")

    # ------------------------------------------------------------ pages
    async def page_for(self, task_id: str, create=True):
        task_id = task_id or "default"
        p = self.pages.get(task_id)
        if p is not None and not p.is_closed():
            self.last_used[task_id] = time.time()
            self.view_task = task_id
            return p
        if not create:
            return None
        if len(self.pages) >= 6:
            oldest = min(self.pages, key=lambda k: self.last_used.get(k, 0))
            old = self.pages.pop(oldest)
            try:
                await old.close()
            except Exception:
                pass
        blank = [pg for pg in self.ctx.pages if pg.url in ("about:blank", "") and pg not in self.pages.values()]
        p = blank[0] if blank else await self.ctx.new_page()
        self.pages[task_id] = p
        self.last_used[task_id] = time.time()
        self.view_task = task_id
        return p

    def open_requests(self) -> list[dict]:
        """Takeover requests still waiting, newest first (requests older than 6 h are dropped)."""
        now = time.time()
        for k in [k for k, v in self.requests.items() if now - v.get("ts", 0) > 6 * 3600]:
            self.requests.pop(k, None)
        return sorted(self.requests.values(), key=lambda r: -r.get("ts", 0))

    @property
    def requested(self) -> dict | None:
        reqs = self.open_requests()
        return reqs[0] if reqs else None

    def view_page(self):
        p = self.pages.get(self.view_task)
        if p is not None and not p.is_closed():
            return p
        for tid, pg in self.pages.items():
            if not pg.is_closed():
                self.view_task = tid
                return pg
        return self.ctx.pages[0] if self.ctx and self.ctx.pages else None

    async def settle(self, page, ms=700):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(ms / 1000)

    async def snapshot(self, task_id: str, max_chars=12000, near: bool = False) -> dict:
        page = await self.page_for(task_id)
        await self.settle(page, 200)
        try:
            feed = await page.main_frame.evaluate(FEED_JS)
        except Exception:
            feed = None
        if feed and feed.get("items"):
            tabs = len([p for p in self.ctx.pages if not p.is_closed()])
            title = feed.get("feed") or ""
            return {"url": page.url, "title": title, "snapshot": format_feed(feed, page.url), "truncated": False,
                    "tabs": tabs, "feed": True}
        fmap = {}
        parts = []
        frames = [f for f in page.frames if not f.is_detached()]
        for i, fr in enumerate(frames[:8]):
            prefix = "" if i == 0 else f"f{i}"
            fmap[prefix] = fr
            try:
                txt = await fr.evaluate(SNAPSHOT_JS, {"prefix": prefix, "near": bool(near and i == 0)})
            except Exception as e:
                txt = f"(无法读取 frame: {str(e)[:80]})" if i == 0 else ""
            if i == 0:
                parts.append(txt)
            elif txt and txt.strip():
                parts.append(f"--- iframe {prefix}: {fr.url[:100]} ---\n{txt}")
        self.frame_maps[task_id or "default"] = fmap
        title = ""
        try:
            title = await page.title()
        except Exception:
            pass
        body = "\n".join(parts)
        max_chars = max(2000, min(int(max_chars or 12000), 40000))
        truncated = len(body) > max_chars
        if truncated:
            body = body[:max_chars] + ("\n…[快照已截断 snapshot truncated — browser_find(\"text\") locates anything on the page; "
                                       "browser_scroll shows the part around the new position; browser_look lets you see the page]")
        tabs = len([p for p in self.ctx.pages if not p.is_closed()])
        if near:
            body = "(showing the part of the page around the current scroll position)\n" + body
        return {"url": page.url, "title": title, "snapshot": body, "truncated": truncated, "tabs": tabs}

    async def find(self, task_id: str, query: str) -> dict:
        """Locate elements by text anywhere on the page (all frames); refs stay valid for click/type."""
        snap = await self.snapshot(task_id, 2000)          # assigns fresh refs in every frame
        matches = []
        for prefix, fr in (self.frame_maps.get(task_id or "default") or {}).items():
            try:
                found = await fr.evaluate(FIND_JS, query)
            except Exception:
                found = []
            matches += found
        matches.sort(key=lambda m: -m.get("score", 0))
        matches = matches[:25]
        lines = [f"browser_find \"{query}\": {len(matches)} match(es) on {snap['url']}"]
        for m in matches:
            ctx = m.get("context") or ""
            lines.append(f"[{m['ref']}] {m['role']} \"{m['name']}\" ({m['where']})" + (f"\n    context: {ctx}" if ctx and ctx != m['name'] else ""))
        if not matches:
            lines.append("(nothing found — try other words, browser_scroll to load more, or browser_look to see the page)")
        return {"url": snap["url"], "title": snap["title"], "snapshot": "\n".join(lines), "tabs": snap["tabs"], "matches": len(matches)}

    async def look(self, task_id: str) -> dict:
        """Screenshot of the viewport with every visible element labelled by its ref (set-of-marks) for a vision model."""
        page = await self.page_for(task_id)
        await self.snapshot(task_id, 2000)                 # fresh refs
        marks, text = [], ""
        # label the page and every iframe (chat widgets like Zendesk/Intercom live in iframes; their refs look like f1e3)
        frames = [fr for fr in (self.frame_maps.get(task_id or "default") or {}).values() if not fr.is_detached()] or [page.main_frame]
        try:
            for fr in frames:
                try:
                    got = await fr.evaluate(MARKS_JS, "draw")
                except Exception:
                    continue
                marks += got.get("marks") or []
                if got.get("text"):
                    text += ("\n" if text else "") + got["text"]
            img = await page.screenshot(type="jpeg", quality=70, timeout=10000)
        finally:
            for fr in frames:
                try:
                    await fr.evaluate(MARKS_JS, "clear")
                except Exception:
                    pass
        text = text[:3000]
        title = ""
        try:
            title = await page.title()
        except Exception:
            pass
        lines = [f"[{m['ref']}] {m['role']} \"{m['name']}\"" for m in marks]
        import base64 as _b64
        return {"url": page.url, "title": title, "image_b64": _b64.b64encode(img).decode(), "image_type": "image/jpeg",
                "marks": marks, "snapshot": "Labelled elements visible on screen:\n" + "\n".join(lines)
                + ("\n\nText visible on screen:\n" + text if text else ""),
                "viewport": VIEWPORT, "tabs": len([p for p in self.ctx.pages if not p.is_closed()])}

    async def grid(self, task_id: str, region=None, cols=0, rows=0, labels="letters", mark=None) -> dict:
        """Screenshot for visual targeting: a labelled grid over the viewport (or a zoomed region), and/or a red marker."""
        page = await self.page_for(task_id)
        vw, vh = VIEWPORT["width"], VIEWPORT["height"]
        try:
            vw, vh = await page.evaluate("[window.innerWidth, window.innerHeight]")
        except Exception:
            pass
        if region:
            x, y, w, h = (float(v) for v in region)
            x, y = max(0.0, min(x, vw - 20)), max(0.0, min(y, vh - 20))
            region = [x, y, max(20.0, min(w, vw - x)), max(20.0, min(h, vh - y))]
        opts = {"mode": "draw", "region": region, "cols": int(cols or 0), "rows": int(rows or 0), "labels": labels,
                "mark": [float(mark[0]), float(mark[1])] if mark else None}
        try:
            await page.main_frame.evaluate(GRID_JS, opts)
            shot = {"type": "jpeg", "quality": 75, "timeout": 10000}
            if region:
                shot["clip"] = {"x": region[0], "y": region[1], "width": region[2], "height": region[3]}
            img = await page.screenshot(**shot)
        finally:
            try:
                await page.main_frame.evaluate(GRID_JS, {"mode": "clear"})
            except Exception:
                pass
        import base64 as _b64
        out = {"url": page.url, "title": await page.title(), "image_b64": _b64.b64encode(img).decode(), "image_type": "image/jpeg",
               "region": region or [0, 0, vw, vh], "cols": opts["cols"], "rows": opts["rows"], "labels": labels,
               "viewport": {"width": vw, "height": vh}, "tabs": len([p for p in self.ctx.pages if not p.is_closed()]),
               "snapshot": ""}
        if mark:
            out["at"] = await self.at(task_id, mark[0], mark[1])
        return out

    async def at(self, task_id: str, x: float, y: float) -> dict:
        """The element under a viewport point, looking into iframes (also cross-origin) and open shadow roots."""
        page = await self.page_for(task_id)
        frame, fx, fy, frames = page.main_frame, float(x), float(y), []
        for _ in range(4):
            try:
                info = await frame.evaluate(AT_JS, [fx, fy])
            except Exception as e:
                return {"tag": "", "name": "", "error": str(e)[:120], "frames": frames}
            if not info:
                return {"tag": "", "name": "", "frames": frames}
            if not info.get("iframe"):
                info["frames"] = frames
                info["page_url"], info["page_title"] = page.url, await page.title()
                return info
            bx, by = info["box"][0], info["box"][1]
            child = None
            for fr in frame.child_frames:
                try:
                    el = await fr.frame_element()
                    bb = await el.bounding_box()
                except Exception:
                    continue
                if bb and abs(bb["x"] - bx) < 3 and abs(bb["y"] - by) < 3 or (bb and bb["x"] <= fx <= bb["x"] + bb["width"]
                                                                               and bb["y"] <= fy <= bb["y"] + bb["height"]):
                    child = fr
                    break
            if child is None:
                return {"tag": "iframe", "name": info.get("src", ""), "frames": frames}
            frames.append(child.url.split("?")[0][:160])
            frame, fx, fy = child, fx - bx, fy - by
        return {"tag": "", "name": "", "frames": frames}

    def locate(self, task_id: str, ref: str):
        ref = str(ref).strip().strip("[]")
        m = re.fullmatch(r"(f\d+)?e\d+", ref)
        if not m:
            raise HTTPException(400, f"无效的 ref: {ref}（请使用快照中的 [e12] 之类的 ref）")
        prefix = m.group(1) or ""
        fmap = self.frame_maps.get(task_id or "default") or {}
        fr = fmap.get(prefix)
        if fr is None or fr.is_detached():
            raise HTTPException(409, "页面已变化，请先调用 browser_snapshot 获取新的 ref (stale ref, take a new snapshot)")
        return fr.locator(f'[data-persona-ref="{ref}"]').first

    async def describe(self, task_id: str, ref: str) -> dict:
        loc = self.locate(task_id, ref)
        try:
            info = await loc.evaluate("""el => ({
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || '',
                name: (el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) || el.innerText || (['submit','button','reset'].includes((el.type||'').toLowerCase()) ? el.value : '') || el.getAttribute('placeholder') || el.getAttribute('name') || el.getAttribute('title') || '').trim().slice(0,200),
                editable: !!el.isContentEditable,
                autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase().slice(0,40),
                input_type: (el.type || '').toLowerCase(),
                is_password: (el.type || '').toLowerCase() === 'password' || (el.getAttribute('autocomplete')||'').includes('password'),
                in_form: !!el.closest('form'),
                href: el.getAttribute('href') || ''
            })""", timeout=5000)
        except Exception as e:
            raise HTTPException(409, f"找不到元素 {ref}，请重新获取快照 (element not found): {str(e)[:100]}")
        page = await self.page_for(task_id)
        info["page_url"] = page.url
        try:
            info["page_title"] = await page.title()
        except Exception:
            info["page_title"] = ""
        return info

    async def focused(self, task_id: str) -> dict:
        page = await self.page_for(task_id)
        try:
            return await page.evaluate("""() => { const el = document.activeElement; if (!el || el === document.body) return {};
                return {tag: el.tagName.toLowerCase(), role: el.getAttribute('role')||'', name: (el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.name||'').slice(0,200),
                        input_type: (el.type||'').toLowerCase(), in_form: !!el.closest('form')}; }""")
        except Exception:
            return {}


broker = Broker()


@asynccontextmanager
async def lifespan(app):
    await broker.start()
    yield
    await broker.stop()


app = FastAPI(lifespan=lifespan, title="OMuse Browser Broker")


def auth(x_browser_token: str | None = Header(default=None)):
    if not token_ok(x_browser_token, TOKEN):
        raise HTTPException(401, "unauthorized")


@app.get("/health")
async def health():
    return {"ok": True, "mode": broker.mode, "headless": broker.headless}


# ================================================================== local PDF export
# A separate headless Chromium (not the user's browser profile), JavaScript off and every network request blocked:
# documents are printed on this machine and nothing in them can reach the internet while rendering.
PDF_MAX_SRC = 5_000_000
_pdf = {"browser": None, "lock": asyncio.Lock()}


def _ws(rel: str) -> str:
    p = os.path.realpath(os.path.join(WORKSPACE, str(rel or "").strip().lstrip("/")))
    if not p.startswith(WORKSPACE + os.sep) or os.sep + ".quarantine" in p:
        raise HTTPException(400, "路径必须在工作区内 (path must be inside the workspace)")
    return p


def _img_data(base_dir: str):
    import base64
    import mimetypes

    def resolve(src: str) -> str | None:
        if re.match(r"^[a-z]+:", src, re.I):
            return None   # remote images are not fetched (no network while printing)
        try:
            p = os.path.realpath(os.path.join(base_dir, src))
            if not p.startswith(WORKSPACE + os.sep) or not os.path.isfile(p) or os.path.getsize(p) > 10_000_000:
                return None
            mt = mimetypes.guess_type(p)[0] or ""
            if mt not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                return None
            with open(p, "rb") as f:
                return f"data:{mt};base64," + base64.b64encode(f.read()).decode()
        except OSError:
            return None
    return resolve


async def _print_pdf(doc: str) -> bytes:
    async with _pdf["lock"]:
        b = _pdf["browser"]
        if b is None or not b.is_connected():
            b = _pdf["browser"] = await broker.pw.chromium.launch(headless=True)
        ctx = await b.new_context(java_script_enabled=False, locale="zh-CN")
        try:
            async def block(route):
                if route.request.url.startswith("data:"):
                    await route.continue_()
                else:
                    await route.abort()
            await ctx.route("**/*", block)
            page = await ctx.new_page()
            await page.set_content(doc, wait_until="load", timeout=30000)
            footer = ('<div style="width:100%;font-size:8px;color:#9ca3af;text-align:center;font-family:sans-serif">'
                      '<span class="pageNumber"></span> / <span class="totalPages"></span></div>')
            return await page.pdf(format="A4", print_background=True, prefer_css_page_size=True, display_header_footer=True,
                                  header_template="<div></div>", footer_template=footer,
                                  margin={"top": "18mm", "bottom": "20mm", "left": "16mm", "right": "16mm"})
        finally:
            await ctx.close()


@app.post("/pdf", dependencies=[Depends(auth)])
async def make_pdf(req: Request):
    from app.common.mdhtml import md_to_html, page as html_page
    b = await req.json()
    src_rel, title = str(b.get("source") or ""), str(b.get("title") or "")
    base = WORKSPACE
    if src_rel:
        sp = _ws(src_rel)
        if not os.path.isfile(sp):
            raise HTTPException(404, f"文件不存在 file not found: {src_rel}")
        if os.path.getsize(sp) > PDF_MAX_SRC:
            raise HTTPException(400, "源文件太大 (source over 5 MB)")
        with open(sp, encoding="utf-8", errors="replace") as f:
            text = f.read()
        base = os.path.dirname(sp)
        ext = os.path.splitext(sp)[1].lower()
    else:
        text, ext = str(b.get("markdown") or ""), ".md"
        if not text.strip():
            raise HTTPException(400, "没有内容 (nothing to print)")
    if not title:
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip() if m else (os.path.splitext(os.path.basename(src_rel))[0] if src_rel else "Document")
    if ext in (".html", ".htm"):
        doc = text   # printed with JavaScript off and no network, so scripts / trackers in it do nothing
    elif ext in (".md", ".markdown", ".txt", ""):
        body = md_to_html(text, _img_data(base)) if ext != ".txt" else f"<pre>{__import__('html').escape(text)}</pre>"
        doc = html_page(title, body)
    else:
        raise HTTPException(400, f"不支持的格式 unsupported source type: {ext}（支持 .md .txt .html）")
    out_rel = str(b.get("output") or "")
    if not out_rel:
        out_rel = (os.path.splitext(src_rel)[0] if src_rel else "reports/" + re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", title)[:60]) + ".pdf"
    if not out_rel.lower().endswith(".pdf"):
        out_rel += ".pdf"
    op = _ws(out_rel)
    data = await _print_pdf(doc)
    os.makedirs(os.path.dirname(op), exist_ok=True)
    with open(op, "wb") as f:
        f.write(data)
    return {"path": os.path.relpath(op, WORKSPACE), "size": len(data), "title": title}


# ================================================================== agent API
@app.post("/agent/{action}", dependencies=[Depends(auth)])
async def agent_action(action: str, req: Request):
    body = await req.json()
    task_id = body.get("task_id") or "default"
    if action == "describe":
        return await broker.describe(task_id, body.get("ref", ""))
    if action == "focused":
        return await broker.focused(task_id)
    if action == "describe_at":
        return await broker.at(task_id, float(body.get("x", 0)), float(body.get("y", 0)))
    if action == "request_takeover":
        broker.requests[task_id] = {"task_id": task_id, "reason": str(body.get("reason", ""))[:300], "ts": time.time()}
        await broker.page_for(task_id)
        return {"ok": True, "status": "takeover_requested"}
    broker.check_agent()
    broker.requests.pop(task_id, None)   # the task is working again, so it is no longer waiting for the user
    if action in ("find", "look", "locate"):
        async with broker.lock:
            try:
                if action == "find":
                    return await broker.find(task_id, str(body.get("query", ""))[:200])
                if action == "locate":
                    region, mark = body.get("region"), body.get("mark")
                    if mark:
                        return await broker.grid(task_id, mark=mark)
                    if region:
                        return await broker.grid(task_id, region=region, cols=8, rows=6, labels="numbers")
                    return await broker.grid(task_id, cols=8, rows=6, labels="letters")
                return await broker.look(task_id)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(500, f"浏览器错误 browser error: {str(e)[:300]}")
    async with broker.lock:
        page = await broker.page_for(task_id)
        try:
            if action == "navigate":
                url = str(body.get("url", "")).strip()
                if url and "://" not in url:
                    url = "https://" + url
                ok, why = check_url(url)
                if not ok:
                    raise HTTPException(403, why)
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    broker.nav_status[task_id] = resp.status if resp else 0
                except Exception as e:
                    if "ERR_BLOCKED_BY_CLIENT" in str(e):
                        raise HTTPException(403, "该地址被安全策略拦截 (blocked by egress policy)")
                    raise HTTPException(502, f"打开页面失败 navigation failed: {str(e)[:200]}")
                await broker.settle(page, 1200)
            elif action == "snapshot":
                pass
            elif action == "click":
                loc = broker.locate(task_id, body.get("ref", ""))
                try:
                    await loc.click(timeout=10000)
                except Exception as e:
                    raise HTTPException(409, f"点击失败 click failed: {str(e)[:200]}")
                await broker.settle(page, 1200)
                page = await broker.page_for(task_id)
            elif action == "click_at":
                # visual click (browser_locate found the point): works for iframes, shadow DOM, canvas, unlabeled icons
                x, y = float(body.get("x", -1)), float(body.get("y", -1))
                if not (0 <= x <= VIEWPORT["width"] * 2 and 0 <= y <= VIEWPORT["height"] * 2):
                    raise HTTPException(400, "x/y must be viewport coordinates from browser_locate")
                await page.mouse.click(x, y)
                await broker.settle(page, 900)
                text = str(body.get("text") or "")
                if text:
                    await page.keyboard.type(text, delay=15)
                    if body.get("submit"):
                        await page.keyboard.press("Enter")
                        await broker.settle(page, 1500)
                page = await broker.page_for(task_id)
            elif action == "type":
                loc = broker.locate(task_id, body.get("ref", ""))
                text = str(body.get("text", ""))
                try:
                    try:
                        await loc.fill(text, timeout=8000)
                    except Exception:
                        await loc.click(timeout=5000)
                        await page.keyboard.type(text, delay=15)
                    if body.get("submit"):
                        await loc.press("Enter", timeout=5000)
                        await broker.settle(page, 1500)
                except Exception as e:
                    raise HTTPException(409, f"输入失败 type failed: {str(e)[:200]}")
            elif action == "select":
                loc = broker.locate(task_id, body.get("ref", ""))
                v = str(body.get("value", ""))
                try:
                    try:
                        await loc.select_option(label=v, timeout=5000)
                    except Exception:
                        await loc.select_option(value=v, timeout=5000)
                except Exception as e:
                    raise HTTPException(409, f"选择失败 select failed: {str(e)[:200]}")
                await broker.settle(page, 500)
            elif action == "press":
                key = str(body.get("key", "Enter"))
                if not re.fullmatch(r"[A-Za-z0-9+]{1,24}", key):
                    raise HTTPException(400, "invalid key")
                await page.keyboard.press(key)
                await broker.settle(page, 1000)
            elif action == "scroll":
                d = body.get("direction", "down")
                js = {"down": "window.scrollBy(0, window.innerHeight*0.85)", "up": "window.scrollBy(0, -window.innerHeight*0.85)",
                      "top": "window.scrollTo(0,0)", "bottom": "window.scrollTo(0, document.body.scrollHeight)"}.get(d)
                if not js:
                    raise HTTPException(400, "direction must be up/down/top/bottom")
                y0 = await page.evaluate("window.scrollY")
                await page.mouse.wheel(0, {"down": 700, "up": -700, "top": -100000, "bottom": 100000}[d])
                await asyncio.sleep(0.5)
                if await page.evaluate("window.scrollY") == y0:     # wheel went to an inner element (or nowhere)
                    await page.evaluate(js)
                    await asyncio.sleep(0.3)
            elif action == "back":
                await page.go_back(timeout=20000)
                await broker.settle(page, 800)
            elif action == "wait":
                secs = max(1, min(int(body.get("seconds", 5)), 120))
                await asyncio.sleep(secs)
            elif action == "upload":
                rel = str(body.get("path", ""))
                full = os.path.realpath(os.path.join(WORKSPACE, rel.lstrip("/")))
                if not full.startswith(WORKSPACE + os.sep) or not os.path.isfile(full):
                    raise HTTPException(400, "文件必须位于工作区内 (file must exist inside the workspace)")
                if os.sep + ".quarantine" + os.sep in full:
                    raise HTTPException(403, "隔离区文件不能上传 (quarantined file)")
                loc = broker.locate(task_id, body.get("ref", ""))
                await loc.set_input_files(full, timeout=10000)
                await broker.settle(page, 500)
            else:
                raise HTTPException(404, f"unknown action {action}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"浏览器错误 browser error: {str(e)[:300]}")
        near = action == "scroll" or bool(body.get("near"))
        snap = await broker.snapshot(task_id, body.get("max_chars", 12000), near=near)
        if action in ("navigate", "click", "snapshot", "back", "wait", "press"):
            try:
                blk = await page.main_frame.evaluate(BLOCK_JS, broker.nav_status.get(task_id, 0) if action == "navigate" else 0)
            except Exception:
                blk = None
            if blk:
                snap["blocked"] = blk
        return snap


@app.get("/agent/downloads", dependencies=[Depends(auth)])
async def downloads():
    return {"downloads": broker.downloads[-30:]}


# ================================================================== view + takeover API (user)
@app.get("/state", dependencies=[Depends(auth)])
async def state():
    p = broker.view_page()
    title = ""
    if p is not None:
        try:
            title = await p.title()
        except Exception:
            pass
    popup = p is not None and p in broker.parent
    return {"mode": broker.mode, "url": p.url if p is not None else "", "title": title, "view_task": broker.view_task,
            "popup": popup, "popup_opener_url": broker.parent[p].url if popup else "", "headless": broker.headless,
            "requested": broker.requested, "requests": broker.open_requests(), "takeover_task": broker.takeover_task,
            "tasks": [{"task_id": k, "url": v.url} for k, v in broker.pages.items() if not v.is_closed()],
            "viewport": VIEWPORT}


@app.get("/screenshot", dependencies=[Depends(auth)])
async def screenshot(task_id: str | None = None):
    p = broker.pages.get(task_id) if task_id else broker.view_page()
    if p is None or p.is_closed():
        p = broker.view_page()
    if p is None:
        return Response(status_code=204)
    try:
        img = await p.screenshot(type="jpeg", quality=60, timeout=8000)
    except Exception:
        return Response(status_code=204)
    return Response(img, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/user/view", dependencies=[Depends(auth)])
async def user_view(req: Request):
    body = await req.json()
    tid = body.get("task_id")
    if tid in broker.pages:
        broker.view_task = tid
    return await state()


@app.post("/user/takeover", dependencies=[Depends(auth)])
async def user_takeover(req: Request):
    body = await req.json()
    async with broker.lock:  # waits for any in-flight agent action to finish
        broker.mode = "user"
        broker.takeover_task = body.get("task_id") or (broker.requested or {}).get("task_id") or broker.view_task or "default"
        await broker.page_for(broker.takeover_task)  # the page the user drives is always a registered task page
    return await state()


@app.post("/user/release", dependencies=[Depends(auth)])
async def user_release():
    # only the request this takeover answered is cleared; other tasks' requests stay open (they still need the user)
    tid = broker.takeover_task
    req = broker.requests.pop(tid, None)
    if req is None and tid in ("", "default") and broker.requests:
        req = broker.requests.pop(broker.requested["task_id"])   # untargeted takeover: it answered the latest request
    released = {"task_id": tid, "requested": req}
    broker.mode = "agent"
    broker.takeover_task = ""
    return {"ok": True, "released": released}


@app.post("/user/input", dependencies=[Depends(auth)])
async def user_input(req: Request):
    if broker.mode != "user":
        raise HTTPException(409, "请先点击「接管」(take over first)")
    body = await req.json()
    async with broker.input_lock:  # user events are applied strictly in arrival order
        return await _user_input(body)


async def _user_input(body: dict):
    page = broker.view_page()
    if page is None:
        page = await broker.page_for(broker.takeover_task or "default")
    kind = body.get("type")
    x, y = float(body.get("x", 0)), float(body.get("y", 0))
    if kind == "click":
        await page.mouse.click(x, y)
    elif kind == "dblclick":
        await page.mouse.dblclick(x, y)
    elif kind == "wheel":
        await page.mouse.move(x, y)
        await page.mouse.wheel(float(body.get("dx", 0)), float(body.get("dy", 0)))
    elif kind == "text":
        await page.keyboard.type(str(body.get("text", ""))[:2000], delay=10)
    elif kind == "key":
        key = str(body.get("key", ""))
        if re.fullmatch(r"[A-Za-z0-9+]{1,30}", key):
            await page.keyboard.press(key)
    elif kind == "navigate":
        url = str(body.get("url", "")).strip()
        if url and "://" not in url:
            url = "https://" + url
        ok, why = check_url(url)
        if not ok:
            raise HTTPException(403, why)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            raise HTTPException(502, str(e)[:200])
    elif kind == "back":
        await page.go_back()
    elif kind == "reload":
        await page.reload()
    else:
        raise HTTPException(400, "unknown input type")
    await asyncio.sleep(0.15)
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_exc(req, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
