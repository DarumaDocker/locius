"""Multiple Gmail accounts.

Accounts live in the "gmail" connection config: {"accounts": [{id, email, display_name, imap_host, smtp_host}],
"default": "g1"}; each account's App Password is its own vault secret (cred_gmail_<n> for id g<n>).
Message / thread ids of the first account (g1) stay bare numbers (backwards compatible); ids from other
accounts are prefixed, e.g. "g2:1877319910085372507", so the agent can pass them around without knowing
which mailbox they came from.
"""
from __future__ import annotations

import re

_ID = re.compile(r"^(g\d+):(\d{5,25})$")


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
        a = dict(a)
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
    clean = [{k: a.get(k, "") for k in ("id", "email", "display_name", "imap_host", "smtp_host")} for a in accs]
    cfg = {"accounts": clean}
    if default is not None:
        cfg["default"] = default
    dflt = default if default is not None else store.connection("gmail")["config"].get("default")
    prim = next((a for a in clean if a["id"] == dflt), clean[0] if clean else None)
    # keep the legacy single-account fields pointing at the default account
    cfg["email"] = prim["email"] if prim else ""
    cfg["display_name"] = prim.get("display_name", "") if prim else ""
    store.save_connection("gmail", cfg)


def save_account(store, email: str, app_password: str, display_name: str = "") -> dict:
    accs = [dict(a) for a in accounts(store)]
    acc = next((a for a in accs if a["email"].lower() == email.lower()), None)
    if acc is None:
        n = max([int(a["id"][1:]) for a in accs] + [0]) + 1
        acc = {"id": f"g{n}", "email": email, "display_name": display_name, "imap_host": "imap.gmail.com",
               "smtp_host": "smtp.gmail.com"}
        accs.append(acc)
    else:
        acc["display_name"] = display_name or acc.get("display_name", "")
    store.put_secret("gmail", {"app_password": app_password}, handle=handle(acc["id"]))
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
