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


class Broker:
    def __init__(self):
        self.pw = None
        self.ctx = None
        self.pages: dict[str, object] = {}       # task_id -> page
        self.frame_maps: dict[str, dict] = {}    # task_id -> {prefix: frame}
        self.view_task: str = ""
        self.mode = "agent"                      # agent | user
        self.takeover_reason = ""
        self.takeover_task = ""
        self.requested = None                    # {"task_id","reason","ts"} when agent asked for help
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

    async def snapshot(self, task_id: str, max_chars=12000) -> dict:
        page = await self.page_for(task_id)
        await self.settle(page, 200)
        fmap = {}
        parts = []
        frames = [f for f in page.frames if not f.is_detached()]
        for i, fr in enumerate(frames[:8]):
            prefix = "" if i == 0 else f"f{i}"
            fmap[prefix] = fr
            try:
                txt = await fr.evaluate(SNAPSHOT_JS, prefix)
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
            body = body[:max_chars] + "\n…[快照已截断 snapshot truncated — use browser_scroll or a larger max_chars]"
        tabs = len([p for p in self.ctx.pages if not p.is_closed()])
        return {"url": page.url, "title": title, "snapshot": body, "truncated": truncated, "tabs": tabs}

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
                name: (el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) || el.innerText || el.value || el.getAttribute('placeholder') || el.getAttribute('title') || '').trim().slice(0,200),
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


app = FastAPI(lifespan=lifespan, title="Locius Browser Broker")


def auth(x_browser_token: str | None = Header(default=None)):
    if not token_ok(x_browser_token, TOKEN):
        raise HTTPException(401, "unauthorized")


@app.get("/health")
async def health():
    return {"ok": True, "mode": broker.mode, "headless": broker.headless}


# ================================================================== agent API
@app.post("/agent/{action}", dependencies=[Depends(auth)])
async def agent_action(action: str, req: Request):
    body = await req.json()
    task_id = body.get("task_id") or "default"
    if action == "describe":
        return await broker.describe(task_id, body.get("ref", ""))
    if action == "focused":
        return await broker.focused(task_id)
    if action == "request_takeover":
        broker.requested = {"task_id": task_id, "reason": str(body.get("reason", ""))[:300], "ts": time.time()}
        await broker.page_for(task_id)
        return {"ok": True, "status": "takeover_requested"}
    broker.check_agent()
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
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
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
                await page.mouse.wheel(0, {"down": 700, "up": -700, "top": -100000, "bottom": 100000}[d])
                await asyncio.sleep(0.5)
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
        snap = await broker.snapshot(task_id, body.get("max_chars", 12000))
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
            "requested": broker.requested, "takeover_task": broker.takeover_task,
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
    released = {"task_id": broker.takeover_task, "requested": broker.requested}
    broker.mode = "agent"
    broker.requested = None
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
