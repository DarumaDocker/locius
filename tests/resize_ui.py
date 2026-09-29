"""The chat list column can be resized (drag / keyboard / double-click reset), the width is remembered, long titles
become readable, and phones keep the single-column layout."""
import asyncio, sys
import httpx
from playwright.async_api import async_playwright

B = "http://127.0.0.1:8080/"
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else info)
    if not cond:
        fails.append(name)


async def main():
    c = httpx.Client(timeout=30, trust_env=False)
    for a in c.get(B + "sentinel/api/approvals?status=pending").json()["approvals"]:   # no approval pop-up on load
        c.post(f"{B}sentinel/api/approvals/{a['id']}/resolve", json={"decision": "deny"}, headers={"X-Persona-UI": "1"})
    c.post(B + "api/chat", json={"message": "你好 这是一个很长很长的对话标题，用来测试对话列表能不能拉宽看到完整标题"},
           headers={"X-Persona-UI": "1"})
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1400, "height": 800})
        pg = await ctx.new_page()
        await pg.goto(B + "#chat")
        await pg.wait_for_selector(".conv-resizer")
        width = lambda: pg.evaluate("() => document.querySelector('.chat > .convlist').getBoundingClientRect().width")
        clipped = lambda: pg.evaluate("""() => { const t = [...document.querySelectorAll('.chat > .convlist .conv .ct')]
            .find(x => x.textContent.includes('很长很长')); return t ? t.scrollHeight > t.clientHeight + 1 : null; }""")
        w0 = await width()
        check("default width 240", abs(w0 - 240) < 2, w0)
        await pg.wait_for_timeout(800)
        check("long title is cut off at the default width (2 lines max)", await clipped() is True, await clipped())
        async def drag(dx):
            box = await pg.locator(".conv-resizer").bounding_box()
            x, y = box["x"] + box["width"] / 2, box["y"] + 300
            await pg.mouse.move(x, y); await pg.mouse.down(); await pg.mouse.move(x + dx, y, steps=8); await pg.mouse.up()
        await drag(200)
        w1 = await width()
        check("drag makes the list wider", abs(w1 - 440) < 6, w1)
        check("long title fully visible after widening", await clipped() is False)
        await pg.screenshot(path="/tmp/claude-0/-home-claude/656e5484-5983-5ed0-a14e-945a6f98bbb6/scratchpad/resize.png")
        await pg.reload(); await pg.wait_for_selector(".conv-resizer"); await pg.wait_for_timeout(300)
        check("width remembered after reload", abs(await width() - w1) < 3, await width())
        await drag(-400)
        check("cannot drag narrower than 180", abs(await width() - 180) < 3, await width())
        await pg.focus(".conv-resizer"); await pg.keyboard.press("ArrowRight"); await pg.keyboard.press("ArrowRight")
        check("arrow keys resize", abs(await width() - 228) < 3, await width())
        await pg.dblclick(".conv-resizer")
        check("double-click resets", abs(await width() - 240) < 3, await width())
        await drag(2000)
        check("capped at 560", abs(await width() - 560) < 3, await width())
        phone = await (await b.new_context(viewport={"width": 390, "height": 800})).new_page()
        await phone.goto(B + "#chat"); await phone.wait_for_selector("#chatInput")
        vis = await phone.evaluate("() => [getComputedStyle(document.querySelector('.chat > .convlist')).display, getComputedStyle(document.querySelector('.conv-resizer')).display, document.querySelector('.thread').getBoundingClientRect().width]")
        check("phone: list and handle hidden, thread full width", vis[0] == "none" and vis[1] == "none" and vis[2] > 380, vis)
        await b.close()
    print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
    sys.exit(1 if fails else 0)

asyncio.run(main())
