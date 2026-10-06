"""Email providers other than Gmail: server presets, Gmail-style search -> IMAP SEARCH, modified UTF-7 folder names,
and Microsoft (Outlook.com / Hotmail / Microsoft 365) OAuth 2.0 device-code sign-in.

Every provider except Outlook works with IMAP + SMTP and an app password / authorization code (授权码).
Microsoft turned off password sign-in for Outlook.com in Sept 2024, so Outlook uses OAuth 2.0 (XOAUTH2) with the
user's own app registration (client ID) — the same "bring your own key" model as Google Calendar.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from datetime import date, datetime, timedelta

import httpx

# ------------------------------------------------------------------ presets
# security: "ssl" (implicit TLS) | "starttls" | "none" (loopback hosts only)
PROVIDERS: dict[str, dict] = {
    "gmail": {"label": "Gmail", "imap": ("imap.gmail.com", 993, "ssl"), "smtp": ("smtp.gmail.com", 465, "ssl"),
              "auth": "password", "domains": ["gmail.com", "googlemail.com"],
              "help": "https://myaccount.google.com/apppasswords"},
    "outlook": {"label": "Outlook / Hotmail / Microsoft 365", "imap": ("outlook.office365.com", 993, "ssl"),
                "smtp": ("smtp-mail.outlook.com", 587, "starttls"), "auth": "oauth",
                "domains": ["outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.sg", "hotmail.co.uk", "live.cn",
                            "outlook.jp", "hotmail.fr", "live.com.sg"],
                "help": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"},
    "yahoo": {"label": "Yahoo Mail", "imap": ("imap.mail.yahoo.com", 993, "ssl"), "smtp": ("smtp.mail.yahoo.com", 465, "ssl"),
              "auth": "password", "domains": ["yahoo.com", "ymail.com", "rocketmail.com", "yahoo.com.sg", "yahoo.co.jp",
                                              "yahoo.co.uk", "yahoo.com.hk", "yahoo.com.tw"],
              "help": "https://login.yahoo.com/account/security"},
    "icloud": {"label": "iCloud Mail", "imap": ("imap.mail.me.com", 993, "ssl"), "smtp": ("smtp.mail.me.com", 587, "starttls"),
               "auth": "password", "domains": ["icloud.com", "me.com", "mac.com"], "help": "https://account.apple.com"},
    "qq": {"label": "QQ Mail / Foxmail", "imap": ("imap.qq.com", 993, "ssl"), "smtp": ("smtp.qq.com", 465, "ssl"),
           "auth": "password", "domains": ["qq.com", "foxmail.com", "vip.qq.com"], "help": "https://mail.qq.com"},
    "netease": {"label": "NetEase Mail 163 / 126 / yeah.net", "imap": ("imap.{domain}", 993, "ssl"), "smtp": ("smtp.{domain}", 465, "ssl"),
                "auth": "password", "domains": ["163.com", "126.com", "yeah.net"], "help": "https://mail.163.com"},
    "zoho": {"label": "Zoho Mail", "imap": ("imap.zoho.com", 993, "ssl"), "smtp": ("smtp.zoho.com", 465, "ssl"),
             "auth": "password", "domains": ["zoho.com", "zohomail.com"], "help": "https://accounts.zoho.com/home#security/app_password"},
    "aol": {"label": "AOL Mail", "imap": ("imap.aol.com", 993, "ssl"), "smtp": ("smtp.aol.com", 465, "ssl"),
            "auth": "password", "domains": ["aol.com"], "help": "https://login.aol.com/account/security"},
    "custom": {"label": "Other (IMAP / SMTP)", "imap": ("", 993, "ssl"), "smtp": ("", 465, "ssl"), "auth": "password",
               "domains": [], "help": ""},
}
SECURITIES = ("ssl", "starttls", "none")
LOOPBACK = re.compile(r"^(localhost|127\.\d+\.\d+\.\d+|::1|\[::1\])$", re.I)


def guess_provider(email_addr: str) -> str:
    dom = email_addr.rsplit("@", 1)[-1].lower().strip()
    for key, p in PROVIDERS.items():
        if dom in p["domains"]:
            return key
    return ""


def label(provider: str) -> str:
    return PROVIDERS.get(provider or "gmail", PROVIDERS["custom"])["label"]


def public_presets() -> list[dict]:
    """What the Connections page needs to show the provider picker (no secrets here)."""
    return [{"key": k, "label": p["label"], "auth": p["auth"], "domains": p["domains"], "help": p["help"],
             "imap": list(p["imap"]), "smtp": list(p["smtp"])} for k, p in PROVIDERS.items()]


def server_settings(provider: str, email_addr: str, custom: dict | None = None) -> dict:
    """Account server fields for a provider; for "custom" they come from the form (validated here)."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown email provider: {provider}")
    p = PROVIDERS[provider]
    dom = email_addr.rsplit("@", 1)[-1].lower()
    custom = custom or {}
    if provider == "custom":
        out = {"imap_host": str(custom.get("imap_host") or "").strip(), "imap_port": int(custom.get("imap_port") or 993),
               "imap_security": str(custom.get("imap_security") or "ssl"),
               "smtp_host": str(custom.get("smtp_host") or "").strip(), "smtp_port": int(custom.get("smtp_port") or 465),
               "smtp_security": str(custom.get("smtp_security") or "ssl")}
        if not out["imap_host"] or not out["smtp_host"]:
            raise ValueError("请填写 IMAP 和 SMTP 服务器地址 (IMAP / SMTP host required)")
        for k in ("imap_host", "smtp_host"):
            if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}|\[?::1\]?", out[k]):
                raise ValueError(f"服务器地址无效 invalid host: {out[k]}")
        for k in ("imap_security", "smtp_security"):
            if out[k] not in SECURITIES:
                raise ValueError(f"invalid {k}: {out[k]}")
            host = out[k.replace("security", "host")]
            if out[k] == "none" and not LOOPBACK.match(host):
                raise ValueError("不加密的连接只允许本机地址 (unencrypted only allowed for localhost)")
        for k in ("imap_port", "smtp_port"):
            if not 1 <= out[k] <= 65535:
                raise ValueError(f"invalid {k}")
    else:
        (ih, ip, isec), (sh, sp, ssec) = p["imap"], p["smtp"]
        out = {"imap_host": ih.format(domain=dom), "imap_port": ip, "imap_security": isec,
               "smtp_host": sh.format(domain=dom), "smtp_port": sp, "smtp_security": ssec}
        if provider == "outlook" and dom not in p["domains"]:
            out["smtp_host"] = "smtp.office365.com"  # Microsoft 365 work / school accounts
    out["username"] = str(custom.get("username") or "").strip() or email_addr
    return out


# ------------------------------------------------------------------ modified UTF-7 (RFC 3501 §5.1.3) folder names
def mutf7_decode(s: str) -> str:
    def dec(m):
        b64 = m.group(1)
        if not b64:
            return "&"
        b64 = b64.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            return base64.b64decode(b64).decode("utf-16-be")
        except (binascii.Error, UnicodeDecodeError):
            return m.group(0)
    return re.sub(r"&([A-Za-z0-9+,]*)-", dec, s)


def mutf7_encode(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append("&" + b + "-")
            buf.clear()
    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


# ------------------------------------------------------------------ Gmail-style query -> IMAP SEARCH
FOLDER_WORDS = {"inbox": "inbox", "sent": "sent", "draft": "drafts", "drafts": "drafts", "trash": "trash", "bin": "trash",
                "spam": "spam", "junk": "spam", "archive": "archive", "anywhere": "*", "all": "*"}
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_date(d: date) -> str:
    return f"{d.day}-{_MONTHS[d.month - 1]}-{d.year}"


def _rel_date(v: str, today: date) -> date | None:
    m = re.fullmatch(r"(\d+)([dwmy])", v.lower())
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2)
    days = {"d": 1, "w": 7, "m": 30, "y": 365}[u] * n
    return today - timedelta(days=days)


def _abs_date(v: str) -> date | None:
    v = v.strip()
    if re.fullmatch(r"\d{9,11}", v):  # unix seconds (Gmail accepts these)
        return datetime.utcfromtimestamp(int(v)).date()
    m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", v)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _tokens(q: str) -> list[str]:
    # keeps "quoted phrases", key:"quoted value", { } groups and ( ) as tokens
    return re.findall(r'-?[A-Za-z_]+:"[^"]*"|-?"[^"]*"|[{}()]|[^\s{}()]+', q)


def _unq(v: str) -> str:
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v


def _qs(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


_SIZE = re.compile(r"(\d+(?:\.\d+)?)([kmg]?)b?$", re.I)


def translate_query(query: str, today: date | None = None) -> dict:
    """Gmail search syntax -> {"folders": [...], "criteria": [str…] (ASCII), "text": [(field, value)…] (non-ASCII terms,
    one goes to the server as a UTF-8 literal, the rest are filtered locally), "notes": [...]}.

    Supported: in:/label:, from: to: cc: bcc: subject:, is:unread/read/starred/important/flagged, has:attachment,
    newer_than: older_than: after: before: (newer/older), larger: smaller:, category:promotions/social/updates/forums/
    primary (approximated by the List-Unsubscribe header), "phrases", plain words, -negation, OR and {a b}."""
    today = today or date.today()
    folders: list[str] = []
    notes: list[str] = []
    text: list[tuple[str, str, bool, int]] = []
    items: list[str] = []          # each item is a complete IMAP search key
    pending_or = False
    # OR-grouping for non-ASCII terms: the server only takes ASCII keys, so Chinese words are filtered locally.
    # Terms that share a group id are OR-ed by local_match; terms in different groups are AND-ed. Without this,
    # ("笔试" OR "在线测评") was filtered as "笔试" AND "在线测评" and matched almost nothing (2026-10-05 colleague bug).
    text_gid = 0
    last_text_gid: int | None = None
    last_added_text = False
    in_brace = False
    brace_gid: int | None = None

    def push(crit: str):
        nonlocal pending_or, last_added_text
        if pending_or and items:
            items[-1] = f"OR {items[-1]} {crit}"
            pending_or = False
        else:
            items.append(crit)
        last_added_text = False

    def add_text(key: str, val: str, neg: bool):
        nonlocal text_gid, last_text_gid, last_added_text, pending_or, brace_gid
        if in_brace:                                    # inside { … }: one OR group (allocated on first member)
            if brace_gid is None:
                text_gid += 1
                brace_gid = text_gid
            gid = brace_gid
        elif pending_or and last_added_text and last_text_gid is not None:  # "a OR b" between two non-ASCII terms
            gid = last_text_gid
        else:
            text_gid += 1
            gid = text_gid
        text.append((key, val, neg, gid))
        last_text_gid = gid
        last_added_text = True
        pending_or = False

    toks = _tokens(query or "")
    i = 0
    group: list[str] | None = None
    while i < len(toks):
        t = toks[i]
        i += 1
        if t == "{":
            group = []
            in_brace = True
            brace_gid = None
            continue
        if t == "}":
            if group:
                crit = group[0]
                for g in group[1:]:
                    crit = f"OR {crit} {g}"
                push(crit)
            group = None
            in_brace = False
            brace_gid = None
            continue
        if t in ("(", ")", "AND"):
            continue
        if t == "OR":
            pending_or = True
            continue
        neg = t.startswith("-") and len(t) > 1
        if neg:
            t = t[1:]
        crit = None
        m = re.match(r"([A-Za-z_]+):(.*)$", t)
        if m and not t.startswith('"'):
            key, val = m.group(1).lower(), _unq(m.group(2))
            if key in ("in", "label"):
                v = val.lower()
                if v in ("starred", "important"):
                    crit = "FLAGGED"
                elif neg:
                    continue  # -in:spam etc.: spam / trash are not searched unless asked for
                else:
                    folders.append(FOLDER_WORDS.get(v) or ("label:" + val))
                    continue
            elif key in ("from", "to", "cc", "bcc", "subject"):
                if not val:
                    continue
                if val.isascii():
                    crit = f"{key.upper()} {_qs(val)}"
                else:
                    add_text(key.upper(), val, neg)
                    continue
            elif key == "is":
                crit = {"unread": "UNSEEN", "read": "SEEN", "starred": "FLAGGED", "important": "FLAGGED",
                        "flagged": "FLAGGED", "answered": "ANSWERED", "replied": "ANSWERED"}.get(val.lower())
                if crit is None:
                    notes.append(f"is:{val} ignored")
                    continue
            elif key == "has":
                if val.lower() in ("attachment", "attachments"):
                    crit = 'HEADER Content-Type "multipart/mixed"'
                elif val.lower() in ("userlabels", "nouserlabels", "drive", "document", "spreadsheet", "presentation",
                                     "youtube", "yellow-star"):
                    notes.append(f"has:{val} ignored")
                    continue
                else:
                    continue
            elif key in ("newer_than", "older_than"):
                d = _rel_date(val, today)
                if not d:
                    notes.append(f"{key}:{val} ignored")
                    continue
                crit = f"SINCE {imap_date(d)}" if key == "newer_than" else f"BEFORE {imap_date(d)}"
            elif key in ("after", "before", "newer", "older"):
                d = _abs_date(val)
                if not d:
                    notes.append(f"{key}:{val} ignored")
                    continue
                crit = f"SINCE {imap_date(d)}" if key in ("after", "newer") else f"BEFORE {imap_date(d)}"
            elif key in ("larger", "smaller", "size"):
                sm = _SIZE.match(val)
                if not sm:
                    continue
                n = float(sm.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[sm.group(2).lower()]
                crit = f"{'SMALLER' if key == 'smaller' else 'LARGER'} {int(n)}"
            elif key == "category":
                v = val.lower()
                if v in ("promotions", "social", "updates", "forums"):
                    crit = 'HEADER List-Unsubscribe ""'
                elif v in ("primary", "personal"):
                    crit = 'NOT HEADER List-Unsubscribe ""'
                else:
                    continue
                notes.append("category:* approximated by the List-Unsubscribe header (bulk mail)")
            elif key in ("filename", "list", "deliveredto", "rfc822msgid"):
                if key == "list":
                    crit = f"HEADER List-Id {_qs(val)}"
                elif key == "deliveredto":
                    crit = f"HEADER Delivered-To {_qs(val)}" if val.isascii() else None
                elif key == "rfc822msgid":
                    crit = f"HEADER Message-ID {_qs(val)}"
                else:
                    crit = f"BODY {_qs(val)}" if val.isascii() else None
                if crit is None:
                    continue
            else:  # unknown operator: treat as a plain word
                val = t
                if val.isascii():
                    crit = f"TEXT {_qs(val)}"
                else:
                    add_text("TEXT", val, neg)
                    continue
        else:
            val = _unq(t)
            if not val:
                continue
            if val.isascii():
                crit = f"TEXT {_qs(val)}"
            else:
                add_text("TEXT", val, neg)
                continue
        if neg:
            crit = f"NOT {crit}"
        if group is not None:
            group.append(crit)
        else:
            push(crit)
    # de-duplicate folders, keep order
    seen, fl = set(), []
    for f in folders:
        if f not in seen:
            seen.add(f)
            fl.append(f)
    # no folder given: the inbox plus the archive folder (where "archive" moves mail to), like Gmail's default search
    return {"folders": fl or ["inbox", "archive"], "criteria": items, "text": text, "notes": list(dict.fromkeys(notes))}


def local_match(msg: dict, terms: list[tuple]) -> bool:
    """Filter for the non-ASCII terms the server did not get (subject / from / to / snippet, case-insensitive).

    Terms sharing a group id (4th element, set by translate_query for OR groups) match if ANY of them hits (OR);
    separate groups must all hit (AND); a negative term must NOT hit. A 3-element term (older callers) is its own
    AND group."""
    fields = {"FROM": msg.get("from", ""), "TO": msg.get("to", ""), "CC": msg.get("cc", ""), "BCC": "",
              "SUBJECT": msg.get("subject", "")}

    def hay(key):
        return fields.get(key) if key in fields else " ".join([msg.get("subject", ""), msg.get("from", ""),
                                                               msg.get("to", ""), msg.get("snippet", ""), msg.get("body", "")])
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for n, t in enumerate(terms):
        key, val, neg = t[0], t[1], t[2]
        if neg:                                             # every negative is its own AND constraint
            if val.lower() in (hay(key) or "").lower():
                return False
            continue
        gid = t[3] if len(t) > 3 else ("n%d" % n)
        groups[gid].append((key, val))
    for lst in groups.values():                             # each positive group: at least one member must hit
        if not any(v.lower() in (hay(k) or "").lower() for k, v in lst):
            return False
    return True


# ------------------------------------------------------------------ Microsoft OAuth 2.0 (device code flow)
MS_LOGIN = os.environ.get("MS_LOGIN_URL", "https://login.microsoftonline.com")
MS_SCOPES = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send"


class OAuthError(Exception):
    pass


def ms_tenant(email_addr: str) -> str:
    dom = email_addr.rsplit("@", 1)[-1].lower()
    return "consumers" if dom in PROVIDERS["outlook"]["domains"] else "organizations"


def _ms_err(r: httpx.Response) -> str:
    try:
        j = r.json()
        return f"{j.get('error', r.status_code)}: {(j.get('error_description') or '').splitlines()[0][:300]}"
    except (ValueError, json.JSONDecodeError):
        return f"HTTP {r.status_code}"


def ms_device_start(client_id: str, tenant: str) -> dict:
    if not re.fullmatch(r"[0-9a-fA-F\-]{36}", client_id or ""):
        raise OAuthError("应用程序(客户端) ID 格式不对，应是 36 位的 GUID (Application (client) ID must be a GUID)")
    r = httpx.post(f"{MS_LOGIN}/{tenant}/oauth2/v2.0/devicecode", data={"client_id": client_id, "scope": MS_SCOPES},
                   timeout=20)
    if r.status_code != 200:
        raise OAuthError(f"无法开始微软登录 (device code failed): {_ms_err(r)}")
    j = r.json()
    return {"device_code": j["device_code"], "user_code": j["user_code"],
            "verification_uri": j.get("verification_uri") or "https://microsoft.com/devicelogin",
            "expires_at": time.time() + int(j.get("expires_in") or 900), "interval": int(j.get("interval") or 5)}


def ms_device_poll(client_id: str, tenant: str, device_code: str) -> dict | None:
    """None while the user has not finished signing in; the token dict when done; OAuthError on failure."""
    r = httpx.post(f"{MS_LOGIN}/{tenant}/oauth2/v2.0/token", timeout=20, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": client_id, "device_code": device_code})
    if r.status_code == 200:
        return r.json()
    try:
        err = r.json().get("error", "")
    except ValueError:
        err = ""
    if err in ("authorization_pending", "slow_down"):
        return None
    if err == "expired_token":
        raise OAuthError("登录码已过期，请重新开始 (code expired)")
    if err in ("access_denied", "authorization_declined"):
        raise OAuthError("你在微软页面上拒绝了授权 (access denied)")
    raise OAuthError(f"微软登录失败 (sign-in failed): {_ms_err(r)}")


def ms_refresh(client_id: str, tenant: str, refresh_token: str) -> dict:
    r = httpx.post(f"{MS_LOGIN}/{tenant}/oauth2/v2.0/token", timeout=20, data={
        "grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token, "scope": MS_SCOPES})
    if r.status_code != 200:
        raise OAuthError(f"微软授权已失效，请在「连接」页重新登录 (refresh failed): {_ms_err(r)}")
    return r.json()


def xoauth2(user: str, token: str) -> str:
    return f"user={user}\x01auth=Bearer {token}\x01\x01"
