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
SOURCES["web.page"] = {"connector": "browser", "label": "网页变化 Web page change",
                       "params": {"url": "要监控的网页 page to watch",
                                  "mode": "change（内容有变化）| text（出现某段文字）| price_below（价格低于阈值）",
                                  "text": "mode=text 时要等待出现的文字", "threshold": "mode=price_below 时的价格阈值，如 15",
                                  "keyword": "可选：只看这个词附近的价格/文字 (e.g. the product name)"}}
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


# ------------------------------------------------------------------ web page watch (OpenMuse-style tracking)
import hashlib
import re

_REF = re.compile(r"\[(?:f\d+)?e\d+\]\s*")
_ARROW_URL = re.compile(r"\s→\s\S+")
_PRICE = re.compile(r"(?:S\$|US\$|HK\$|A\$|C\$|RM|SGD|USD|HKD|RMB|CNY|¥|￥|€|£|\$)\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
                    re.I)


def page_text(snapshot: str) -> str:
    """The words a person reads on the page: drop element refs and link targets so layout churn doesn't count."""
    lines = []
    for ln in (snapshot or "").splitlines():
        ln = _ARROW_URL.sub("", _REF.sub("", ln)).strip()
        if ln and not ln.startswith(("…[", "(showing the part")):
            lines.append(re.sub(r"\s+", " ", ln))
    return "\n".join(lines)


_CUR = r"(?:S\$|US\$|HK\$|A\$|C\$|RM|SGD|USD|HKD|RMB|CNY|¥|￥|€|£|\$)"
# shops often draw a price as separate pieces ("S$ 31 . 43", "S$31\n.43", Amazon's "S$ 31 43"): glue them back
_SPLIT = re.compile(r"(" + _CUR + r"\s?\d{1,3}(?:,\d{3})*|" + _CUR + r"\s?\d+)(?:\s+\.\s*|\s*\.\s+|\s+)(\d{2})(?![\d.,%])", re.I)
KEYWORD_WINDOW = 600


def _glue(text: str) -> str:
    return _SPLIT.sub(lambda m: m.group(1) + "." + m.group(2), text)


def price_hits(text: str, keyword: str = "") -> list[tuple[float, str]]:
    """(value, what was read) for the prices on the page. With a keyword: only THE price of that item — the first
    price after the first mention of the keyword that has one (later mentions are usually "related products")."""
    text = _glue(text)

    def hits(span: str, first_only: bool) -> list[tuple[float, str]]:
        out = []
        for m in _PRICE.finditer(span):
            if first_only and len(span) >= KEYWORD_WINDOW and m.end() >= len(span) - 3:
                break                                   # cut off at the window's edge: don't read "31.4" out of "31.43"
            try:
                out.append((float(m.group(1).replace(",", "")), m.group(0).strip()))
            except ValueError:
                continue
            if first_only:
                break
        return out

    if not keyword:
        return hits(text, False)
    low = text.lower()
    for m in re.finditer(re.escape(keyword.lower()), low):
        got = hits(text[m.start(): m.start() + KEYWORD_WINDOW], True)
        if got:
            return got
    return []


def prices(text: str, keyword: str = "") -> list[float]:
    return [v for v, _ in price_hits(text, keyword)]


def _around(text: str, needle: str, before: int = 70, after: int = 30) -> str:
    i = text.find(needle)
    if i < 0:
        return ""
    return re.sub(r"\s+", " ", text[max(0, i - before): i + len(needle) + after]).strip()


def evaluate(params: dict, text: str, title: str, url: str, cursor: dict | None) -> tuple[list, dict]:
    """Compare this observation with the previous one (the cursor). Only a *new* change / newly met condition fires,
    so the same alert never repeats; the first observation only saves the baseline."""
    mode = str(params.get("mode") or "change").strip().lower()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    h = hashlib.sha256(text.encode()).hexdigest()
    line_h = [hashlib.sha1(ln.encode()).hexdigest()[:12] for ln in lines]
    cur = {"hash": h, "lines": line_h[-600:], "title": title[:200]}
    prev = cursor or {}
    events: list = []
    if mode == "text":
        want = str(params.get("text") or "").strip()
        if not want:
            raise WatchError("mode=text 需要指定要等待的文字 (text required)")
        idx = text.lower().find(want.lower())
        met = idx >= 0
        cur["met"] = met
        cur["seen"] = ("已出现 present: " if met else "还没有 not yet: ") + want[:60]
        if met and cursor is not None and not prev.get("met"):
            events.append({"url": url, "title": title, "condition": f"text appeared: {want}",
                           "excerpt": text[max(0, idx - 120): idx + len(want) + 200]})
    elif mode == "price_below":
        try:
            limit = float(str(params.get("threshold") or "").replace(",", "").lstrip("$S"))
        except ValueError:
            raise WatchError("mode=price_below 需要数字阈值 threshold, e.g. 15")
        kw = str(params.get("keyword") or "").strip()
        got = price_hits(text, kw)
        if not got:
            other = sorted({v for v, _ in price_hits(text)})[:8]
            if kw:
                raise WatchError(f"页面上“{kw}”附近没找到价格 (no price found after '{kw}')"
                                 + (f"；页面上的其他价格 other prices on the page: {', '.join(f'{v:g}' for v in other)}" if other else
                                    "；整个页面都没有价格（可能缺货、需要选款式，或网站换了页面）no price anywhere on the page"))
            raise WatchError("页面上没找到价格（可能缺货、需要先选款式，或网站换了页面）no price found on the page")
        found = [v for v, _ in got]
        low = min(found)
        glued = _glue(text)
        read = min(got)[1]
        shown = re.sub(r"\s+", "", read)
        ctx = _around(glued, read, 60, 0)
        cur["seen"] = shown + (f" · …{ctx[:-len(read)].strip()[-60:]}" if kw and ctx.endswith(read) else "")
        cur["low"], cur["met"] = low, bool(low < limit)
        alerted = prev.get("alerted")
        if cur["met"] and cursor is not None and (not prev.get("met") or (alerted is not None and low < alerted)):
            cur["alerted"] = low
            events.append({"url": url, "title": title, "condition": f"price {low:g} < {limit:g}",
                           "prices_seen": sorted(set(found))[:10], "excerpt": _around(glued, read, 110, 0)})
        elif cur["met"]:
            cur["alerted"] = alerted if alerted is not None else low
    else:
        if cursor is not None and prev.get("hash") and prev["hash"] != h:
            old = set(prev.get("lines") or [])
            added = [ln for ln, lh in zip(lines, line_h) if lh not in old][:8]
            if added or len(line_h) != len(prev.get("lines") or []):
                events.append({"url": url, "title": title, "condition": "page changed",
                               "added_lines": [truncate_line(x) for x in added]})
    return events, cur


def truncate_line(s: str, n: int = 240) -> str:
    return s if len(s) <= n else s[:n] + "…"


async def poll_web(store, params: dict, cursor: dict | None) -> dict:
    from app.sentinel.actions import ActionError, broker
    url = str(params.get("url") or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    ok, why = guard.check_url(url)
    if not ok:
        raise WatchError(why)
    dom = guard.domain_of(url)
    if dom and dom in set(store.connection("browser")["config"].get("blocked_domains") or []):
        raise WatchError(f"域名 {dom} 在黑名单中 (blocked domain)")
    tid = "watch-" + hashlib.sha1(url.encode()).hexdigest()[:10]
    try:
        snap = await broker("POST", "/agent/navigate", {"task_id": tid, "url": url, "max_chars": 40000}, timeout=90)
    except ActionError as e:
        raise WatchError(str(e)[:300])
    if snap.get("blocked"):
        raise WatchError(f"网站拦截了自动浏览器 (bot wall: {(snap['blocked'] or {}).get('detail', '')})")
    text, _ = guard.redact_secrets(page_text(str(snap.get("snapshot") or "")))
    events, cur = evaluate(params, text, str(snap.get("title") or ""), str(snap.get("url") or url), cursor)
    blob = "\n".join(str(e) for e in events)
    return {"events": events[:MAX_EVENTS], "cursor": cur, "injection": guard.scan_injection(blob) if events else []}
