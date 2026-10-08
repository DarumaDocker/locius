"""Email connector over IMAP (read/organize/draft) and SMTP (send): Gmail and any other IMAP provider
(Outlook via OAuth 2.0, Yahoo, iCloud, QQ, 163/126, Zoho, AOL, custom servers) with an app password.

Runs only inside Sentinel. The credential is fetched from the vault per call and never returned to callers.
On Gmail, message ids are Gmail's X-GM-MSGID (decimal string) and thread ids X-GM-THRID; on other servers
they are "<folder key>-<uid>" (see _GEN_ID). The class keeps its historical name.
"""
from __future__ import annotations

import base64
import email
import email.utils
import html as htmllib
import imaplib
import re
import smtplib
import ssl
import time
import zlib
from email.header import decode_header, make_header
from email.message import EmailMessage
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.sentinel.guard import check_url, domain_of, is_security_message, redact_secrets
from app.sentinel.mailproviders import OAuthError, local_match, mutf7_decode, mutf7_encode, translate_query, xoauth2

TIMEOUT = 30
MAX_ATTACHMENT = 25 * 1024 * 1024


class GmailError(Exception):
    pass


def _unfold(v) -> str:
    """Collapse header folding (CR/LF + whitespace) into single spaces; EmailMessage rejects values with line breaks."""
    return re.sub(r"[\r\n]+[ \t]*", " ", str(v or "")).strip()


def _dec(value) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>", "\n", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = htmllib.unescape(h)
    h = re.sub(r"[ \t\r\f\v]+", " ", h)
    h = re.sub(r"\n\s*\n+", "\n\n", h)
    return h.strip()


def _body_text(msg: email.message.Message) -> tuple[str, list[dict]]:
    plain, html_parts, attachments = [], [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        ctype = part.get_content_type()
        filename = part.get_filename()
        if filename or "attachment" in disp:
            payload = part.get_payload(decode=True) or b""
            attachments.append({"filename": _dec(filename) or "(unnamed)", "type": ctype, "size": len(payload)})
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain":
            plain.append(text)
        elif ctype == "text/html":
            html_parts.append(text)
    body = "\n".join(plain).strip() or _html_to_text("\n".join(html_parts))
    # drop long quoted history ("> " lines) to save context
    lines = [ln for ln in body.splitlines() if not ln.startswith(">")]
    return "\n".join(lines).strip(), attachments


imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))
imaplib.Commands.setdefault("MOVE", ("SELECTED",))

# generic (non-Gmail) message ids are "<folder key>-<uid>": i = INBOX, s = Sent, d = Drafts, t = Trash, j = Junk,
# a = Archive, x<crc32> = any other folder. Gmail keeps its global X-GM-MSGID numbers.
SPECIAL = {"sent": "s", "drafts": "d", "trash": "t", "spam": "j", "archive": "a"}
_GEN_ID = re.compile(r"^(i|s|d|t|j|a|x[0-9a-f]{8})-(\d{1,12})$")
_FOLDER_NAMES = {
    "sent": ["sent", "sent items", "sent messages", "sent mail", "已发送", "已发送邮件", "已发邮件", "寄件備份", "送信済み"],
    "drafts": ["drafts", "draft", "草稿箱", "草稿", "下書き"],
    "trash": ["trash", "deleted", "deleted items", "deleted messages", "bin", "已删除", "已删除邮件", "垃圾桶", "ゴミ箱"],
    "spam": ["junk", "spam", "junk email", "junk e-mail", "bulk mail", "垃圾邮件", "迷惑メール"],
    "archive": ["archive", "archives", "归档", "存档", "アーカイブ"],
}
_FLAG_KEYS = (("\\All", "all"), ("\\Drafts", "drafts"), ("\\Sent", "sent"), ("\\Trash", "trash"), ("\\Junk", "spam"),
              ("\\Archive", "archive"), ("\\Important", "important"), ("\\Flagged", "starred"))
_STAR = {"\\STARRED", "STARRED", "\\FLAGGED", "FLAGGED", "\\IMPORTANT", "IMPORTANT"}
_LOGIN_HINT = {
    "gmail": "请确认 Google 账号已开启两步验证，并使用 16 位应用专用密码 (App Password)。",
    "outlook": "微软授权无效或已过期，请在「连接 Connections」页重新登录 Microsoft。",
    "qq": "请使用 QQ 邮箱「设置 → 账号」里开启 IMAP/SMTP 服务后生成的授权码，不是 QQ 密码。",
    "netease": "请使用网易邮箱「设置 → POP3/SMTP/IMAP」里开启 IMAP 服务后生成的授权码，不是登录密码。",
    "icloud": "请使用 Apple 账户里生成的 App 专用密码 (app-specific password)，并确认 iCloud 邮件已启用。",
    "yahoo": "请使用 Yahoo 账户安全设置里生成的应用密码 (app password)。",
    "aol": "请使用 AOL 账户安全设置里生成的应用密码 (app password)。",
    "zoho": "请在 Zoho 邮箱设置里开启 IMAP，并使用应用专用密码 (app-specific password)。",
    "custom": "请检查服务器地址、端口、加密方式、用户名和密码。",
}


def _crc(name: str) -> str:
    return "x%08x" % (zlib.crc32(name.encode()) & 0xFFFFFFFF)


class Gmail:
    """One mailbox. Gmail servers (X-GM-EXT-1) use Gmail's own ids, labels and search; every other IMAP server
    (Outlook, Yahoo, iCloud, QQ, 163, Zoho, custom…) uses folder+UID ids and Gmail-style queries translated to IMAP."""

    def __init__(self, email_addr: str, app_password: str = "", imap_host: str = "imap.gmail.com",
                 smtp_host: str = "smtp.gmail.com", display_name: str = "", *, provider: str = "gmail",
                 imap_port: int = 993, imap_security: str = "ssl", smtp_port: int = 465, smtp_security: str = "ssl",
                 username: str = "", token_fn=None):
        self.email = email_addr
        self.password = (app_password or "").replace(" ", "") if provider in ("gmail", "yahoo", "aol", "icloud") else (app_password or "")
        self.imap_host, self.imap_port, self.imap_security = imap_host, int(imap_port or 993), imap_security or "ssl"
        self.smtp_host, self.smtp_port, self.smtp_security = smtp_host, int(smtp_port or 465), smtp_security or "ssl"
        self.username = username or email_addr
        self.display_name = display_name
        self.provider = provider or "gmail"
        self.token_fn = token_fn
        self.gm: bool | None = None
        self._folders: dict | None = None

    # ------------------------------------------------------------ connection
    def _imap(self) -> imaplib.IMAP4:
        try:
            ctx = ssl.create_default_context()
            if self.imap_security == "ssl":
                m = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=ctx, timeout=TIMEOUT)
            else:
                m = imaplib.IMAP4(self.imap_host, self.imap_port, timeout=TIMEOUT)
                if self.imap_security == "starttls":
                    m.starttls(ssl_context=ctx)
            if self.token_fn:
                tok = self.token_fn()
                m.authenticate("XOAUTH2", lambda _: xoauth2(self.username, tok).encode())
            else:
                m.login(self.username, self.password)
        except imaplib.IMAP4.error as e:
            raise GmailError(f"IMAP 登录失败 (login failed): {e}. {_LOGIN_HINT.get(self.provider, _LOGIN_HINT['custom'])}")
        except OAuthError as e:
            raise GmailError(str(e))
        except (OSError, ssl.SSLError) as e:
            raise GmailError(f"无法连接 {self.imap_host}:{self.imap_port}: {e}")
        try:
            typ, dat = m.capability()
            if typ == "OK" and dat and dat[-1]:
                m.capabilities = tuple(dat[-1].decode(errors="replace").upper().split())
        except Exception:
            pass
        self.gm = "X-GM-EXT-1" in m.capabilities
        if "ID" in m.capabilities:  # NetEase (163/126) refuses SELECT until the client identifies itself (RFC 2971)
            try:
                m._simple_command("ID", '("name" "OMuse" "version" "1.0" "vendor" "OMuse")')
            except Exception:
                pass
        return m

    def folders(self, m) -> dict:
        if self._folders:
            return self._folders
        typ, data = m.list()
        out: dict = {"_names": [], "_display": {}}
        for raw in data or []:
            if isinstance(raw, tuple):  # name sent as a literal
                head, lit = raw[0].decode(errors="replace"), raw[1].decode(errors="replace")
                line = head.rsplit("{", 1)[0] + self._quote(lit)
            else:
                line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw or "")
            mm = re.match(r'\((?P<flags>[^)]*)\) (?:"(?P<sep>[^"]*)"|NIL) (?P<name>.+)$', line)
            if not mm:
                continue
            name = mm.group("name").strip()
            flags = mm.group("flags")
            if "\\Noselect" in flags or "\\NonExistent" in flags:
                continue
            disp = mutf7_decode(name.strip('"').replace('\\"', '"'))
            out["_display"][name] = disp
            for flag, key in _FLAG_KEYS:
                if flag.lower() in flags.lower():
                    out.setdefault(key, name)
            out["_names"].append(name)
        # servers without SPECIAL-USE flags (older QQ / 163 / custom): match well-known folder names
        for key, names in _FOLDER_NAMES.items():
            if key in out:
                continue
            for name in out["_names"]:
                d = out["_display"][name].lower()
                if d in names or d.split("/")[-1] in names or d.split(".")[-1] in names:
                    out[key] = name
                    break
        if self._gm():
            out.setdefault("all", '"[Gmail]/All Mail"')
            out.setdefault("drafts", '"[Gmail]/Drafts"')
        self._folders = out
        return out

    @staticmethod
    def _quote(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _select(self, m, folder: str, readonly=True):
        if getattr(m, "_om_sel", None) == (folder, readonly):
            return
        typ, data = m.select(folder, readonly=readonly)
        if typ != "OK":
            raise GmailError(f"无法打开文件夹 cannot open folder {self._fname(m, folder)}: {data}")
        m._om_sel = (folder, readonly)

    def _select_all(self, m, readonly=True):
        f = self.folders(m)["all"]
        try:
            self._select(m, f, readonly)
        except GmailError:
            raise GmailError("无法打开「所有邮件」文件夹 (cannot select All Mail)")

    def _fname(self, m, raw: str) -> str:
        if raw.upper() == "INBOX":
            return "INBOX"
        return (self._folders or {}).get("_display", {}).get(raw) or raw.strip('"')

    # generic folders <-> id keys
    def _fkey(self, m, raw: str) -> str:
        if raw.upper() == "INBOX":
            return "i"
        f = self.folders(m)
        for key, code in SPECIAL.items():
            if f.get(key) == raw:
                return code
        return _crc(raw)

    def _fraw(self, m, key: str) -> str:
        if key == "i":
            return "INBOX"
        f = self.folders(m)
        for name, code in SPECIAL.items():
            if code == key:
                if f.get(name):
                    return f[name]
                raise GmailError(f"这个邮箱没有{name}文件夹 (no {name} folder)")
        for raw in f["_names"]:
            if _crc(raw) == key:
                return raw
        raise GmailError("找不到邮件所在的文件夹 (folder no longer exists)")

    def _folder_by_name(self, m, name: str, create: bool = False) -> str | None:
        f = self.folders(m)
        want = name.strip().strip("/").lower()
        if want in ("inbox", "\\inbox"):
            return "INBOX"
        for key in ("sent", "drafts", "trash", "spam", "archive"):
            if want in (key, "\\" + key) and f.get(key):
                return f[key]
        for raw in f["_names"]:
            d = f["_display"][raw].lower()
            if d == want or d.split("/")[-1] == want or d.split(".")[-1] == want:
                return raw
        if not create:
            return None
        raw = self._quote(mutf7_encode(name.strip()))
        typ, data = m.create(raw)
        if typ != "OK":
            raise GmailError(f"无法创建文件夹 cannot create folder {name}: {data}")
        self._folders = None
        self.folders(m)
        return raw

    def _locate(self, m, msgid: str, readonly=True) -> str:
        """Select the message's folder and return its UID."""
        if self._gm():
            self._select_all(m, readonly)
            return self._uid_for_msgid(m, msgid)
        mm = _GEN_ID.match(str(msgid).strip())
        if not mm:
            raise GmailError(f"无效的 message_id: {msgid}")
        self._select(m, self._fraw(m, mm.group(1)), readonly)
        m._om_key = mm.group(1)
        return mm.group(2)

    def _uid_for_msgid(self, m, msgid: str) -> str:
        if not re.fullmatch(r"\d{5,25}", str(msgid)):
            raise GmailError(f"无效的 message_id: {msgid}")
        typ, data = m.uid("SEARCH", None, "X-GM-MSGID", str(msgid))
        uids = (data[0] or b"").split() if data else []
        if not uids:
            raise GmailError(f"找不到邮件 message not found: {msgid}")
        return uids[-1].decode()

    def _fetch_meta(self, m, uids: list[bytes], with_snippet=True) -> list[dict]:
        if not uids:
            return []
        uid_set = b",".join(uids).decode()
        gm = " X-GM-MSGID X-GM-THRID X-GM-LABELS" if self._gm() else ""
        parts = f"(UID{gm} FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)]"
        parts += " BODY.PEEK[TEXT]<0.3000>)" if with_snippet else ")"
        typ, data = m.uid("FETCH", uid_set, parts)
        key = getattr(m, "_om_key", "i")
        folder_name = self._fname(m, (getattr(m, "_om_sel", None) or ("INBOX",))[0])
        results: dict[str, dict] = {}
        cur = None
        for item in data or []:
            if isinstance(item, tuple):
                head = item[0].decode(errors="replace")
                uid = re.search(r"UID (\d+)", head)
                if uid:
                    cur = results.setdefault(uid.group(1), {"uid": uid.group(1)})
                    fl = re.search(r"FLAGS \(([^)]*)\)", head)
                    if self._gm():
                        gmid = re.search(r"X-GM-MSGID (\d+)", head)
                        th = re.search(r"X-GM-THRID (\d+)", head)
                        lb = re.search(r"X-GM-LABELS \(([^)]*)\)", head)
                        if gmid:
                            cur["id"] = gmid.group(1)
                        if th:
                            cur["thread_id"] = th.group(1)
                        if lb:
                            cur["labels"] = [x.strip('"').replace("\\\\", "\\") for x in re.findall(r'"[^"]*"|\S+', lb.group(1))]
                    else:
                        cur["id"] = cur["thread_id"] = f"{key}-{uid.group(1)}"
                        cur["labels"] = [folder_name] + (["\\Starred"] if fl and "\\Flagged" in fl.group(1) else [])
                    if fl:
                        cur["unread"] = "\\Seen" not in fl.group(1)
                        if "\\Draft" in fl.group(1):
                            cur["draft"] = True
                    if any(str(x).lower() in ("\\draft", "drafts", "[gmail]/drafts") for x in cur.get("labels") or []):
                        cur["draft"] = True
                if cur is None:
                    continue
                if "HEADER.FIELDS" in head:
                    hdr = email.message_from_bytes(item[1])
                    cur["from"] = _dec(hdr.get("From"))
                    cur["to"] = _dec(hdr.get("To"))
                    cur["cc"] = _dec(hdr.get("Cc"))
                    cur["subject"] = _dec(hdr.get("Subject"))
                    cur["date"] = hdr.get("Date", "")
                    cur["message_id_header"] = hdr.get("Message-ID", "")
                    cur["_unsub"] = parse_list_unsubscribe(hdr.get("List-Unsubscribe", ""), hdr.get("List-Unsubscribe-Post", ""))
                elif "BODY[TEXT]" in head:
                    cur["snippet"] = _snippet(item[1] or b"")
        out = []
        for r in results.values():
            sec = is_security_message(r.get("subject", ""))
            r["security_message"] = sec
            if sec:
                r["snippet"] = "[安全类邮件，内容已隐藏 security email hidden]"
            else:
                r["snippet"], _ = redact_secrets(r.get("snippet", ""))
            r.pop("uid", None)
            u = r.get("_unsub") or {}
            if u.get("method"):
                r["unsubscribe"] = u["method"]  # one-click | email | link — the URL itself stays inside Sentinel
            out.append(r)
        out.sort(key=lambda x: _date_key(x.get("date", "")), reverse=True)
        return out

    # ------------------------------------------------------------ read
    @staticmethod
    def public(msg: dict) -> dict:
        msg.pop("_unsub", None)
        if msg.get("draft"):
            msg["status"] = "草稿，从未发送 DRAFT - never sent (do not say the user replied / sent it)"
        return msg

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        max_results = max(1, min(int(max_results or 10), 100))
        query = dedupe_or_terms(query)
        m = self._imap()
        try:
            if not self._gm():
                return [self.public(x) for x in self._search_generic(m, query, max_results)]
            self._select_all(m)
            q = query.strip() or "in:inbox"
            if q.isascii():
                typ, data = m.uid("SEARCH", None, "X-GM-RAW", self._quote(q))
            else:
                m.literal = q.encode("utf-8")
                typ, data = m.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
            if typ != "OK":
                raise GmailError(f"搜索失败 search failed: {data}")
            uids = (data[0] or b"").split()
            uids = uids[-max_results:]
            return [self.public(x) for x in self._fetch_meta(m, uids)]
        finally:
            _logout(m)

    def _search_targets(self, m, wanted: list[str]) -> list[str]:
        f = self.folders(m)
        out: list[str] = []
        for w in wanted:
            if w == "*":
                cands = ["INBOX", f.get("archive"), f.get("sent")]
            elif w == "inbox":
                cands = ["INBOX"]
            elif w in SPECIAL:
                cands = [f.get(w)]
                if w != "archive" and not f.get(w):
                    raise GmailError(f"这个邮箱没有 {w} 文件夹 (no {w} folder)")
            else:  # label:<name> -> a folder
                name = w.split(":", 1)[1]
                raw = self._folder_by_name(m, name)
                if not raw:
                    names = ", ".join(f["_display"][n] for n in f["_names"][:30])
                    raise GmailError(f"找不到文件夹 folder not found: {name}. 现有文件夹 folders: {names}")
                cands = [raw]
            out += [c for c in cands if c and c not in out]
        return out

    def _search_generic(self, m, query: str, n: int) -> list[dict]:
        tq = translate_query(query)
        self.search_notes = tq["notes"]
        crit = tq["criteria"] or ["ALL"]
        texts = tq["text"]
        # Only pre-filter on the server by one positive term when the positive terms are pure AND. If any OR group has
        # more than one term (e.g. "笔试" OR "在线测评"), narrowing by a single member would drop messages that match a
        # sibling, so fetch by the date/folder criteria and OR-filter everything locally instead.
        from collections import Counter
        pos_gids = Counter(t[3] for t in texts if not t[2] and len(t) > 3)
        has_or = any(c > 1 for c in pos_gids.values())
        server_txt = None if has_or else next(((t[0], t[1]) for t in texts if not t[2]), None)
        local = [t for t in texts if not (server_txt is not None and not t[2] and (t[0], t[1]) == server_txt)]
        found: list[dict] = []
        for raw in self._search_targets(m, tq["folders"]):
            try:
                self._select(m, raw)
            except GmailError:
                continue
            m._om_key = self._fkey(m, raw)
            uids, need_local = None, list(local)
            if server_txt:
                try:
                    m.literal = server_txt[1].encode("utf-8")
                    typ, data = m.uid("SEARCH", "CHARSET", "UTF-8", *crit, server_txt[0])
                    if typ == "OK":
                        uids = (data[0] or b"").split()
                except imaplib.IMAP4.error:
                    uids = None
                finally:
                    m.literal = None
                if uids is None:  # server cannot search UTF-8 text: filter the newest messages locally
                    need_local = list(tq["text"])
            if uids is None:
                typ, data = m.uid("SEARCH", *crit)
                if typ != "OK":
                    raise GmailError(f"搜索失败 search failed: {data}")
                uids = (data[0] or b"").split()
            uids = sorted(uids, key=int)
            if need_local:
                metas = []
                pool = uids[-300:]
                for i in range(0, len(pool), 100):
                    metas += self._fetch_meta(m, pool[i:i + 100])
                found += [x for x in metas if local_match(x, need_local)]
            else:
                found += self._fetch_meta(m, uids[-n:])
        found.sort(key=lambda x: _date_key(x.get("date", "")), reverse=True)
        return found[:n]

    def get_message(self, message_id: str) -> dict:
        m = self._imap()
        try:
            uid = self._locate(m, message_id)
            meta = self._fetch_meta(m, [uid.encode()], with_snippet=False)
            typ, data = m.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((d[1] for d in data if isinstance(d, tuple)), b"")
            if not raw:
                raise GmailError(f"找不到邮件 message not found: {message_id}")
            msg = email.message_from_bytes(raw)
            body, attachments = _body_text(msg)
            info = self.public(meta[0]) if meta else {"id": message_id}
            if info.get("security_message"):
                info["body"] = "[这是一封验证码/登录/重置密码类的安全邮件，内容不会提供给 Agent。This is a security email; its content is withheld from the agent.]"
                info["redactions"] = 1
            else:
                body, n = redact_secrets(body)
                info["body"] = body[:15000]
                info["redactions"] = n
            info["attachments"] = attachments
            info["references"] = msg.get("References", "")
            return info
        finally:
            _logout(m)

    def get_thread(self, thread_id: str) -> list[dict]:
        if not self._gm():
            return self._thread_generic(thread_id)
        if not re.fullmatch(r"\d{5,25}", str(thread_id)):
            raise GmailError(f"无效的 thread_id: {thread_id}")
        m = self._imap()
        try:
            self._select_all(m)
            typ, data = m.uid("SEARCH", None, "X-GM-THRID", str(thread_id))
            uids = (data[0] or b"").split()[-15:]
            metas = self._fetch_meta(m, uids, with_snippet=False)
        finally:
            _logout(m)
        return self._full_thread(metas)

    def _full_thread(self, metas: list[dict]) -> list[dict]:
        msgs = []
        for meta in sorted(metas, key=lambda x: _date_key(x.get("date", "")))[-15:]:
            full = self.get_message(meta["id"])
            full["body"] = full.get("body", "")[:6000]
            msgs.append(full)
        return msgs

    def _thread_generic(self, anchor: str) -> list[dict]:
        """No thread ids outside Gmail: follow Message-ID / References / In-Reply-To across Inbox, Archive and Sent."""
        m = self._imap()
        try:
            uid = self._locate(m, anchor)
            typ, data = m.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO)])")
            raw = next((d[1] for d in data or [] if isinstance(d, tuple)), b"")
            h = email.message_from_bytes(raw)
            refs = re.findall(r"<[^<>\s]+>", (h.get("References") or "") + " " + (h.get("In-Reply-To") or ""))
            own = (h.get("Message-ID") or "").strip()
            root = refs[0] if refs else own
            metas: dict[str, dict] = {}
            if root and root.isascii():
                q = self._quote(root)
                for folder in self._search_targets(m, ["*"]):
                    try:
                        self._select(m, folder)
                    except GmailError:
                        continue
                    m._om_key = self._fkey(m, folder)
                    typ, data = m.uid("SEARCH", "OR", "OR", "HEADER", "Message-ID", q, "HEADER", "References", q,
                                      "HEADER", "In-Reply-To", q)
                    if typ == "OK":
                        for x in self._fetch_meta(m, (data[0] or b"").split()[-15:], with_snippet=False):
                            metas[x["id"]] = x
            if anchor not in metas:
                metas[anchor] = {"id": anchor, "date": ""}
        finally:
            _logout(m)
        return self._full_thread(list(metas.values()))

    def list_labels(self) -> list[str]:
        m = self._imap()
        try:
            f = self.folders(m)
            return [f["_display"].get(n, n.strip('"')) for n in f.get("_names", [])]
        finally:
            _logout(m)

    # ------------------------------------------------------------ organize
    def _store_labels(self, ids: list[str], op: str, labels: list[str]) -> int:
        m = self._imap()
        try:
            self._select_all(m, readonly=False)
            n = 0
            for mid in ids[:50]:
                uid = self._uid_for_msgid(m, mid)
                lab = "(" + " ".join(self._quote(x) if not x.startswith("\\") else x for x in labels) + ")"
                typ, _ = m.uid("STORE", uid, f"{op}X-GM-LABELS", lab)
                if typ == "OK":
                    n += 1
            return n
        finally:
            _logout(m)

    def _move(self, m, uid: str, dest: str) -> bool:
        if "MOVE" in m.capabilities:
            typ, _ = m.uid("MOVE", uid, dest)
            return typ == "OK"
        typ, _ = m.uid("COPY", uid, dest)
        if typ != "OK":
            return False
        m.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        if "UIDPLUS" in m.capabilities:  # only this message; never a plain EXPUNGE of other \Deleted mail
            m.uid("EXPUNGE", uid)
        return True

    def _generic_each(self, ids: list[str], fn) -> int:
        m = self._imap()
        try:
            n = 0
            for mid in ids[:50]:
                uid = self._locate(m, str(mid), readonly=False)
                n += bool(fn(m, uid, m._om_key))
            return n
        finally:
            _logout(m)

    def _gm(self) -> bool:
        """Gmail server? Known after the first login (X-GM-EXT-1); before that, from the account's provider."""
        if self.gm is None and self.provider == "custom":
            _logout(self._imap())
        return self.gm if self.gm is not None else self.provider == "gmail"

    def archive(self, ids: list[str]) -> dict:
        if self._gm():
            return {"archived": self._store_labels(ids, "-", ["\\Inbox"])}

        def mv(m, uid, key):
            if key != "i":
                return False  # already out of the inbox
            dest = self.folders(m).get("archive") or self._folder_by_name(m, "Archive", create=True)
            return self._move(m, uid, dest)
        return {"archived": self._generic_each(ids, mv)}

    def label(self, ids: list[str], add: list[str] | None = None, remove: list[str] | None = None) -> dict:
        res = {}
        if self._gm():
            if add:
                res["added"] = self._store_labels(ids, "+", add)
            if remove:
                res["removed"] = self._store_labels(ids, "-", remove)
            return res
        # other providers: folders instead of labels; starred = \Flagged
        for lab in add or []:
            up = lab.strip().upper()

            def do_add(m, uid, key, lab=lab, up=up):
                if up in _STAR:
                    return m.uid("STORE", uid, "+FLAGS", "(\\Flagged)")[0] == "OK"
                if up in ("UNREAD", "\\UNREAD"):
                    return m.uid("STORE", uid, "-FLAGS", "(\\Seen)")[0] == "OK"
                dest = self._folder_by_name(m, lab, create=True)
                if dest == "INBOX":
                    return key != "i" and self._move(m, uid, "INBOX")
                return m.uid("COPY", uid, dest)[0] == "OK"
            res["added"] = res.get("added", 0) + self._generic_each(ids, do_add)
        for lab in remove or []:
            up = lab.strip().upper()

            def do_rm(m, uid, key, lab=lab, up=up):
                if up in _STAR:
                    return m.uid("STORE", uid, "-FLAGS", "(\\Flagged)")[0] == "OK"
                if up in ("UNREAD", "\\UNREAD"):
                    return m.uid("STORE", uid, "+FLAGS", "(\\Seen)")[0] == "OK"
                if up in ("INBOX", "\\INBOX"):
                    if key != "i":
                        return False
                    dest = self.folders(m).get("archive") or self._folder_by_name(m, "Archive", create=True)
                    return self._move(m, uid, dest)
                src = self._folder_by_name(m, lab)
                if src and m._om_sel[0] == src:  # "remove label" = move the message back to the inbox
                    return self._move(m, uid, "INBOX")
                return False
            res["removed"] = res.get("removed", 0) + self._generic_each(ids, do_rm)
        return res

    def mark_read(self, ids: list[str], read: bool = True) -> dict:
        m = self._imap()
        try:
            n = 0
            for mid in ids[:50]:
                uid = self._locate(m, mid, readonly=False)
                typ, _ = m.uid("STORE", uid, "+FLAGS" if read else "-FLAGS", "(\\Seen)")
                n += typ == "OK"
            return {"updated": n}
        finally:
            _logout(m)

    # ------------------------------------------------------------ compose
    def get_attachment(self, message_id: str, filename: str) -> tuple[str, str, bytes]:
        """One attachment of a message: (filename, content type, bytes). Security emails never give out attachments."""
        m = self._imap()
        try:
            uid = self._locate(m, message_id)
            meta = self._fetch_meta(m, [uid.encode()], with_snippet=False)
            if meta and meta[0].get("security_message"):
                raise GmailError("安全类邮件的附件不提供给 Agent (security email)")
            typ, data = m.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((d[1] for d in data if isinstance(d, tuple)), b"")
            msg = email.message_from_bytes(raw)
            names = []
            want = (filename or "").strip().lower()
            for part in msg.walk():
                if part.is_multipart():
                    continue
                fn = _dec(part.get_filename()) if part.get_filename() else ""
                if not fn and "attachment" not in (part.get("Content-Disposition") or "").lower():
                    continue
                names.append(fn or "(unnamed)")
                if not want or fn.lower() == want:
                    payload = part.get_payload(decode=True) or b""
                    if len(payload) > MAX_ATTACHMENT:
                        raise GmailError(f"附件太大 attachment too large ({len(payload) // 1_000_000} MB)")
                    return fn or "attachment", part.get_content_type(), payload
            raise GmailError(f"找不到附件 attachment not found: {filename}. Attachments: {', '.join(names) or 'none'}")
        finally:
            _logout(m)

    def _compose(self, to: str, subject: str, body: str, cc: str = "", in_reply_to: dict | None = None,
                 attachments: list | None = None) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((self.display_name, self.email)) if self.display_name else self.email
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        if in_reply_to:
            subj = _unfold(in_reply_to.get("subject", ""))
            if not subject:
                subject = subj if subj.lower().startswith("re:") else f"Re: {subj}"
            mid = _unfold(in_reply_to.get("message_id_header", ""))
            if mid:
                msg["In-Reply-To"] = mid
                # long References headers arrive folded over several lines (CRLF + space); EmailMessage refuses
                # header values with line breaks, so unfold them (E2E-1: two of three reply drafts failed on this)
                refs = _unfold(in_reply_to.get("references", "") + " " + mid)
                msg["References"] = refs
        msg["Subject"] = _unfold(subject) or "(no subject)"
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=self.email.split("@")[-1])
        msg.set_content(body)
        for name, ctype, data in attachments or []:
            main, _, sub = (ctype or "application/octet-stream").partition("/")
            msg.add_attachment(data, maintype=main, subtype=sub or "octet-stream", filename=name)
        return msg

    def _original(self, message_id: str | None) -> dict | None:
        if not message_id:
            return None
        m = self._imap()
        try:
            uid = self._locate(m, message_id)
            typ, data = m.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO TO CC SUBJECT MESSAGE-ID REFERENCES)])")
            raw = next((d[1] for d in data if isinstance(d, tuple)), b"")
            h = email.message_from_bytes(raw)
            return {"from": _dec(h.get("Reply-To") or h.get("From")), "to": _dec(h.get("To")), "cc": _dec(h.get("Cc")),
                    "subject": _dec(h.get("Subject")), "message_id_header": h.get("Message-ID", ""),
                    "references": h.get("References", "")}
        finally:
            _logout(m)

    def reply_defaults(self, message_id: str, reply_all: bool = False) -> dict:
        orig = self._original(message_id) or {}
        to = orig.get("from", "")
        cc = ""
        if reply_all:
            others = [a for a in email.utils.getaddresses([orig.get("to", ""), orig.get("cc", "")])
                      if a[1] and a[1].lower() != self.email.lower()]
            cc = ", ".join(email.utils.formataddr(a) for a in others)
        subj = orig.get("subject", "")
        return {"to": to, "cc": cc, "subject": subj if subj.lower().startswith("re:") else f"Re: {subj}"}

    def create_draft(self, to: str, subject: str, body: str, cc: str = "", reply_to_message_id: str | None = None,
                     attachments: list | None = None) -> dict:
        orig = self._original(reply_to_message_id)
        msg = self._compose(to, subject, body, cc, orig, attachments)
        m = self._imap()
        try:
            drafts = self.folders(m).get("drafts") or self._folder_by_name(m, "Drafts", create=True)
            typ, data = m.append(drafts, "(\\Draft \\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if typ != "OK":
                raise GmailError(f"保存草稿失败 draft failed: {data}")
            return {"saved": True, "to": to, "subject": msg["Subject"], "folder": self._fname(m, drafts)}
        finally:
            _logout(m)

    def _smtp(self):
        ctx = ssl.create_default_context()
        if self.smtp_security == "ssl":
            s = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=ctx, timeout=TIMEOUT)
        else:
            s = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=TIMEOUT)
            s.ehlo()
            if self.smtp_security == "starttls":
                s.starttls(context=ctx)
                s.ehlo()
        try:
            if self.token_fn:
                tok = base64.b64encode(xoauth2(self.username, self.token_fn()).encode()).decode()
                code, resp = s.docmd("AUTH", "XOAUTH2 " + tok)
                if code == 334:  # server sent an error challenge; finish the exchange to get the real reply
                    code, resp = s.docmd("")
                if code != 235:
                    raise smtplib.SMTPAuthenticationError(code, resp)
            elif self.password or self.smtp_security != "none":
                s.login(self.username, self.password)
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            raise
        return s

    def send(self, to: str, subject: str, body: str, cc: str = "", reply_to_message_id: str | None = None,
             attachments: list | None = None) -> dict:
        orig = self._original(reply_to_message_id)
        msg = self._compose(to, subject, body, cc, orig, attachments)
        try:
            with self._smtp() as s:
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise GmailError(f"SMTP 登录失败 (auth failed): {e}. {_LOGIN_HINT.get(self.provider, _LOGIN_HINT['custom'])}")
        except OAuthError as e:
            raise GmailError(str(e))
        except (smtplib.SMTPException, OSError) as e:
            raise GmailError(f"发送失败 send failed: {e}")
        out = {"sent": True, "to": to, "cc": cc, "subject": msg["Subject"], "message_id_header": msg["Message-ID"],
               "attachments": [a[0] for a in attachments or []]}
        if not self._gm():
            out["saved_to_sent"] = self._keep_sent_copy(msg)
        return out

    def _keep_sent_copy(self, msg: EmailMessage) -> bool:
        """Some servers (e.g. iCloud) do not file mail sent over SMTP into Sent. Check, and append a copy if missing."""
        try:
            m = self._imap()
        except GmailError:
            return False
        try:
            if self._gm():
                return True
            sent = self.folders(m).get("sent")
            if not sent:
                return False
            q = self._quote(msg["Message-ID"])
            for wait in (2, 3):
                time.sleep(wait)
                m._om_sel = None
                self._select(m, sent)
                typ, data = m.uid("SEARCH", "HEADER", "Message-ID", q)
                if typ == "OK" and (data[0] or b"").split():
                    return True
            typ, _ = m.append(sent, "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            return typ == "OK"
        except Exception:
            return False
        finally:
            _logout(m)

    # ------------------------------------------------------------ unsubscribe (RFC 2369 / RFC 8058)
    def unsubscribe_targets(self, ids: list[str]) -> list[dict]:
        """Sender/subject/method for each message. Internal: includes the raw targets (never sent to the agent)."""
        m = self._imap()
        try:
            out = []
            for mid in ids[:MAX_UNSUB]:
                try:
                    uid = self._locate(m, str(mid))
                    meta = self._fetch_meta(m, [uid.encode()], with_snippet=False)
                except GmailError as e:
                    out.append({"id": str(mid), "error": str(e), "method": ""})
                    continue
                if not meta:
                    out.append({"id": str(mid), "error": "not found", "method": ""})
                    continue
                x = meta[0]
                u = x.get("_unsub") or {}
                out.append({"id": str(mid), "from": x.get("from", ""), "subject": x.get("subject", ""),
                            "date": x.get("date", ""), "method": u.get("method", ""), "_unsub": u})
            return out
        finally:
            _logout(m)

    def unsubscribe(self, ids: list[str]) -> list[dict]:
        results = []
        for t in self.unsubscribe_targets(ids):
            u = t.pop("_unsub", None) or {}
            r = {k: t.get(k, "") for k in ("id", "from", "subject", "method")}
            if t.get("error"):
                r.update(status="failed", note=t["error"])
            elif not u.get("method"):
                r.update(status="manual", note="这封邮件没有标准退订信息 (no List-Unsubscribe header)，请在邮件正文里手动点退订")
            else:
                r.update(self._do_unsub(u))
            results.append(r)
        return results

    def _do_unsub(self, u: dict) -> dict:
        errors = []
        # 1) RFC 8058 one-click: a single POST, no page to confirm
        if u.get("one_click") and u.get("https"):
            ok, why = check_url(u["https"])
            if ok:
                try:
                    with httpx.Client(timeout=20, follow_redirects=True, max_redirects=5,
                                      headers={"User-Agent": "OMuse-Unsubscribe/1.0"}) as c:
                        resp = c.post(u["https"], data={"List-Unsubscribe": "One-Click"})
                    if resp.status_code < 400:
                        return {"status": "done", "via": "one-click", "note": f"已一键退订 ({domain_of(u['https'])})"}
                    errors.append(f"one-click HTTP {resp.status_code}")
                except httpx.HTTPError as e:
                    errors.append(f"one-click {type(e).__name__}")
            else:
                errors.append(why)
        # 2) mailto: send the unsubscribe email the sender asked for
        if u.get("mailto"):
            try:
                mt = urlparse(u["mailto"])
                to = unquote(mt.path)
                q = parse_qs(mt.query)
                subj = (q.get("subject") or ["unsubscribe"])[0][:200]
                body = (q.get("body") or ["unsubscribe"])[0][:1000]
                if re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[A-Za-z]{2,}", to):
                    self.send(to, subj, body)
                    return {"status": "done", "via": "email", "note": f"已发送退订邮件给 {to.split('@')[-1]}"}
                errors.append("invalid mailto")
            except GmailError as e:
                errors.append(str(e))
        # 3) plain link: open it; many senders unsubscribe on visit, some still want a click on their page
        if u.get("https") and not u.get("one_click"):
            ok, why = check_url(u["https"])
            if ok:
                try:
                    with httpx.Client(timeout=20, follow_redirects=True, max_redirects=5,
                                      headers={"User-Agent": "Mozilla/5.0 OMuse-Unsubscribe/1.0"}) as c:
                        resp = c.get(u["https"])
                    if resp.status_code < 400:
                        return {"status": "link_opened", "via": "link",
                                "note": f"已打开退订页面 ({domain_of(u['https'])})；部分网站还需要在页面上再点一次确认"}
                    errors.append(f"link HTTP {resp.status_code}")
                except httpx.HTTPError as e:
                    errors.append(f"link {type(e).__name__}")
            else:
                errors.append(why)
        return {"status": "failed", "note": "; ".join(errors) or "unsupported unsubscribe method"}

    def test(self) -> dict:
        m = self._imap()
        try:
            f = self.folders(m)
            if self._gm():
                self._select_all(m)
                typ, data = m.uid("SEARCH", None, "X-GM-RAW", '"in:inbox is:unread"')
            else:
                self._select(m, "INBOX")
                typ, data = m.uid("SEARCH", "UNSEEN")
            unread = len((data[0] or b"").split()) if typ == "OK" else None
            out = {"ok": True, "provider": self.provider, "drafts": self._fname(m, f["drafts"]) if f.get("drafts") else None,
                   "sent": self._fname(m, f["sent"]) if f.get("sent") else None, "unread_inbox": unread}
            if self._gm():
                out["all_mail"] = f.get("all")
            return out
        finally:
            _logout(m)


def _logout(m) -> None:
    try:
        m.logout()
    except Exception:
        pass


def _snippet(raw: bytes) -> str:
    txt = raw.decode("utf-8", errors="replace")
    if "<html" in txt.lower() or "<div" in txt.lower():
        txt = _html_to_text(txt)
    txt = re.sub(r"=\r?\n", "", txt)
    txt = re.sub(r"--[A-Za-z0-9_=.\-]{10,}.*", " ", txt)
    txt = re.sub(r"Content-[A-Za-z-]+:[^\n]*", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()[:240]


def _date_key(d: str) -> float:
    try:
        return email.utils.parsedate_to_datetime(d).timestamp()
    except Exception:
        return 0.0


MAX_UNSUB = 30


def parse_list_unsubscribe(value: str, post: str = "") -> dict:
    """Parse RFC 2369 List-Unsubscribe (+ RFC 8058 List-Unsubscribe-Post)."""
    value = re.sub(r"\s+", "", _dec(value) if value else "")
    targets = re.findall(r"<([^>]+)>", value) or [x for x in value.split(",") if x]
    out: dict = {}
    for t in targets:
        tl = t.lower()
        if tl.startswith("mailto:") and "mailto" not in out:
            out["mailto"] = t
        elif tl.startswith("https://") and "https" not in out:
            out["https"] = t
    out["one_click"] = bool(out.get("https")) and "one-click" in (post or "").lower().replace(" ", "")
    if out["one_click"]:
        out["method"] = "one-click"
    elif out.get("mailto"):
        out["method"] = "email"
    elif out.get("https"):
        out["method"] = "link"
    else:
        out["method"] = ""
    return out


def dedupe_or_terms(q: str) -> str:
    """A looping model writes "conference OR conference OR conference …" (2026-10-04 V4-07); keep each OR term once."""
    q = str(q or "")
    if q.count(" OR ") < 3:
        return q
    out, seen = [], set()
    for part in re.split(r"(\s+OR\s+)", q):
        if re.fullmatch(r"\s+OR\s+", part):
            out.append(part)
            continue
        k = part.strip().lower()
        if k in seen:
            if out and re.fullmatch(r"\s+OR\s+", out[-1]):
                out.pop()
            continue
        seen.add(k)
        out.append(part)
    return "".join(out).strip()
