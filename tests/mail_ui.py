"""Connections page: email provider picker (help steps, fields per provider, auto-detect from the address) and
connecting a custom IMAP mailbox through the UI (needs Dovecot from tests/dovecot.conf + an SMTP-less test)."""
import asyncio, sys
import httpx
from playwright.async_api import async_playwright

B = "http://127.0.0.1:8080/"
fails = []
SHOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-0"


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else info)
    if not cond:
        fails.append(name)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await (await b.new_context(viewport={"width": 1300, "height": 1000})).new_page()
        await pg.goto(B + "#connections")
        sel = pg.locator("select[aria-label='邮箱服务商 Provider'], select[aria-label='Email provider']").first
        await sel.wait_for()
        opts = await sel.locator("option").all_text_contents()
        check("9 providers listed", len(opts) == 9 and "Gmail" in opts[0], opts)
        card = pg.locator(".card", has=sel)
        await sel.select_option("qq")
        txt = await card.inner_text()
        check("QQ: authorization code steps + label", ("授权码" in txt or "Authorization code" in txt) and "mail.qq.com" in txt, txt[:400])
        await sel.select_option("outlook")
        cid = card.locator("input[placeholder='00000000-0000-0000-0000-000000000000']")
        pw = card.locator("input[type=password]")
        check("Outlook: client id shown, password hidden", await cid.is_visible() and not await pw.is_visible())
        txt = await card.inner_text()
        check("Outlook: Entra steps + Microsoft button", "entra.microsoft.com" in txt and ("用微软账号登录" in txt or "Sign in with Microsoft" in txt), txt[:300])
        await pg.screenshot(path=f"{SHOT}/mail_outlook.png", full_page=True)
        await sel.select_option("custom")
        check("custom: server fields shown", await card.locator("input[placeholder='imap.example.com']").is_visible())
        em = card.locator("input[type=email]")
        await em.fill("someone@163.com"); await em.dispatch_event("change")
        check("auto-detect provider from address", await sel.input_value() == "netease")
        await sel.select_option("custom")
        await em.fill("tester@example.com"); await em.dispatch_event("change")
        await card.locator("input[placeholder='imap.example.com']").fill("127.0.0.1")
        nums = card.locator("input[type=number]")
        await nums.nth(0).fill("1143"); await nums.nth(1).fill("1025")
        secs = card.locator("select:not([aria-label])")
        await secs.nth(0).select_option("none"); await secs.nth(1).select_option("none")
        await card.locator("input[placeholder='smtp.example.com']").fill("127.0.0.1")
        await pw.fill("secretpass123")
        await card.locator("button.btn.primary").click()
        await pg.wait_for_selector("text=tester@example.com", timeout=20000)
        txt = await pg.locator(".card", has=pg.locator("text=tester@example.com")).first.inner_text()
        check("custom mailbox connected via UI, provider shown", "IMAP" in txt, txt[:300])
        await pg.screenshot(path=f"{SHOT}/mail_connected.png", full_page=True)
        await b.close()
    c = httpx.Client(timeout=30, trust_env=False)
    gm = next(x for x in c.get(B + "sentinel/api/connections").json()["connections"] if x["name"] == "gmail")
    for a in gm["accounts"]:
        if a["email"] == "tester@example.com":
            c.delete(B + f"sentinel/api/connections/gmail/accounts/{a['id']}", headers={"X-Persona-UI": "1"})
    print("\n%d failures" % len(fails), fails)
    sys.exit(1 if fails else 0)


asyncio.run(main())
