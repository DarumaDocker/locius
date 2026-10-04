"""Faster calendar connections: the read-only iCal address (30 seconds) and the one-click OMuse Google sign-in
(fixed relay page -> this box's callback, PKCE, secret built in or added by a token broker)."""
import json
import sys
from urllib.parse import parse_qs, urlparse

import httpx

B = "http://127.0.0.1:8080"
S = B + "/sentinel/api"
F = "http://127.0.0.1:8094"
H = {"X-Persona-UI": "1"}
RT = {"X-Persona-Runtime": "rt-test"}
GM = "/tmp/claude-0/persona-test/google_managed.json"
c = httpx.Client(timeout=60, trust_env=False)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:1500])
    if not cond:
        fails.append(name)


def cal():
    return next(x for x in c.get(S + "/connections", headers=H).json()["connections"] if x["name"] == "calendar")


def act(tool, args, task="t-cal"):
    return c.post(B + "/internal/act", json={"tool": tool, "args": args, "task_id": task, "call_id": "x"}, headers=RT).json()


def tools():
    return {t["function"]["name"] for t in c.get(B + "/internal/catalog", headers=RT).json()["tools"]}


c.delete(S + "/connections/calendar/credential", headers=H)
open(GM, "w").write("{}")
# ------------------------------------------------------------------ iCal (read-only)
x = cal()
check("no OMuse Google client in this build -> the one-click button is hidden", x["managed_available"] is False, x)
r = c.post(S + "/connections/calendar/google/start", json={"origin": B}, headers=H)
check("one-click start refuses clearly when the build has no client", r.status_code == 400 and "iCal" in r.text, r.text)
r = c.post(S + "/connections/calendar/ical", json={"url": "ftp://x/y.ics"}, headers=H)
check("iCal: only https/webcal addresses", r.status_code == 400 and "https://" in r.text, r.text)
r = c.post(S + "/connections/calendar/ical", json={"url": F + "/ics/private-gone/basic.ics"}, headers=H)
check("iCal: a reset (404) address says so", r.status_code == 400 and "重新复制" in r.text, r.text)
r = c.post(S + "/connections/calendar/ical", json={"url": F + "/ics/notcal.ics"}, headers=H)
check("iCal: a page that isn't a calendar is refused", r.status_code == 400 and "VCALENDAR" in r.text, r.text)
r = c.post(S + "/connections/calendar/ical", json={"url": F + "/ics/private-ok/basic.ics", "time_zone": "America/Chicago"}, headers=H).json()
check("iCal: connects, reads the calendar name, time zone and events", r.get("ok") and r["name"] == "Lucas Lu"
      and r["time_zone"] == "Asia/Singapore" and r["events"] == 3, r)
x = cal()
check("iCal: connection shows mode ical, enabled, no secret in the view", x["mode"] == "ical" and x["enabled"] and x["has_credential"]
      and "private-ok" not in json.dumps(x), x)
t = tools()
check("iCal: the agent gets the read tools only", {"calendar_list_events", "calendar_free_slots"} <= t
      and not t & {"calendar_create_event", "calendar_update_event", "calendar_delete_event"}, sorted(x for x in t if "calendar" in x))
st = c.get(B + "/internal/catalog", headers=RT).json()["connections"]["calendar"]
check("iCal: catalog status says read-only", st["ready"] and st["read_only"] is True, st)
r = act("calendar_list_events", {"time_min": "2026-10-05", "time_max": "2026-10-20"})
evs = ((r.get("result") or {}).get("content") or {}).get("events") or []
titles = [(e["start"], e["title"]) for e in evs]
check("iCal: recurring events expanded (EXDATE skipped), all-day and UTC events in local time",
      r["status"] == "ok" and ("2026-10-05 10:00", "Weekly sync") in titles and ("2026-10-19 10:00", "Weekly sync") in titles
      and not any(s.startswith("2026-10-12") for s, _ in titles) and ("2026-10-06 14:00", "Dentist") in titles
      and ("2026-10-07", "Public holiday") in titles, (r.get("status"), titles))
check("iCal: event text is marked as untrusted, injection flagged", (r.get("result") or {}).get("trust") == "untrusted" and (r.get("result") or {}).get("injection_warning"),
      json.dumps(r)[:600])
r = act("calendar_free_slots", {"time_min": "2026-10-05 09:00", "time_max": "2026-10-05 18:00", "duration_minutes": 60})
check("iCal: free slots around the weekly sync", r["status"] == "ok" and "2026-10-05 Mon 11:00–18:00" in json.dumps(r, ensure_ascii=False), r)
r = act("calendar_create_event", {"title": "x", "start": "2026-10-08 10:00"})
check("iCal: writes are refused with the reason (no approval card)", r["status"] == "denied" and "只读" in r["reason"], r)
tst = c.post(S + "/connections/calendar/test", json={}, headers=H).json()
check("iCal: test button works", tst.get("ok") is True, tst)

# ------------------------------------------------------------------ one-click Google sign-in, secret built in
open(GM, "w").write(json.dumps({"client_id": "omuse-test.apps.googleusercontent.com", "client_secret": "GOCSPX-test-secret-123",
                                "relay": F + "/relay/callback"}))
x = cal()
check("with an OMuse client in the build, the one-click button shows", x["managed_available"] is True, x)
r = c.post(S + "/connections/calendar/google/start", json={"origin": B}, headers=H).json()
au = r.get("auth_url", "")
q = parse_qs(urlparse(au).query)
check("one-click: Google consent with OMuse's client, the fixed relay as redirect, PKCE S256, offline",
      au.startswith(F + "/g/auth?") and q["client_id"] == ["omuse-test.apps.googleusercontent.com"]
      and q["redirect_uri"] == [F + "/relay/callback"] and q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"], au)
page = c.get(au, follow_redirects=True)
check("one-click: Google -> relay -> this box's callback -> connected", page.status_code == 200 and "lucas@example.com" in page.text
      and "✓" in page.text and str(page.url).startswith(B + "/sentinel/api/connections/calendar/callback"), (page.status_code, page.text[:300]))
again = c.get(str(page.url))
check("one-click: the callback can't be replayed", "state mismatch" in again.text, again.text[:200])
x = cal()
check("one-click: mode google, read-write tools back", x["mode"] == "google" and x["enabled"]
      and "calendar_create_event" in tools(), x)
r = act("calendar_list_events", {})
check("one-click: calendar calls work (token refresh with the built-in secret)", r["status"] == "ok", r)

# a relay link pointing at another site is refused by the relay; a forged state is refused by the box
bad = c.get(F + "/relay/callback", params={"code": "c", "state": "eyJyIjoiaHR0cDovL2V2aWwudGVzdC94In0"})
check("relay refuses a return address that isn't an OMuse callback", bad.status_code == 400, bad.text)
forged = c.get(B + "/sentinel/api/connections/calendar/callback", params={"code": "x", "state": "nope"})
check("box refuses a state it didn't issue", "state mismatch" in forged.text, forged.text[:200])

# ------------------------------------------------------------------ one-click with a token broker (no secret in the app)
open(GM, "w").write(json.dumps({"client_id": "omuse-test.apps.googleusercontent.com", "relay": F + "/relay/callback",
                                "broker": F + "/broker"}))
c.delete(S + "/connections/calendar/credential", headers=H)
au = c.post(S + "/connections/calendar/google/start", json={"origin": B}, headers=H).json()["auth_url"]
page = c.get(au, follow_redirects=True)
check("broker: sign-in completes through the broker", page.status_code == 200 and "lucas@example.com" in page.text, page.text[:300])
r = act("calendar_list_events", {})
check("broker: calls work", r["status"] == "ok", r)
c.post(S + "/connections/calendar/test", json={}, headers=H)

# ------------------------------------------------------------------ switching back to iCal clears Google tokens
r = c.post(S + "/connections/calendar/ical", json={"url": F + "/ics/private-ok/basic.ics"}, headers=H).json()
check("switching to iCal works and is read-only again", r.get("ok") and cal()["mode"] == "ical" and "calendar_create_event" not in tools(), r)
c.delete(S + "/connections/calendar/credential", headers=H)
x = cal()
check("disconnect removes the iCal address", not x["has_credential"] and not x["enabled"] and x["mode"] == "", x)
open(GM, "w").write("{}")
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
