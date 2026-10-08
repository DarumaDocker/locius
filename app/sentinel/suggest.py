"""Fewer approvals over time (roadmap batch 3): when the user has approved the same low-risk action (same tool, same
destination) 3 times in 30 days, OMuse proposes a standing approval for it. Nothing changes until the user turns it on;
payments, card fills, phone calls, order cancels / returns and browser clicks are never proposed — those stay one by one.
"""
from __future__ import annotations

import time
from collections import defaultdict

from app.common.util import now_ts

NEED, WINDOW = 3, 30 * 86400
NEVER = {"purchase_confirm", "browser_fill_secret", "phone_call", "gmail_forward"}


def eligible(ap: dict) -> bool:
    tool = str(ap.get("tool") or "")
    reason = str(ap.get("reason") or "")
    if tool in NEVER or tool.startswith("browser_") or tool.startswith("mcp:"):
        return False
    return not ("spends money" in reason or "changes an order" in reason or "injection" in reason)


def _init(store):
    store.db.script("CREATE TABLE IF NOT EXISTS grant_suggestions (key TEXT PRIMARY KEY, status TEXT, updated_at REAL);")


def key(tool: str, dest: str) -> str:
    return f"{tool}|{dest}"


def suggestions(store, now: float | None = None) -> list[dict]:
    _init(store)
    now = now or now_ts()
    seen: dict[str, dict] = defaultdict(lambda: {"count": 0, "last": 0.0, "titles": []})
    for ap in store.approvals("resolved", 500):
        if ap["status"] != "approved" or (ap.get("resolved_at") or 0) < now - WINDOW or not eligible(ap):
            continue
        dest = str((ap.get("summary") or {}).get("destination") or "")
        k = key(ap["tool"], dest)
        s = seen[k]
        s.update(tool=ap["tool"], destination=dest)
        s["count"] += 1
        s["last"] = max(s["last"], ap.get("resolved_at") or 0)
        t = str((ap.get("summary") or {}).get("title") or "")
        if t and t not in s["titles"]:
            s["titles"].append(t)
    closed = {r["key"] for r in store.db.all("SELECT key FROM grant_suggestions WHERE status IN ('dismissed','accepted')")}
    granted = {key(g["tool"], (g.get("match") or {}).get("destination") or "") for g in store.active_grants()}
    granted_any = {g["tool"] for g in store.active_grants() if not (g.get("match") or {}).get("destination")}
    out = [{"key": k, **v, "title": (v["titles"] or [v["tool"]])[0]} for k, v in seen.items()
           if v["count"] >= NEED and k not in closed and k not in granted and v["tool"] not in granted_any]
    out.sort(key=lambda x: -x["count"])
    return out


def accept(store, k: str, days: float | None = 30) -> str:
    _init(store)
    tool, dest = k.split("|", 1)
    gid = store.add_grant(tool, "PERMANENT" if not days else "TIME_BOUND", None, {"destination": dest} if dest else {},
                          days * 86400 if days else None, note="你批准过 3 次以上后开启 (turned on from a suggestion)")
    store.db.execute("INSERT OR REPLACE INTO grant_suggestions(key, status, updated_at) VALUES (?,?,?)", (k, "accepted", time.time()))
    return gid


def dismiss(store, k: str):
    _init(store)
    store.db.execute("INSERT OR REPLACE INTO grant_suggestions(key, status, updated_at) VALUES (?,?,?)", (k, "dismissed", time.time()))


def just_reached(store, ap: dict) -> dict | None:
    """After an approval: the suggestion it just completed (to tell the user once)."""
    if not eligible(ap):
        return None
    dest = str((ap.get("summary") or {}).get("destination") or "")
    for s in suggestions(store):
        if s["key"] == key(ap["tool"], dest) and s["count"] == NEED:
            return s
    return None
