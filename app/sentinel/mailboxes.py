"""Multiple mailboxes (Gmail and other providers).

Accounts live in the "gmail" connection config (the historical name of the email connector):
{"accounts": [{id, email, display_name, provider, imap_host/port/security, smtp_host/port/security, username, auth}],
"default": "g1"}; each account's secret is its own vault entry (cred_gmail_<n> for id g<n>): {"app_password": …}
or, for Outlook, {"oauth": {client_id, tenant, refresh_token}}.
Message / thread ids of the first account (g1) stay bare numbers (backwards compatible); ids from other
accounts are prefixed, e.g. "g2:1877319910085372507", so the agent can pass them around without knowing
which mailbox they came from.
"""
from __future__ import annotations

import re

from app.sentinel.mailproviders import PROVIDERS

_ID = re.compile(r"^(g\d+):(\S{1,40})$")
FIELDS = ("id", "email", "display_name", "provider", "imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port",
          "smtp_security", "username", "auth")


def _defaults(a: dict) -> dict:
    """Fill in fields that older (Gmail-only) configs do not have."""
    a = dict(a)
    a.setdefault("provider", "gmail")
    p = PROVIDERS.get(a["provider"], PROVIDERS["custom"])
    dom = a.get("email", "").rsplit("@", 1)[-1].lower()
    a["imap_host"] = a.get("imap_host") or p["imap"][0].format(domain=dom)
    a["smtp_host"] = a.get("smtp_host") or p["smtp"][0].format(domain=dom)
    a["imap_port"] = int(a.get("imap_port") or p["imap"][1])
    a["smtp_port"] = int(a.get("smtp_port") or p["smtp"][1])
    a["imap_security"] = a.get("imap_security") or p["imap"][2]
    a["smtp_security"] = a.get("smtp_security") or p["smtp"][2]
    a["username"] = a.get("username") or a.get("email", "")
    a["auth"] = a.get("auth") or ("oauth" if p["auth"] == "oauth" else "password")
    return a


def handle(aid: str) -> str:
    return f"cred_gmail_{aid[1:]}"


def accounts(store) -> list[dict]:
    cfg = store.connection("gmail")["config"]
    accs = cfg.get("accounts")
    if accs is None:  # legacy single-account config
        accs = [{"id": "g1", "email": cfg["email"], "display_name": cfg.get("display_name", ""),
                 "imap_host": cfg.get("imap_host") or "imap.gmail.com", "smtp_host": cfg.get("smtp_host") or "smtp.gmail.com"}] \
            if cfg.get("email") else []
    out = []
    for a in accs:
        a = _defaults(a)
        a["ready"] = store.has_secret(handle(a["id"]))
        out.append(a)
    return out


def ready_accounts(store) -> list[dict]:
    return [a for a in accounts(store) if a["ready"]]


def default_id(store) -> str:
    accs = ready_accounts(store)
    want = store.connection("gmail")["config"].get("default")
    if want and any(a["id"] == want for a in accs):
        return want
    return accs[0]["id"] if accs else ""


def find(store, ref: str | None) -> dict | None:
    """Account by id ("g2"), email address, or None/"" for the default account."""
    accs = ready_accounts(store)
    if not ref:
        ref = default_id(store)
    ref = str(ref).strip().lower()
    for a in accs:
        if a["id"] == ref or a["email"].lower() == ref:
            return a
    return None


def split_id(mid: str) -> tuple[str, str]:
    mid = str(mid).strip()
    m = _ID.match(mid)
    return (m.group(1), m.group(2)) if m else ("g1", mid)


def make_id(aid: str, raw: str) -> str:
    return str(raw) if aid == "g1" else f"{aid}:{raw}"


def _write(store, accs: list[dict], default: str | None = None):
    clean = [{k: a.get(k, "") for k in FIELDS} for a in accs]
    cfg = {"accounts": clean}
    if default is not None:
        cfg["default"] = default
    dflt = default if default is not None else store.connection("gmail")["config"].get("default")
    prim = next((a for a in clean if a["id"] == dflt), clean[0] if clean else None)
    # keep the legacy single-account fields pointing at the default account
    cfg["email"] = prim["email"] if prim else ""
    cfg["display_name"] = prim.get("display_name", "") if prim else ""
    store.save_connection("gmail", cfg)


def save_account(store, email: str, app_password: str = "", display_name: str = "", provider: str = "gmail",
                 servers: dict | None = None, oauth: dict | None = None) -> dict:
    """Add a mailbox, or update it when the email address is already connected (same address = new password)."""
    accs = [dict(a) for a in accounts(store)]
    acc = next((a for a in accs if a["email"].lower() == email.lower()), None)
    if acc is None:
        n = max([int(a["id"][1:]) for a in accs] + [0]) + 1
        acc = {"id": f"g{n}", "email": email}
        accs.append(acc)
    acc["display_name"] = display_name or acc.get("display_name", "")
    acc["provider"] = provider
    for k in ("imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port", "smtp_security", "username"):
        acc.pop(k, None)
    acc.update(servers or {})
    acc["auth"] = "oauth" if oauth else "password"
    acc.update(_defaults(acc))
    secret = {"oauth": oauth} if oauth else {"app_password": app_password}
    store.put_secret("gmail", secret, handle=handle(acc["id"]))
    dflt = store.connection("gmail")["config"].get("default") or accs[0]["id"]  # first mailbox stays default
    if not any(a["id"] == dflt for a in accs):
        dflt = acc["id"]
    _write(store, accs, dflt)
    store.save_connection("gmail", enabled=True)
    return acc


def remove_account(store, aid: str) -> None:
    accs = [a for a in accounts(store) if a["id"] != aid]
    store.delete_secret(handle(aid))
    dflt = store.connection("gmail")["config"].get("default")
    if dflt == aid or not any(a["id"] == dflt for a in accs):
        dflt = accs[0]["id"] if accs else ""
    _write(store, accs, dflt)


def set_default(store, aid: str) -> None:
    accs = accounts(store)
    if not any(a["id"] == aid for a in accs):
        raise KeyError(aid)
    _write(store, accs, aid)
