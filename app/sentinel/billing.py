"""Hosted installs: a "Manage subscription" link to the Stripe customer portal.

Enabled only when OMUSE_STRIPE_API_KEY, OMUSE_STRIPE_CUSTOMER_ID and OMUSE_STRIPE_SUBSCRIPTION_ID are all set (the
host that sells this box sets them). OMuse only opens the portal: what happens after the user cancels or renews is
handled by whoever receives Stripe's webhooks. The API key stays in Sentinel; the browser only ever gets the
short-lived portal URL.
"""
from __future__ import annotations

import os

import httpx

STRIPE_API = os.environ.get("STRIPE_API", "https://api.stripe.com")


class BillingError(Exception):
    pass


def config() -> dict | None:
    """{api_key, customer, subscription} when all three variables are set, else None (feature off)."""
    cfg = {k: (os.environ.get(env) or "").strip() for k, env in (("api_key", "OMUSE_STRIPE_API_KEY"),
                                                               ("customer", "OMUSE_STRIPE_CUSTOMER_ID"),
                                                               ("subscription", "OMUSE_STRIPE_SUBSCRIPTION_ID"))}
    return cfg if all(cfg.values()) else None


def _error(r: httpx.Response) -> str:
    try:
        return str((r.json().get("error") or {}).get("message") or "")[:300] or f"HTTP {r.status_code}"
    except ValueError:
        return f"HTTP {r.status_code}"


async def status() -> dict:
    """The subscription as Stripe has it: {status, cancel_at_period_end, current_period_end}. Raises BillingError."""
    cfg = config()
    if not cfg:
        raise BillingError("subscription management is not configured")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{STRIPE_API}/v1/subscriptions/{cfg['subscription']}", auth=(cfg["api_key"], ""))
    except httpx.HTTPError as e:
        raise BillingError(f"Stripe 无法访问 Stripe unreachable: {type(e).__name__}")
    if r.status_code >= 400:
        raise BillingError(f"Stripe: {_error(r)}")
    s = r.json()
    if s.get("customer") not in (None, cfg["customer"]):
        raise BillingError("subscription does not belong to the configured customer")
    items = ((s.get("items") or {}).get("data") or [{}])
    return {"status": str(s.get("status") or ""), "cancel_at_period_end": bool(s.get("cancel_at_period_end")),
            "current_period_end": s.get("current_period_end") or items[0].get("current_period_end")}


async def portal_url(return_url: str) -> str:
    """A fresh customer-portal session for the configured customer. Raises BillingError."""
    cfg = config()
    if not cfg:
        raise BillingError("subscription management is not configured")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{STRIPE_API}/v1/billing_portal/sessions", auth=(cfg["api_key"], ""),
                             data={"customer": cfg["customer"], "return_url": return_url})
    except httpx.HTTPError as e:
        raise BillingError(f"Stripe 无法访问 Stripe unreachable: {type(e).__name__}")
    if r.status_code >= 400:
        raise BillingError(f"Stripe: {_error(r)}")
    url = str(r.json().get("url") or "")
    if not url.startswith("https://") and not (STRIPE_API.startswith("http://") and url.startswith("http://")):
        raise BillingError("Stripe returned no portal URL")
    return url
