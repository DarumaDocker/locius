"""Event sources for triggers ("when X happens, do Y").

The runtime's scheduler polls Sentinel's /internal/watch with a source, its parameters and the cursor it
got last time. Sentinel (which holds the credentials) looks for new items and returns them plus a new
cursor. The first poll only sets the cursor, so existing backlog never fires a trigger.
Items written by Locius itself (its own Slack messages, its own Notion edits) are skipped to avoid loops.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from app.sentinel import guard, mailboxes

SOURCES = {
    "gmail.new_email": {"connector": "gmail", "label": "收到新邮件 New email",
                        "params": {"query": "可选 Gmail 搜索条件，如 from:boss@x.com 或 is:important",
                                   "account": "可选：只看某个邮箱"}},
    "slack.new_message": {"connector": "slack", "label": "Slack 新消息 New Slack message",
                          "params": {"channel": "频道，如 #sales", "keyword": "可选：包含关键词才触发",
                                     "mentions_only": "可选：只在 @我 时触发"}},
    "notion.db_changed": {"connector": "notion", "label": "Notion 数据库有新增/修改 Notion database changed",
                          "params": {"database_id": "数据库 ID 或链接"}},
}
MAX_EVENTS = 20


class WatchError(Exception):
    pass


def _gmail(store, params: dict, cursor: dict | None) -> tuple[list, dict]:
    from app.sentinel.actions import _tag, gmail_client
    acc = str(params.get("account") or "").strip()
    accounts = [a for a in mailboxes.ready_accounts(store) if not acc or a["email"].lower() == acc.lower() or a["id"] == acc]
    if not accounts:
        raise WatchError("Gmail 未连接或找不到指定邮箱 (mailbox not connected)")
    q = (str(params.get("query") or "").strip() or "in:inbox") + " newer_than:2d"
    seen = dict((cursor or {}).get("seen") or {})
    events = []
    for a in accounts:
        g = gmail_client(store, a["id"])
        msgs = [_tag(m, g) for m in g.search(q, 30)]
        old = set(seen.get(a["id"]) or [])
        if cursor is not None and a["id"] in seen:
            for m in msgs:
                if m["id"] not in old:
                    events.append({k: m.get(k) for k in ("id", "thread_id", "account", "from", "to", "subject", "date", "snippet",
                                                         "security_message") if m.get(k) is not None})
        seen[a["id"]] = (list(old) + [m["id"] for m in msgs if m["id"] not in old])[-500:]
    return events, {"seen": seen}


def _slack(store, params: dict, cursor: dict | None) -> tuple[list, dict]:
    from app.sentinel.actions import slack_client
    ch = str(params.get("channel") or "").strip()
    if not ch:
        raise WatchError("请指定要监听的 Slack 频道 (channel required)")
    conf = store.connection("slack")["config"]
    me = {conf.get("user_id"), conf.get("bot_id")} - {"", None}
    s = slack_client(store)
    try:
        info = s.resolve_channel(ch)
        oldest = (cursor or {}).get("ts")
        d = s._call("conversations.history", channel=info["id"], limit=1 if oldest is None else 50,
                    oldest=oldest or None)
        raw = sorted(d.get("messages", []), key=lambda m: float(m.get("ts", 0)))
        newest = max([oldest or "0"] + [m.get("ts", "0") for m in raw], key=float)
        if oldest is None:
            return [], {"ts": newest if raw else f"{time.time():.6f}"}
        kw = str(params.get("keyword") or "").strip().lower()
        only_me = str(params.get("mentions_only") or "").lower() in ("1", "true", "yes", "on")
        events = []
        for m in raw:
            if m.get("user") in me or (m.get("bot_id") and m.get("bot_id") in me) or m.get("subtype") in ("channel_join", "channel_leave"):
                continue
            if kw and kw not in (m.get("text") or "").lower():
                continue
            if only_me and not any(f"<@{u}>" in (m.get("text") or "") for u in me):
                continue
            events.append(s._msg(m, info))
        return events, {"ts": newest}
    finally:
        s.close()


def _notion(store, params: dict, cursor: dict | None) -> tuple[list, dict]:
    from app.sentinel.actions import notion_client
    from app.sentinel.notion import page_brief
    db = str(params.get("database_id") or "").strip()
    if not db:
        raise WatchError("请指定要监听的 Notion 数据库 (database_id required)")
    bot = store.connection("notion")["config"].get("bot_id", "")
    n = notion_client(store)
    try:
        since = (cursor or {}).get("since") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00.000Z")
        seen = dict((cursor or {}).get("seen") or {})
        title, rows = n.edited_since(db, since)
        events = []
        for p in rows:
            let = p.get("last_edited_time", "")
            if seen.get(p["id"]) == let:
                continue
            seen[p["id"]] = let
            if cursor is None or (bot and (p.get("last_edited_by") or {}).get("id") == bot):
                continue
            events.append(dict(page_brief(p), database=title,
                               change="new" if p.get("created_time") == let else "edited"))
        newest = max([since] + [p.get("last_edited_time", "") for p in rows])
        seen = {k: v for k, v in seen.items() if v >= newest}
        return events, {"since": newest, "seen": seen}
    finally:
        n.close()


def poll(store, source: str, params: dict, cursor: dict | None) -> dict:
    if source not in SOURCES:
        raise WatchError(f"未知的触发来源 unknown source: {source}")
    fn = {"gmail.new_email": _gmail, "slack.new_message": _slack, "notion.db_changed": _notion}[source]
    try:
        events, new_cursor = fn(store, params or {}, cursor)
    except WatchError:
        raise
    except Exception as e:  # connector errors (ActionError, GmailError, SlackError, NotionError…)
        raise WatchError(str(e)[:300])
    events = events[:MAX_EVENTS]
    text = "\n".join(str(e) for e in events)
    return {"events": events, "cursor": new_cursor, "injection": guard.scan_injection(text) if events else []}
