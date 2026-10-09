"""Hosted installs: with the three OMUSE_STRIPE_* variables set (run_local.sh sets them, against the fake Stripe in
fake_apps.py), Settings ends with a "Manage subscription" section whose button opens the Stripe customer portal."""
import asyncio
import sys

import httpx
from playwright.async_api import async_playwright

B = "http://127.0.0.1:8080"
F = "http://127.0.0.1:8094"
H = {"X-Persona-UI": "1"}
c = httpx.Client(timeout=60, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:1200])
    if not cond:
        fails.append(name)


c.post(F + "/_stripe", json={"status": "active", "cancel_at_period_end": False})
s = c.get(B + "/sentinel/api/subscription").json()
check("subscription is on, with Stripe's state", s.get("enabled") is True and s.get("status") == "active" and s.get("current_period_end"), s)
check("nothing secret in the answer", "sk_test" not in str(s) and "cus_" not in str(s), s)
check("portal needs the UI header (no cross-site posts)", c.post(B + "/sentinel/api/subscription/portal", json={}).status_code == 403)
before = len(c.get(F + "/_stripe").json()["sessions"])
p = c.post(B + "/sentinel/api/subscription/portal", json={}, headers=H)
sess = c.get(F + "/_stripe").json()["sessions"]
check("portal session created for the configured customer", p.status_code == 200 and "/stripe/portal/bps_test_" in p.json().get("url", "")
      and len(sess) == before + 1 and sess[-1]["customer"] == "cus_test123", (p.status_code, p.text[:300], sess[-1:]))
check("Stripe sends the user back to Settings", sess and sess[-1].get("return_url") == B + "/#settings", sess[-1:])
aud = c.get(B + "/sentinel/api/audit", params={"limit": 20}).text
check("opening the portal is in the audit log, without the key", "subscription.portal" in aud and "sk_test" not in aud, aud[:400])


async def ui():
    async with async_playwright() as pw:
        br = await pw.chromium.launch()
        pg = await br.new_page(locale="en-US")
        await pg.goto(B + "/#settings")
        card = pg.locator("#subscription")
        await card.wait_for(timeout=15000)
        check("Settings has a Manage subscription section", "Manage subscription" in await card.locator("h3").inner_text())
        last = await pg.evaluate("document.querySelector('#view').lastElementChild.id")
        check("it is the last section of Settings", last == "subscription", last)
        check("it says the subscription is active", "active" in (await pg.locator("#subscriptionState").inner_text()).lower())
        await card.get_by_role("button", name="Open subscription page").click()
        await pg.wait_for_url("**/stripe/portal/**", timeout=15000)
        check("the button opens the Stripe portal", "Billing portal" in await pg.content(), pg.url)

        c.post(F + "/_stripe", json={"cancel_at_period_end": True})
        await pg.goto(B + "/#settings")
        await pg.locator("#subscriptionState").wait_for(timeout=15000)
        check("a subscription set to cancel is shown as such", "set to cancel" in await pg.locator("#subscriptionState").inner_text())
        c.post(F + "/_stripe", json={"cancel_at_period_end": False})
        await br.close()


asyncio.run(ui())
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
