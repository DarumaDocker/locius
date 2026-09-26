"""Slack connector (Slack Web API with a bot token xoxb-… or a user token xoxp-…).

Bot token: the Slack app must be invited to the channels it should read (/invite @app).
User token: acts as the user (can also search); only the scopes the user granted work.
"""
from __future__ import annotations

import os
import re
import time

import httpx

SLACK_API = os.environ.get("SLACK_API", "https://slack.com/api")


class SlackError(Exception):
    pass


_HINTS = {
    "not_in_channel": "机器人还不在这个频道里：请在频道里输入 /invite @你的应用名 (invite the app to the channel)",
    "channel_not_found": "找不到这个频道，或者没有权限 (channel not found)",
    "missing_scope": "Slack 应用缺少权限范围 (missing OAuth scope)",
    "invalid_auth": "Slack 令牌无效 (invalid token)",
    "token_revoked": "Slack 令牌已被撤销 (token revoked)",
    "not_allowed_token_type": "这个操作需要用户令牌 xoxp- (needs a user token)",
    "ratelimited": "Slack 请求过于频繁，请稍后再试 (rate limited)",
}


class Slack:
    def __init__(self, token: str, timeout: float = 30.0):
        self.token = token
        self.c = httpx.Client(base_url=SLACK_API, timeout=timeout, headers={"Authorization": f"Bearer {token}"})
        self._users: dict[str, str] = {}
        self._chans: dict[str, dict] | None = None

    def close(self):
        self.c.close()

    def _call(self, method: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        for attempt in range(3):
            try:
                if method in ("chat.postMessage",):
                    r = self.c.post(f"/{method}", json=params, headers={"Content-Type": "application/json; charset=utf-8"})
                else:
                    r = self.c.post(f"/{method}", data=params)
            except httpx.HTTPError as e:
                raise SlackError(f"无法连接 Slack (network): {type(e).__name__}")
            if r.status_code == 429 and attempt < 2:
                time.sleep(min(float(r.headers.get("retry-after", "1") or 1), 5))
                continue
            try:
                data = r.json()
            except Exception:
                raise SlackError(f"Slack HTTP {r.status_code}")
            if not data.get("ok"):
                err = data.get("error", "unknown_error")
                extra = f" (needed: {data.get('needed')})" if data.get("needed") else ""
                raise SlackError(f"Slack: {_HINTS.get(err, err)}{extra}")
            return data
        raise SlackError(_HINTS["ratelimited"])

    # ---------------------------------------------------------------- identity / lookup
    def auth_test(self) -> dict:
        d = self._call("auth.test")
        return {"team": d.get("team", ""), "team_id": d.get("team_id", ""), "user": d.get("user", ""),
                "user_id": d.get("user_id", ""), "bot_id": d.get("bot_id", ""), "url": d.get("url", ""),
                "token_type": "user" if self.token.startswith("xoxp-") else "bot"}

    def user_name(self, uid: str) -> str:
        if not uid:
            return ""
        if uid not in self._users:
            try:
                u = self._call("users.info", user=uid).get("user") or {}
                p = u.get("profile") or {}
                self._users[uid] = p.get("display_name") or p.get("real_name") or u.get("name") or uid
            except SlackError:
                self._users[uid] = uid
        return self._users[uid]

    def channels(self, limit: int = 200) -> list[dict]:
        if self._chans is None:
            out, cursor = {}, None
            for _ in range(10):
                d = self._call("conversations.list", types="public_channel,private_channel,im,mpim", exclude_archived="true",
                               limit=200, cursor=cursor)
                for ch in d.get("channels", []):
                    name = ch.get("name") or (f"DM:{self.user_name(ch.get('user', ''))}" if ch.get("is_im") else ch.get("id"))
                    out[ch["id"]] = {"id": ch["id"], "name": name, "is_private": bool(ch.get("is_private")),
                                     "is_member": bool(ch.get("is_member", ch.get("is_im"))), "is_im": bool(ch.get("is_im")),
                                     "topic": ((ch.get("topic") or {}).get("value") or "")[:120]}
                cursor = (d.get("response_metadata") or {}).get("next_cursor")
                if not cursor or len(out) >= limit:
                    break
            self._chans = out
        return list(self._chans.values())[:limit]

    def resolve_channel(self, ref: str) -> dict:
        ref = str(ref or "").strip()
        if re.fullmatch(r"[CGD][A-Z0-9]{6,}", ref):
            known = next((c for c in self.channels() if c["id"] == ref), None)
            return known or {"id": ref, "name": ref}
        name = ref.lstrip("#").lower()
        for c in self.channels():
            if c["name"].lower() == name:
                return c
        raise SlackError(f"找不到频道「{ref}」(channel not found) — 用 slack_list_channels 查看可用频道")

    # ---------------------------------------------------------------- messages
    def _msg(self, m: dict, channel: dict) -> dict:
        text = m.get("text", "")
        text = re.sub(r"<@([UW][A-Z0-9]+)>", lambda x: "@" + self.user_name(x.group(1)), text)
        text = re.sub(r"<#([CG][A-Z0-9]+)\|?([^>]*)>", lambda x: "#" + (x.group(2) or x.group(1)), text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"[\2](\1)", text)
        return {"channel": channel.get("name", ""), "channel_id": channel.get("id", ""), "ts": m.get("ts", ""),
                "thread_ts": m.get("thread_ts", ""), "reply_count": m.get("reply_count", 0),
                "user": self.user_name(m.get("user", "")) if m.get("user") else (m.get("username") or m.get("bot_id", "")),
                "user_id": m.get("user", ""), "bot": bool(m.get("bot_id")),
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(m.get("ts", "0") or 0))), "text": text[:4000],
                "files": [f.get("name", "") for f in m.get("files") or []]}

    def history(self, ref: str, limit: int = 20, oldest: str = "") -> dict:
        ch = self.resolve_channel(ref)
        d = self._call("conversations.history", channel=ch["id"], limit=max(1, min(int(limit or 20), 100)), oldest=oldest or None)
        return {"channel": ch.get("name"), "channel_id": ch["id"], "messages": [self._msg(m, ch) for m in d.get("messages", [])]}

    def thread(self, ref: str, ts: str, limit: int = 50) -> dict:
        ch = self.resolve_channel(ref)
        d = self._call("conversations.replies", channel=ch["id"], ts=ts, limit=max(1, min(int(limit or 50), 200)))
        return {"channel": ch.get("name"), "channel_id": ch["id"], "messages": [self._msg(m, ch) for m in d.get("messages", [])]}

    def search(self, query: str, limit: int = 20) -> dict:
        if not self.token.startswith("xoxp-"):
            raise SlackError("搜索需要 Slack 用户令牌 (xoxp-, scope search:read)；机器人令牌不能搜索。可以改用 slack_read_channel")
        d = self._call("search.messages", query=query, count=max(1, min(int(limit or 20), 50)), sort="timestamp")
        out = []
        for m in (d.get("messages") or {}).get("matches", []):
            ch = m.get("channel") or {}
            out.append({**self._msg(m, {"id": ch.get("id", ""), "name": ch.get("name", "")}), "permalink": m.get("permalink", "")})
        return {"total": (d.get("messages") or {}).get("total", 0), "messages": out}

    def post(self, ref: str, text: str, thread_ts: str = "") -> dict:
        ch = self.resolve_channel(ref)
        d = self._call("chat.postMessage", channel=ch["id"], text=text[:39000], thread_ts=thread_ts or None,
                       unfurl_links=False)
        return {"sent": True, "channel": ch.get("name"), "channel_id": ch["id"], "ts": d.get("ts", "")}
