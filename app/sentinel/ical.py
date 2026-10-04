"""Read-only calendar from a private iCal (ICS) address — the 30-second way to connect a calendar.

Google Calendar → Settings → (your calendar) → "Secret address in iCal format" gives a URL like
https://calendar.google.com/calendar/ical/<id>/private-<key>/basic.ics. iCloud ("Public Calendar" link, webcal://)
and Outlook ("Publish calendar" ICS link) work the same way. The URL itself is the credential, so it is kept in the
vault (cred_calendar_1 = {"ical_url": ...}); the agent only sees events.

Same read interface as gcal.GCal (list_events / free_slots / get_event); writes raise GCalError (read-only).
Recurring events (RRULE / EXDATE / RECURRENCE-ID overrides) are expanded with dateutil.rrule.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from dateutil import rrule as _rrule

from app.sentinel.gcal import GCalError

MAX_BYTES = 8 * 1024 * 1024
CACHE_SECONDS = 300
READ_ONLY = ("这是只读的 iCal 日历连接，不能新建、修改或删除日程。要写入日程，请在「连接 → Google 日历」用 Google 登录连接 "
             "(read-only iCal connection: sign in with Google to add or change events)")
_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()

# Windows time-zone names Outlook puts in TZID
_WIN_TZ = {"China Standard Time": "Asia/Shanghai", "Singapore Standard Time": "Asia/Singapore", "Pacific Standard Time": "America/Los_Angeles",
           "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago", "Mountain Standard Time": "America/Denver",
           "Tokyo Standard Time": "Asia/Tokyo", "GMT Standard Time": "Europe/London", "W. Europe Standard Time": "Europe/Berlin",
           "Taipei Standard Time": "Asia/Taipei", "Korea Standard Time": "Asia/Seoul", "India Standard Time": "Asia/Kolkata",
           "AUS Eastern Standard Time": "Australia/Sydney", "UTC": "UTC", "Romance Standard Time": "Europe/Paris"}


def normalize_url(url: str) -> str:
    u = (url or "").strip()
    if u.lower().startswith("webcal://"):
        u = "https://" + u[9:]
    p = urlparse(u)
    allow_http = os.environ.get("ICAL_ALLOW_HTTP") == "1"
    if p.scheme not in ("https",) + (("http",) if allow_http else ()) or not p.hostname or p.username or p.password:
        raise GCalError("请粘贴以 https:// 或 webcal:// 开头的 iCal 地址 (paste an https:// or webcal:// iCal address)")
    return u


def label(url: str) -> str:
    """Short, non-secret description of the feed for the UI (host + calendar id, never the private key)."""
    p = urlparse(url)
    m = re.search(r"/calendar/ical/([^/]+)/", p.path)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    return p.hostname or "iCal"


def fetch(url: str, force: bool = False) -> str:
    with _lock:
        hit = _cache.get(url)
        if hit and not force and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "OMuse/ical"}) as c:
            with c.stream("GET", url) as r:
                if r.status_code in (401, 403, 404):
                    raise GCalError(f"iCal 地址无效或已被重置 (HTTP {r.status_code})：请在日历设置里重新复制私密地址")
                if r.status_code != 200:
                    raise GCalError(f"读取 iCal 失败 (HTTP {r.status_code})")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise GCalError("iCal 文件太大 (feed larger than 8 MB)")
    except httpx.HTTPError as e:
        raise GCalError(f"连不上 iCal 地址 (cannot fetch feed): {type(e).__name__}")
    text = bytes(buf).decode("utf-8", "replace")
    if "BEGIN:VCALENDAR" not in text[:2000]:
        raise GCalError("这个地址返回的不是 iCal 日历（应以 BEGIN:VCALENDAR 开头）(not an iCal feed)")
    with _lock:
        _cache[url] = (time.time(), text)
    return text


# ------------------------------------------------------------------ parsing
def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if ln[:1] in (" ", "\t") and out:
            out[-1] += ln[1:]
        elif ln:
            out.append(ln)
    return out


def _prop(line: str) -> tuple[str, dict, str]:
    # NAME;PARAM=a;PARAM2="b:c":value  (colons inside quoted params)
    i, q = 0, False
    while i < len(line):
        ch = line[i]
        if ch == '"':
            q = not q
        elif ch == ":" and not q:
            break
        i += 1
    head, value = line[:i], line[i + 1:]
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _text(v: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def _zone(name: str | None, fallback: ZoneInfo) -> ZoneInfo:
    if not name:
        return fallback
    name = _WIN_TZ.get(name, name)
    try:
        return ZoneInfo(name)
    except Exception:
        return fallback


def _dt(value: str, params: dict, tz: ZoneInfo):
    """-> (datetime aware, all_day). All-day dates become midnight in the calendar zone."""
    v = value.strip().split(",")[0]
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        d = datetime.strptime(v[:8], "%Y%m%d")
        return d.replace(tzinfo=tz), True
    if v.endswith("Z"):
        return datetime.strptime(v[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc), False
    return datetime.strptime(v[:15], "%Y%m%dT%H%M%S").replace(tzinfo=_zone(params.get("TZID"), tz)), False


_DUR = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


def _duration(v: str) -> timedelta:
    m = _DUR.fullmatch(v.strip())
    if not m:
        return timedelta(hours=1)
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    td = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
    return -td if m.group(1) == "-" else td


def parse(text: str, fallback_tz: str = "UTC") -> tuple[dict, list[dict]]:
    lines = _unfold(text)
    meta = {"name": "", "time_zone": ""}
    for ln in lines[:200]:
        n, _, v = _prop(ln)
        if n == "X-WR-CALNAME":
            meta["name"] = _text(v)[:120]
        elif n == "X-WR-TIMEZONE":
            meta["time_zone"] = v.strip()
    tz = _zone(meta["time_zone"] or fallback_tz, ZoneInfo("UTC"))
    events, cur, depth = [], None, 0
    for ln in lines:
        n, params, v = _prop(ln)
        if n == "BEGIN" and v.upper() == "VEVENT":
            cur, depth = {"exdates": [], "attendees": []}, 0
            continue
        if cur is None:
            continue
        if n == "BEGIN":                 # nested VALARM etc.
            depth += 1
            continue
        if n == "END":
            if v.upper() == "VEVENT" and depth == 0:
                if "start" in cur:
                    events.append(cur)
                cur = None
            else:
                depth -= 1
            continue
        if depth:
            continue
        try:
            if n == "DTSTART":
                cur["start"], cur["all_day"] = _dt(v, params, tz)
            elif n == "DTEND":
                cur["end"], _ = _dt(v, params, tz)
            elif n == "DURATION":
                cur["duration"] = _duration(v)
            elif n == "RRULE":
                cur["rrule"] = v.strip()
            elif n == "EXDATE":
                for x in v.split(","):
                    cur["exdates"].append(_dt(x, params, tz)[0])
            elif n == "RECURRENCE-ID":
                cur["recurrence_id"], _ = _dt(v, params, tz)
            elif n in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "STATUS", "URL", "TRANSP"):
                cur[n.lower()] = _text(v)
            elif n == "ORGANIZER":
                cur["organizer"] = re.sub(r"(?i)^mailto:", "", v)
            elif n == "ATTENDEE" and len(cur["attendees"]) < 30:
                a = re.sub(r"(?i)^mailto:", "", v)
                st = params.get("PARTSTAT", "")
                cur["attendees"].append(a + (f" ({st.lower()})" if st else ""))
        except ValueError:
            continue
    return meta, events


def _length(e: dict) -> timedelta:
    if e.get("end"):
        return max(e["end"] - e["start"], timedelta(0))
    if e.get("duration"):
        return e["duration"]
    return timedelta(days=1) if e.get("all_day") else timedelta(0)


def expand(events: list[dict], start: datetime, end: datetime, limit: int = 2000) -> list[tuple[datetime, datetime, dict]]:
    """Instances overlapping [start, end): (begin, finish, event)."""
    overrides = {(e.get("uid"), e["recurrence_id"].astimezone(timezone.utc)): e for e in events if e.get("recurrence_id")}
    out = []
    for e in events:
        if e.get("recurrence_id"):
            continue
        ln = _length(e)
        if not e.get("rrule"):
            starts = [e["start"]]
        else:
            rule = e["rrule"]
            dtstart = e["start"]
            try:
                r = _rrule.rrulestr("RRULE:" + rule, dtstart=dtstart, ignoretz=False)
            except (ValueError, TypeError):
                # UNTIL in UTC with a zoned DTSTART is fine; a floating mismatch is not — retry without UNTIL's Z
                try:
                    r = _rrule.rrulestr("RRULE:" + re.sub(r"(UNTIL=\d{8}(T\d{6})?)Z", r"\1", rule), dtstart=dtstart)
                except (ValueError, TypeError):
                    starts = [dtstart]
                    r = None
            if r is not None:
                ex = {x.astimezone(timezone.utc) for x in e["exdates"]}
                starts = [s for s in r.between(start - ln - timedelta(seconds=1), end, inc=True)
                          if s.astimezone(timezone.utc) not in ex][:limit]
        for s in starts:
            ov = overrides.get((e.get("uid"), s.astimezone(timezone.utc)))
            inst = ov or e
            b = ov["start"] if ov else s
            f = b + (_length(ov) if ov else ln)
            if f > start and b < end or (b == f and start <= b < end):
                out.append((b, f, inst))
    # moved overrides whose original slot lies outside the window
    seen = {id(x[2]) for x in out}
    for ov in overrides.values():
        if id(ov) not in seen:
            b, f = ov["start"], ov["start"] + _length(ov)
            if f > start and b < end:
                out.append((b, f, ov))
    out.sort(key=lambda x: x[0])
    return out


class ICalFeed:
    """Duck-types the read side of gcal.GCal."""
    read_only = True

    def __init__(self, url: str, tz: str = "UTC"):
        self.url = url
        self.tz = tz or "UTC"
        self._parsed = None

    def close(self):
        pass

    def _load(self):
        if self._parsed is None:
            meta, events = parse(fetch(self.url), self.tz)
            self._parsed = (meta, events)
        return self._parsed

    def info(self) -> dict:
        meta, events = self._load()
        return {"name": meta["name"], "time_zone": meta["time_zone"] or self.tz, "events": len(events)}

    def _z(self) -> ZoneInfo:
        return _zone(self.tz, ZoneInfo("UTC"))

    def parse_time(self, value) -> datetime:
        from app.sentinel.gcal import GCal
        return GCal.parse_time(self, value)   # same accepted formats; uses self._zone

    def _zone(self, tz=None):
        return _zone(tz or self.tz, ZoneInfo("UTC"))

    def _fmt(self, d: datetime, all_day: bool) -> str:
        return d.strftime("%Y-%m-%d") if all_day else d.astimezone(self._z()).strftime("%Y-%m-%d %H:%M")

    def _event(self, b: datetime, f: datetime, e: dict) -> dict:
        ad = bool(e.get("all_day"))
        return {"id": f"{e.get('uid', '')}@{b.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}", "title": e.get("summary") or "(无标题 no title)",
                "start": self._fmt(b, ad), "end": self._fmt(f, ad), "all_day": ad, "location": e.get("location", ""),
                "status": (e.get("status") or "").lower(), "organizer": e.get("organizer", ""), "attendees": e.get("attendees", []),
                "meeting_link": "", "description": (e.get("description") or "")[:1500], "link": e.get("url", "")}

    def list_events(self, time_min=None, time_max=None, query: str = "", calendar_id: str = "primary", max_results: int = 25) -> dict:
        z = self._z()
        start = self.parse_time(time_min) if time_min else datetime.now(z)
        end = self.parse_time(time_max) if time_max else start + timedelta(days=7)
        if end <= start:
            end = start + timedelta(days=1)
        _, events = self._load()
        q = (query or "").lower()
        items = []
        for b, f, e in expand(events, start, end):
            if (e.get("status") or "").upper() == "CANCELLED":
                continue
            if q and q not in " ".join(str(e.get(k, "")) for k in ("summary", "location", "description")).lower():
                continue
            items.append(self._event(b, f, e))
            if len(items) >= max(1, min(int(max_results or 25), 100)):
                break
        return {"calendar": "iCal (read-only)", "time_zone": self.tz,
                "range": f"{start.astimezone(z):%Y-%m-%d %H:%M} → {end.astimezone(z):%Y-%m-%d %H:%M}", "events": items}

    def free_slots(self, time_min, time_max, duration_minutes: int = 30, day_start: str = "09:00", day_end: str = "18:00",
                   calendar_id: str = "primary") -> dict:
        z = self._z()
        start, end = self.parse_time(time_min), self.parse_time(time_max)
        if (end - start).days > 31:
            raise GCalError("时间范围太长，最多 31 天 (range too long)")
        _, events = self._load()
        busy = sorted((b, f) for b, f, e in expand(events, start, end)
                      if (e.get("status") or "").upper() != "CANCELLED" and (e.get("transp") or "").upper() != "TRANSPARENT"
                      and not e.get("all_day"))
        dur = timedelta(minutes=max(5, int(duration_minutes or 30)))
        h1, m1 = (int(x) for x in (day_start or "09:00").split(":"))
        h2, m2 = (int(x) for x in (day_end or "18:00").split(":"))
        slots, day = [], start.astimezone(z).date()
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
                "busy_count": len(busy), "free": [f"{a.astimezone(z):%Y-%m-%d %a %H:%M}–{b.astimezone(z):%H:%M}" for a, b in slots]}

    def get_event(self, event_id: str, calendar_id: str = "primary") -> dict:
        uid, _, stamp = str(event_id).rpartition("@")
        _, events = self._load()
        if stamp:
            try:
                t = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                for b, f, e in expand(events, t - timedelta(days=1), t + timedelta(days=1)):
                    if e.get("uid") == uid and b.astimezone(timezone.utc) == t:
                        return self._event(b, f, e)
            except ValueError:
                pass
        for e in events:
            if e.get("uid") in (event_id, uid):
                return self._event(e["start"], e["start"] + _length(e), e)
        raise GCalError("找不到这个日程 (not found)")

    def create_event(self, a):
        raise GCalError(READ_ONLY)

    update_event = delete_event = create_event
