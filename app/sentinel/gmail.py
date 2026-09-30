"""Gmail connector over IMAP (read/organize/draft) and SMTP (send), using an App Password.

Runs only inside Sentinel. The credential is fetched from the vault per call and never
returned to callers. Message ids are Gmail's X-GM-MSGID (decimal string), thread ids are
X-GM-THRID, so the interface matches the Gmail API shape and can be swapped later.
"""
from __future__ import annotations

import email
import email.utils
import html as htmllib
import imaplib
import re
import smtplib
import ssl
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.sentinel.guard import check_url, domain_of, is_security_message, redact_secrets

TIMEOUT = 30
MAX_ATTACHMENT = 25 * 1024 * 1024


class GmailError(Exception):
    pass


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


class Gmail:
    def __init__(self, email_addr: str, app_password: str, imap_host: str = "imap.gmail.com",
                 smtp_host: str = "smtp.gmail.com", display_name: str = ""):
        self.email = email_addr
        self.password = app_password.replace(" ", "")
        self.imap_host = imap_host
        self.smtp_host = smtp_host
        self.display_name = display_name
        self._folders: dict | None = None

    # ------------------------------------------------------------ connection
    def _imap(self) -> imaplib.IMAP4_SSL:
        try:
            m = imaplib.IMAP4_SSL(self.imap_host, 993, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
            m.login(self.email, self.password)
            return m
        except imaplib.IMAP4.error as e:
            raise GmailError(f"IMAP 登录失败 (login failed): {e}. 请检查邮箱地址和应用专用密码 App Password，并确认 Gmail 已开启 IMAP。")
        except OSError as e:
            raise GmailError(f"无法连接 {self.imap_host}: {e}")

    def folders(self, m) -> dict:
        if self._folders:
            return self._folders
        typ, data = m.list()
        out = {}
        for raw in data or []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            mm = re.match(r'\((?P<flags>[^)]*)\) "(?P<sep>[^"]*)" (?P<name>.+)$', line)
            if not mm:
                continue
            name = mm.group("name").strip()
            flags = mm.group("flags")
            for flag, key in (("\\All", "all"), ("\\Drafts", "drafts"), ("\\Sent", "sent"), ("\\Trash", "trash"),
                              ("\\Junk", "spam"), ("\\Important", "important"), ("\\Flagged", "starred")):
                if flag in flags:
                    out[key] = name
            out.setdefault("_names", []).append(name)
        out.setdefault("all", '"[Gmail]/All Mail"')
        out.setdefault("drafts", '"[Gmail]/Drafts"')
        self._folders = out
        return out

    @staticmethod
    def _quote(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _select_all(self, m, readonly=True):
        f = self.folders(m)["all"]
        typ, _ = m.select(f, readonly=readonly)
        if typ != "OK":
            raise GmailError("无法打开「所有邮件」文件夹 (cannot select All Mail)")

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
        parts = "(UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)]"
        parts += " BODY.PEEK[TEXT]<0.3000>)" if with_snippet else ")"
        typ, data = m.uid("FETCH", uid_set, parts)
        results: dict[str, dict] = {}
        cur = None
        for item in data or []:
            if isinstance(item, tuple):
                head = item[0].decode(errors="replace")
                uid = re.search(r"UID (\d+)", head)
                if uid:
                    cur = results.setdefault(uid.group(1), {"uid": uid.group(1)})
                    gm = re.search(r"X-GM-MSGID (\d+)", head)
                    th = re.search(r"X-GM-THRID (\d+)", head)
                    lb = re.search(r"X-GM-LABELS \(([^)]*)\)", head)
                    fl = re.search(r"FLAGS \(([^)]*)\)", head)
                    if gm:
                        cur["id"] = gm.group(1)
                    if th:
                        cur["thread_id"] = th.group(1)
                    if lb:
                        cur["labels"] = [x.strip('"').replace("\\\\", "\\") for x in re.findall(r'"[^"]*"|\S+', lb.group(1))]
                    if fl:
                        cur["unread"] = "\\Seen" not in fl.group(1)
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
                    raw = item[1] or b""
                    txt = raw.decode("utf-8", errors="replace")
                    if "<html" in txt.lower() or "<div" in txt.lower():
                        txt = _html_to_text(txt)
                    txt = re.sub(r"=\r?\n", "", txt)
                    txt = re.sub(r"--[A-Za-z0-9_=.\-]{10,}.*", " ", txt)
                    txt = re.sub(r"Content-[A-Za-z-]+:[^\n]*", " ", txt)
                    cur["snippet"] = re.sub(r"\s+", " ", txt).strip()[:240]
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
        return msg

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        max_results = max(1, min(int(max_results or 10), 30))
        m = self._imap()
        try:
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

    def get_message(self, message_id: str) -> dict:
        m = self._imap()
        try:
            self._select_all(m)
            uid = self._uid_for_msgid(m, message_id)
            meta = self._fetch_meta(m, [uid.encode()], with_snippet=False)
            typ, data = m.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((d[1] for d in data if isinstance(d, tuple)), b"")
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
        msgs = []
        for meta in sorted(metas, key=lambda x: _date_key(x.get("date", ""))):
            full = self.get_message(meta["id"])
            full["body"] = full.get("body", "")[:6000]
            msgs.append(full)
        return msgs

    def list_labels(self) -> list[str]:
        m = self._imap()
        try:
            f = self.folders(m)
            return [n.strip('"') for n in f.get("_names", [])]
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

    def archive(self, ids: list[str]) -> dict:
        return {"archived": self._store_labels(ids, "-", ["\\Inbox"])}

    def label(self, ids: list[str], add: list[str] | None = None, remove: list[str] | None = None) -> dict:
        res = {}
        if add:
            res["added"] = self._store_labels(ids, "+", add)
        if remove:
            res["removed"] = self._store_labels(ids, "-", remove)
        return res

    def mark_read(self, ids: list[str], read: bool = True) -> dict:
        m = self._imap()
        try:
            self._select_all(m, readonly=False)
            n = 0
            for mid in ids[:50]:
                uid = self._uid_for_msgid(m, mid)
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
            self._select_all(m)
            uid = self._uid_for_msgid(m, message_id)
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
            subj = in_reply_to.get("subject", "")
            if not subject:
                subject = subj if subj.lower().startswith("re:") else f"Re: {subj}"
            mid = in_reply_to.get("message_id_header", "")
            if mid:
                msg["In-Reply-To"] = mid
                refs = (in_reply_to.get("references", "") + " " + mid).strip()
                msg["References"] = refs
        msg["Subject"] = subject or "(no subject)"
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
            self._select_all(m)
            uid = self._uid_for_msgid(m, message_id)
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
            drafts = self.folders(m)["drafts"]
            typ, data = m.append(drafts, "(\\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if typ != "OK":
                raise GmailError(f"保存草稿失败 draft failed: {data}")
            return {"saved": True, "to": to, "subject": msg["Subject"], "folder": drafts.strip('"')}
        finally:
            _logout(m)

    def send(self, to: str, subject: str, body: str, cc: str = "", reply_to_message_id: str | None = None,
             attachments: list | None = None) -> dict:
        orig = self._original(reply_to_message_id)
        msg = self._compose(to, subject, body, cc, orig, attachments)
        try:
            with smtplib.SMTP_SSL(self.smtp_host, 465, context=ssl.create_default_context(), timeout=TIMEOUT) as s:
                s.login(self.email, self.password)
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise GmailError(f"SMTP 登录失败 (auth failed): {e}")
        except (smtplib.SMTPException, OSError) as e:
            raise GmailError(f"发送失败 send failed: {e}")
        return {"sent": True, "to": to, "cc": cc, "subject": msg["Subject"], "message_id_header": msg["Message-ID"],
                "attachments": [a[0] for a in attachments or []]}

    # ------------------------------------------------------------ unsubscribe (RFC 2369 / RFC 8058)
    def unsubscribe_targets(self, ids: list[str]) -> list[dict]:
        """Sender/subject/method for each message. Internal: includes the raw targets (never sent to the agent)."""
        m = self._imap()
        try:
            self._select_all(m)
            out = []
            for mid in ids[:MAX_UNSUB]:
                try:
                    uid = self._uid_for_msgid(m, str(mid))
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
                                      headers={"User-Agent": "Locius-Unsubscribe/1.0"}) as c:
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
                                      headers={"User-Agent": "Mozilla/5.0 Locius-Unsubscribe/1.0"}) as c:
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
            self._select_all(m)
            typ, data = m.uid("SEARCH", None, "X-GM-RAW", '"in:inbox is:unread"')
            unread = len((data[0] or b"").split()) if typ == "OK" else None
            return {"ok": True, "all_mail": f.get("all"), "drafts": f.get("drafts"), "unread_inbox": unread}
        finally:
            _logout(m)


def _logout(m) -> None:
    try:
        m.logout()
    except Exception:
        pass


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
