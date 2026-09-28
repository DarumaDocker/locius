"""Google Calendar connector (official REST API v3, OAuth 2.0 with the user's own Google Cloud client).

Setup (Connections page): the user creates an OAuth client (type "Web application") in their own Google Cloud
project, enables the Calendar API, adds Locius's callback URL as a redirect URI and pastes the client id/secret.
"Connect with Google" runs the consent flow; the refresh token is stored in Sentinel's vault and never leaves it.
The agent only ever sees event data, never tokens.
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

GOOGLE_AUTH = os.environ.get("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
GOOGLE_TOKEN = os.environ.get("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
GCAL_API = os.environ.get("GCAL_API", "https://www.googleapis.com/calendar/v3")
GOOGLE_USERINFO = os.environ.get("GOOGLE_USERINFO_URL", "https://www.googleapis.com/oauth2/v3/userinfo")
SCOPES = "openid email https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly"
TIMEOUT = 30


class GCalError(Exception):
    pass


def auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    return GOOGLE_AUTH + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state})


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    r = httpx.post(GOOGLE_TOKEN, data={"code": code, "client_id": client_id, "client_secret": client_secret,
                                       "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=TIMEOUT)
    d = r.json() if r.content else {}
    if r.status_code != 200 or not d.get("access_token"):
        raise GCalError(f"Google 授权失败 (token exchange failed): {d.get('error_description') or d.get('error') or r.status_code}")
    if not d.get("refresh_token"):
        raise GCalError("Google 没有返回 refresh token：请在 myaccount.google.com/permissions 移除 Locius 的旧授权后再连接一次")
    return d


def _fmt_err(r: httpx.Response) -> str:
    try:
        e = r.json().get("error") or {}
        msg = e.get("message") if isinstance(e, dict) else str(e)
    except Exception:
        msg = r.text[:200]
    hint = {401: "授权已失效，请在「连接」页重新连接 Google 日历 (re-connect)",
            403: "没有权限：确认已启用 Google Calendar API、授权时勾选了日历权限 (API disabled or scope missing)",
            404: "找不到这个日历或日程 (not found)"}.get(r.status_code, "")
    return f"Google Calendar HTTP {r.status_code}: {msg}" + (f" — {hint}" if hint else "")


class GCal:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, tz: str = "UTC", on_token=None,
                 access_token: str = "", expires_at: float = 0):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.tz = tz or "UTC"
        self.on_token = on_token
        self.access_token, self.expires_at = access_token, expires_at
        self.c = httpx.Client(timeout=TIMEOUT)

    def close(self):
        self.c.close()

    # ------------------------------------------------------------ auth
    def _token(self) -> str:
        if self.access_token and time.time() < self.expires_at - 60:
            return self.access_token
        r = self.c.post(GOOGLE_TOKEN, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                            "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        d = r.json() if r.content else {}
        if r.status_code != 200 or not d.get("access_token"):
            err = d.get("error", "")
            if err == "invalid_grant":
                raise GCalError("Google 授权已过期或被撤销 (invalid_grant)。请在「连接」页重新连接 Google 日历。"
                                "如果每 7 天就过期一次：把 Google Cloud 里 OAuth 同意屏幕的发布状态改成「正式版 In production」。")
            raise GCalError(f"刷新 Google 令牌失败 (token refresh failed): {d.get('error_description') or err or r.status_code}")
        self.access_token = d["access_token"]
        self.expires_at = time.time() + int(d.get("expires_in") or 3600)
        if self.on_token:
            self.on_token(self.access_token, self.expires_at)
        return self.access_token

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self.c.request(method, GCAL_API + path, headers={"Authorization": f"Bearer {self._token()}"}, **kw)
        if r.status_code == 204:
            return {}
        if r.status_code >= 400:
            raise GCalError(_fmt_err(r))
        return r.json() if r.content else {}

    def userinfo(self) -> dict:
        r = self.c.get(GOOGLE_USERINFO, headers={"Authorization": f"Bearer {self._token()}"})
        return r.json() if r.status_code == 200 else {}

    def settings_tz(self) -> str:
        try:
            return self._req("GET", "/users/me/settings/timezone").get("value") or self.tz
        except GCalError:
            return self.tz

    def calendars(self) -> list[dict]:
        d = self._req("GET", "/users/me/calendarList", params={"maxResults": 50})
        return [{"id": c["id"], "name": c.get("summaryOverride") or c.get("summary", ""), "primary": bool(c.get("primary")),
                 "access": c.get("accessRole", "")} for c in d.get("items") or []]

    # ------------------------------------------------------------ time helpers
    def _zone(self, tz: str | None = None) -> ZoneInfo:
        try:
            return ZoneInfo(tz or self.tz)
        except Exception:
            return ZoneInfo("UTC")

    def parse_time(self, value, tz: str | None = None) -> datetime:
        """'2026-10-03 19:00', '2026-10-03T19:00', ISO with offset/Z, or a date → aware datetime in the calendar tz."""
        s = str(value or "").strip()
        if not s:
            raise GCalError("缺少时间 (missing time)")
        s = s.replace("Z", "+00:00")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            s += "T00:00"
        s = s.replace(" ", "T", 1)
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            raise GCalError(f"看不懂的时间格式 (bad time): {value!r}；请用 2026-10-03 19:00 这种格式")
        if d.tzinfo is None:
            d = d.replace(tzinfo=self._zone(tz))
        return d

    def _when(self, ev_time: dict) -> str:
        if "date" in ev_time:
            return ev_time["date"]
        try:
            return datetime.fromisoformat(ev_time["dateTime"].replace("Z", "+00:00")).astimezone(self._zone()).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ev_time.get("dateTime", "")

    def _event(self, e: dict) -> dict:
        conf = ""
        for ep in (e.get("conferenceData") or {}).get("entryPoints") or []:
            if ep.get("entryPointType") == "video":
                conf = ep.get("uri", "")
        return {"id": e.get("id"), "title": e.get("summary", "(无标题 no title)"), "start": self._when(e.get("start") or {}),
                "end": self._when(e.get("end") or {}), "all_day": "date" in (e.get("start") or {}),
                "location": e.get("location", ""), "status": e.get("status", ""),
                "organizer": (e.get("organizer") or {}).get("email", ""),
                "attendees": [f"{a.get('email', '')}{' (' + a['responseStatus'] + ')' if a.get('responseStatus') else ''}"
                              for a in (e.get("attendees") or [])[:30]],
                "meeting_link": conf or e.get("hangoutLink", ""), "description": (e.get("description") or "")[:1500],
                "link": e.get("htmlLink", "")}

    # ------------------------------------------------------------ read
    def list_events(self, time_min=None, time_max=None, query: str = "", calendar_id: str = "primary",
                    max_results: int = 25) -> dict:
        z = self._zone()
        start = self.parse_time(time_min) if time_min else datetime.now(z)
        end = self.parse_time(time_max) if time_max else start + timedelta(days=7)
        if end <= start:
            end = start + timedelta(days=1)
        params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime",
                  "maxResults": max(1, min(int(max_results or 25), 100)), "timeZone": self.tz}
        if query:
            params["q"] = query
        d = self._req("GET", f"/calendars/{_cid(calendar_id)}/events", params=params)
        return {"calendar": calendar_id or "primary", "time_zone": self.tz,
                "range": f"{start.astimezone(z):%Y-%m-%d %H:%M} → {end.astimezone(z):%Y-%m-%d %H:%M}",
                "events": [self._event(e) for e in d.get("items") or [] if e.get("status") != "cancelled"]}

    def free_slots(self, time_min, time_max, duration_minutes: int = 30, day_start: str = "09:00", day_end: str = "18:00",
                   calendar_id: str = "primary") -> dict:
        z = self._zone()
        start, end = self.parse_time(time_min), self.parse_time(time_max)
        if (end - start).days > 31:
            raise GCalError("时间范围太长，最多 31 天 (range too long)")
        d = self._req("POST", "/freeBusy", json={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": self.tz,
                                                  "items": [{"id": calendar_id or "primary"}]})
        cal = (d.get("calendars") or {}).get(calendar_id or "primary") or {}
        if cal.get("errors"):
            raise GCalError(f"无法读取忙闲 (freeBusy error): {cal['errors']}")
        busy = sorted((datetime.fromisoformat(b["start"].replace("Z", "+00:00")), datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
                      for b in cal.get("busy") or [])
        dur = timedelta(minutes=max(5, int(duration_minutes or 30)))
        h1, m1 = (int(x) for x in (day_start or "09:00").split(":"))
        h2, m2 = (int(x) for x in (day_end or "18:00").split(":"))
        slots = []
        day = start.astimezone(z).date()
        while day <= end.astimezone(z).date() and len(slots) < 60:
            ws = max(datetime(day.year, day.month, day.day, h1, m1, tzinfo=z), start)
            we = min(datetime(day.year, day.month, day.day, h2, m2, tzinfo=z), end)
            cur = ws
            for b0, b1 in busy:
                if b1 <= cur or b0 >= we:
                    continue
                if b0 - cur >= dur:
                    slots.append((cur, b0))
                cur = max(cur, b1)
            if we - cur >= dur:
                slots.append((cur, we))
            day += timedelta(days=1)
        return {"time_zone": self.tz, "duration_minutes": int(dur.total_seconds() // 60), "working_hours": f"{day_start}-{day_end}",
                "busy_count": len(busy),
                "free": [f"{a.astimezone(z):%Y-%m-%d %a %H:%M}–{b.astimezone(z):%H:%M}" for a, b in slots]}

    def get_event(self, event_id: str, calendar_id: str = "primary") -> dict:
        return self._event(self._req("GET", f"/calendars/{_cid(calendar_id)}/events/{_eid(event_id)}"))

    # ------------------------------------------------------------ write
    def _times(self, start, end, all_day: bool, tz: str | None) -> tuple[dict, dict]:
        if all_day:
            d0 = self.parse_time(start, tz).date()
            d1 = self.parse_time(end, tz).date() if end else d0
            if d1 <= d0:
                d1 = d0 + timedelta(days=1)
            return {"date": d0.isoformat()}, {"date": d1.isoformat()}
        s = self.parse_time(start, tz)
        e = self.parse_time(end, tz) if end else s + timedelta(hours=1)
        if e <= s:
            raise GCalError("结束时间必须晚于开始时间 (end must be after start)")
        zone = tz or self.tz
        return {"dateTime": s.isoformat(), "timeZone": zone}, {"dateTime": e.isoformat(), "timeZone": zone}

    def create_event(self, a: dict) -> dict:
        title = str(a.get("title") or a.get("summary") or "").strip()
        if not title:
            raise GCalError("缺少标题 (title required)")
        st, en = self._times(a.get("start"), a.get("end"), bool(a.get("all_day")), a.get("time_zone") or None)
        body = {"summary": title[:300], "start": st, "end": en}
        if a.get("location"):
            body["location"] = str(a["location"])[:500]
        if a.get("description"):
            body["description"] = str(a["description"])[:8000]
        att = attendees(a.get("attendees"))
        if att:
            body["attendees"] = [{"email": x} for x in att]
        if a.get("reminder_minutes") not in (None, ""):
            body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": int(a["reminder_minutes"])}]}
        e = self._req("POST", f"/calendars/{_cid(a.get('calendar_id'))}/events",
                      params={"sendUpdates": "all" if att else "none"}, json=body)
        return {"created": True, "event": self._event(e), "invites_sent_to": att}

    def update_event(self, a: dict) -> dict:
        eid = str(a.get("event_id") or "")
        if not eid:
            raise GCalError("缺少 event_id (use calendar_list_events to find it)")
        body: dict = {}
        if a.get("title"):
            body["summary"] = str(a["title"])[:300]
        if a.get("start") or a.get("end"):
            cur = self._req("GET", f"/calendars/{_cid(a.get('calendar_id'))}/events/{_eid(eid)}")
            all_day = "date" in (cur.get("start") or {})
            start = a.get("start") or (cur["start"].get("dateTime") or cur["start"].get("date"))
            end = a.get("end")
            if not end:  # keep the duration
                c0 = self.parse_time(cur["start"].get("dateTime") or cur["start"].get("date"))
                c1 = self.parse_time(cur["end"].get("dateTime") or cur["end"].get("date"))
                end = (self.parse_time(start) + (c1 - c0)).isoformat()
            body["start"], body["end"] = self._times(start, end, all_day, a.get("time_zone") or None)
        for k in ("location", "description"):
            if a.get(k) is not None and a.get(k) != "":
                body[k] = str(a[k])[:8000]
        att = attendees(a.get("attendees")) if a.get("attendees") is not None else None
        if att is not None:
            body["attendees"] = [{"email": x} for x in att]
        if not body:
            raise GCalError("没有要修改的内容 (nothing to change)")
        e = self._req("PATCH", f"/calendars/{_cid(a.get('calendar_id'))}/events/{_eid(eid)}",
                      params={"sendUpdates": "all" if a.get("notify_attendees", True) else "none"}, json=body)
        return {"updated": True, "event": self._event(e)}

    def delete_event(self, a: dict) -> dict:
        eid = str(a.get("event_id") or "")
        if not eid:
            raise GCalError("缺少 event_id")
        self._req("DELETE", f"/calendars/{_cid(a.get('calendar_id'))}/events/{_eid(eid)}",
                  params={"sendUpdates": "all" if a.get("notify_attendees", True) else "none"})
        return {"deleted": True, "event_id": eid}


def attendees(v) -> list[str]:
    if not v:
        return []
    items = v if isinstance(v, list) else re.split(r"[,;\s]+", str(v))
    out = []
    for x in items:
        x = str(x).strip().strip("<>").lower()
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", x) and x not in out:
            out.append(x)
    return out[:50]


def _cid(calendar_id) -> str:
    from urllib.parse import quote
    return quote(str(calendar_id or "primary"), safe="")


def _eid(event_id) -> str:
    e = str(event_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,1024}", e):
        raise GCalError(f"无效的 event_id: {e[:60]}")
    return e


def today_str(tz: str) -> str:
    try:
        return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
    except Exception:
        return date.today().isoformat()
