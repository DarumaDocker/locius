"""Non-Gmail mailboxes against a real IMAP server (Dovecot on 127.0.0.1:1143) and an SMTP server (aiosmtpd on :1025).

Start Dovecot first (tests/dovecot.conf, see the docstring there); this script starts its own SMTP server and seeds the
mailbox. Covers: search translation (folders, from:, is:unread, UTF-8 text, dates, OR), message / thread / attachment,
drafts, send + Sent copy, archive (Archive folder created), labels as folders (UTF-7 names), stars, read state,
unsubscribe targets, and the API connect flow when a local stack runs (pass --api)."""
import email.utils
import imaplib
import sys
import time

sys.path.insert(0, ".")
from aiosmtpd.controller import Controller  # noqa: E402
from aiosmtpd.smtp import AuthResult, LoginPassword  # noqa: E402

from app.sentinel.gmail import Gmail, GmailError  # noqa: E402

USER, PW = "tester@example.com", "secretpass123"
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else str(info)[:600])
    if not cond:
        fails.append(name)


class Sink:
    def __init__(self):
        self.msgs = []
        self.xoauth = []

    async def auth_XOAUTH2(self, server, args):  # aiosmtpd custom AUTH mechanism
        import base64
        raw = base64.b64decode(args[1]).decode() if len(args) > 1 else ""
        self.xoauth.append(raw)
        ok = raw.startswith("user=me@outlook.com\x01auth=Bearer at-") and raw.endswith("\x01\x01")
        return AuthResult(success=ok, handled=False)

    async def handle_DATA(self, server, session, envelope):
        self.msgs.append((envelope.mail_from, envelope.rcpt_tos, envelope.content))
        return "250 OK"


def auth(server, session, envelope, mechanism, data):
    ok = isinstance(data, LoginPassword) and data.login.decode() == USER and data.password.decode() == PW
    return AuthResult(success=ok)


def seed():
    m = imaplib.IMAP4("127.0.0.1", 1143)
    m.login(USER, PW)
    for box in ("INBOX", "Sent", "Drafts", "Archive", "&XeVPXA-"):  # &XeVPXA- = 工作
        try:
            m.select(box)
            m.store("1:*", "+FLAGS", "(\\Deleted)")
            m.expunge()
        except Exception:
            pass
    m.select("INBOX")  # never delete the selected mailbox (Dovecot drops the connection)
    for box in ("Archive", "&XeVPXA-"):
        m.delete(box)
    now = email.utils.formatdate(localtime=True)
    old = email.utils.formatdate(time.time() - 40 * 86400, localtime=True)
    msgs = [
        ("\\Seen", f"From: Boss <boss@acme.com>\r\nTo: {USER}\r\nSubject: Quarterly plan\r\nDate: {now}\r\n"
                   "Message-ID: <q1@acme.com>\r\n\r\nPlease review the quarterly plan by Friday.\r\n"),
        ("", f"From: =?utf-8?b?6LSi5Yqh?= <finance@acme.com>\r\nTo: {USER}\r\nSubject: =?utf-8?b?5Y+R56Wo5oql6ZSA?=\r\n"
             f"Date: {now}\r\nMessage-ID: <f1@acme.com>\r\nContent-Type: text/plain; charset=utf-8\r\n"
             "Content-Transfer-Encoding: 8bit\r\n\r\n" + "请在月底前提交发票报销单。".encode().decode("latin-1") + "\r\n"),
        ("", f"From: Shop <news@shop.com>\r\nTo: {USER}\r\nSubject: Big sale today\r\nDate: {now}\r\nMessage-ID: <n1@shop.com>\r\n"
             "List-Unsubscribe: <https://shop.com/unsub/XYZ>, <mailto:leave@shop.com>\r\nList-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n"
             "\r\n50% off everything.\r\n"),
        ("\\Seen", f"From: Old <old@acme.com>\r\nTo: {USER}\r\nSubject: Old news\r\nDate: {old}\r\nMessage-ID: <o1@acme.com>\r\n\r\nLast month.\r\n"),
        ("", f"From: Boss <boss@acme.com>\r\nTo: {USER}\r\nSubject: Re: Quarterly plan\r\nDate: {now}\r\nMessage-ID: <q2@acme.com>\r\n"
             "In-Reply-To: <q1@acme.com>\r\nReferences: <q1@acme.com>\r\nContent-Type: multipart/mixed; boundary=BB\r\n\r\n--BB\r\n"
             "Content-Type: text/plain\r\n\r\nAttached the numbers.\r\n--BB\r\nContent-Type: text/csv\r\n"
             "Content-Disposition: attachment; filename=\"numbers.csv\"\r\n\r\na,b\r\n1,2\r\n--BB--\r\n"),
    ]
    for flags, raw in msgs:
        when = time.time() - (40 * 86400 if "Old news" in raw else 0)  # SINCE / BEFORE use the arrival (internal) date
        m.append("INBOX", f"({flags})" if flags else None, imaplib.Time2Internaldate(when), raw.encode("latin-1"))
    m.append("Sent", "(\\Seen)", imaplib.Time2Internaldate(time.time()),
             f"From: {USER}\r\nTo: boss@acme.com\r\nSubject: Re: Quarterly plan\r\nDate: {now}\r\nMessage-ID: <q3@example.com>\r\n"
             "In-Reply-To: <q1@acme.com>\r\nReferences: <q1@acme.com>\r\n\r\nWill do.\r\n".encode())
    m.logout()


def main():
    seed()
    sink = Sink()
    ctl = Controller(sink, hostname="127.0.0.1", port=1025, authenticator=auth, auth_require_tls=False)
    ctl.start()
    try:
        run(sink)
        outlook(sink)
        if "--api" in sys.argv:
            api()
    finally:
        ctl.stop()
    print("\n%d failures" % len(fails), fails)
    sys.exit(1 if fails else 0)


def client():
    return Gmail(USER, PW, "127.0.0.1", "127.0.0.1", "Tester", provider="custom", imap_port=1143, imap_security="none",
                 smtp_port=1025, smtp_security="none")


def run(sink):
    g = client()
    t = g.test()
    check("test(): generic mode, folders found", t["ok"] and g.gm is False and t["sent"] == "Sent" and t["drafts"] == "Drafts", t)
    check("test(): unread count", t["unread_inbox"] == 3, t)

    res = g.search("in:inbox", 10)
    check("search in:inbox -> 5 messages, folder ids", len(res) == 5 and all(r["id"].startswith("i-") for r in res), res)
    check("decoded subjects / from", any(r["subject"] == "发票报销" and "财务" in r["from"] for r in res), [r["subject"] for r in res])
    check("search from:boss", len(g.search("from:boss@acme.com", 10)) == 2)
    check("search is:unread", len(g.search("is:unread", 10)) == 3)
    r = g.search("发票", 10)
    check("search UTF-8 text", len(r) == 1 and r[0]["subject"] == "发票报销", r)
    check("search newer_than:7d excludes old", len(g.search("newer_than:7d", 10)) == 4)
    check("search older_than:30d", [x["subject"] for x in g.search("older_than:30d", 10)] == ["Old news"])
    check("search OR", len(g.search("subject:sale OR subject:news", 10)) == 2)
    check("search {a b}", len(g.search("{from:news@shop.com from:old@acme.com}", 10)) == 2)
    check("search -category:promotions", all("sale" not in x["subject"] for x in g.search("-category:promotions", 10)))
    check("search category:promotions", [x["subject"] for x in g.search("category:promotions", 10)] == ["Big sale today"])
    check("search has:attachment", [x["subject"] for x in g.search("has:attachment", 10)] == ["Re: Quarterly plan"])
    check("search in:sent", [x["id"][:2] for x in g.search("in:sent", 10)] == ["s-"])
    check("search in:anywhere covers sent", len(g.search("in:anywhere quarterly", 10)) == 3)
    try:
        g.search("label:Nope", 5)
        check("unknown folder errors", False)
    except GmailError as e:
        check("unknown folder errors with folder list", "Sent" in str(e), e)
    sale = next(x for x in res if x["subject"] == "Big sale today")
    check("unsubscribe method visible, URL hidden", sale.get("unsubscribe") == "one-click" and "XYZ" not in str(res), sale)

    reply = next(x for x in res if x["subject"] == "Re: Quarterly plan")
    full = g.get_message(reply["id"])
    check("get_message body + attachment list", "numbers" in full["body"] and full["attachments"][0]["filename"] == "numbers.csv", full)
    name, ctype, data = g.get_attachment(reply["id"], "numbers.csv")
    check("get_attachment", name == "numbers.csv" and b"1,2" in data, (name, data))
    th = g.get_thread(reply["thread_id"])
    check("thread via References (inbox + sent)", len(th) == 3 and th[0]["subject"] == "Quarterly plan", [x["subject"] for x in th])

    d = g.create_draft("boss@acme.com", "", "Draft reply", reply_to_message_id=reply["id"])
    dr = g.search("in:drafts", 5)
    check("draft saved in Drafts", d["saved"] and d["folder"] == "Drafts" and dr and dr[0]["subject"] == "Re: Re: Quarterly plan"
          or dr[0]["subject"] == "Re: Quarterly plan", (d, dr))

    s = g.send("boss@acme.com", "Hello", "Body text", reply_to_message_id=reply["id"])
    check("send via SMTP (login)", s["sent"] and len(sink.msgs) == 1 and sink.msgs[0][1] == ["boss@acme.com"], sink.msgs)
    check("sent copy appended to Sent", s.get("saved_to_sent") is True and any(x["subject"] == "Re: Quarterly plan" or x["subject"] == "Hello"
                                                                          for x in g.search("in:sent subject:Hello", 5)), s)
    check("reply headers", b"In-Reply-To: <q2@acme.com>" in sink.msgs[0][2], sink.msgs[0][2][:400])

    old = next(x for x in g.search("in:inbox", 10) if x["subject"] == "Old news")
    a = g.archive([old["id"]])
    check("archive moves to Archive (created)", a["archived"] == 1 and "Archive" in g.list_labels(), a)
    arch = g.search("in:archive", 5)
    check("archived message found in Archive", [x["subject"] for x in arch] == ["Old news"] and arch[0]["id"].startswith("a-"), arch)
    check("default search covers Archive", "Old news" in [x["subject"] for x in g.search("older_than:30d", 10)])

    boss = next(x for x in g.search("in:inbox", 10) if x["subject"] == "Quarterly plan")
    lab = g.label([boss["id"]], add=["STARRED", "工作"])
    check("label: star + UTF-7 folder copy", lab.get("added") == 2 and "工作" in g.list_labels(), (lab, g.list_labels()))
    w = g.search("label:工作", 5)
    check("search label:<chinese folder>", [x["subject"] for x in w] == ["Quarterly plan"], w)
    check("is:starred", [x["subject"] for x in g.search("is:starred", 5)] == ["Quarterly plan"])
    lab = g.label([boss["id"]], remove=["STARRED"])
    check("unstar", lab.get("removed") == 1 and not g.search("is:starred", 5))
    mr = g.mark_read([sale["id"]], True)
    check("mark read", mr["updated"] == 1 and len(g.search("is:unread", 10)) == 2)
    tg = g.unsubscribe_targets([sale["id"]])
    check("unsubscribe targets", tg[0]["method"] == "one-click" and tg[0]["_unsub"]["https"].endswith("XYZ"), tg)

    bad = Gmail(USER, "wrong", "127.0.0.1", "127.0.0.1", provider="qq", imap_port=1143, imap_security="none")
    try:
        bad.test()
        check("bad password fails", False)
    except GmailError as e:
        check("bad password: provider-specific hint", "授权码" in str(e), e)


def outlook(sink):
    """Outlook path end to end: device code sign-in (fake Microsoft endpoint), XOAUTH2 to IMAP (Dovecot verifies the
    token through the same fake's introspection endpoint) and SMTP, token caching and refresh-token rotation."""
    import threading
    from app.sentinel import actions, mailproviders as mp
    from tests import fake_ms
    srv = fake_ms.serve()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mp.MS_LOGIN = "http://127.0.0.1:18999"
    cid = "11111111-2222-3333-4444-555555555555"
    try:
        mp.ms_device_start("not-a-guid", "consumers")
        check("MS: bad client id rejected locally", False)
    except mp.OAuthError as e:
        check("MS: bad client id rejected locally", "GUID" in str(e))
    try:
        mp.ms_device_start("99999999-2222-3333-4444-555555555555", "consumers")
        check("MS: unknown app -> readable error", False)
    except mp.OAuthError as e:
        check("MS: unknown app -> readable error", "AADSTS700016" in str(e), e)
    d = mp.ms_device_start(cid, "consumers")
    check("MS: device code", d["user_code"] == "ABCD-1234" and d["interval"] == 1, d)
    first = mp.ms_device_poll(cid, "consumers", d["device_code"])
    tok = mp.ms_device_poll(cid, "consumers", d["device_code"])
    check("MS: pending then token", first is None and tok and tok["refresh_token"] == "rt-1", (first, tok))

    class St:
        def __init__(self):
            self.s = {"cred_gmail_9": {"oauth": {"client_id": cid, "tenant": "consumers", "refresh_token": "rt-1"}}}

        def get_secret(self, h):
            return self.s.get(h)

        def put_secret(self, c, payload, handle=None):
            self.s[handle] = payload
    st = St()
    acc = {"id": "g9", "email": "me@outlook.com", "provider": "outlook", "imap_host": "127.0.0.1", "imap_port": 1143,
           "imap_security": "none", "smtp_host": "127.0.0.1", "smtp_port": 1025, "smtp_security": "none"}
    actions._MS_TOKENS.pop("g9", None)
    g = actions.make_client(acc, st.get_secret("cred_gmail_9"), st)
    t = g.test()
    check("Outlook: IMAP XOAUTH2 login + generic mode", t["ok"] and g.gm is False, t)
    check("Outlook: refresh token rotated and saved", st.s["cred_gmail_9"]["oauth"]["refresh_token"] == "rt-2", st.s)
    n_before = fake_ms.STATE["n"]
    g.test()
    check("Outlook: access token cached", fake_ms.STATE["n"] == n_before)
    r = g.send("friend@example.com", "Hi from Outlook", "Hello")
    check("Outlook: SMTP XOAUTH2 send", r["sent"] and sink.xoauth and sink.msgs[-1][1] == ["friend@example.com"], sink.xoauth)
    check("Outlook: sent copy kept", r.get("saved_to_sent") is True, r)
    st.s["cred_gmail_9"]["oauth"]["refresh_token"] = "revoked"
    actions._MS_TOKENS.pop("g9", None)
    g2 = actions.make_client(acc, st.get_secret("cred_gmail_9"), st)
    try:
        g2.test()
        check("Outlook: revoked token -> reconnect hint", False)
    except GmailError as e:
        check("Outlook: revoked token -> reconnect hint", "重新登录" in str(e), e)
    srv.shutdown()


def api():
    """Connect the same mailbox through Sentinel's API on a local stack, then run gmail_search as the agent would."""
    import httpx
    B, H = "http://127.0.0.1:8080", {"X-Persona-UI": "1"}
    c = httpx.Client(timeout=60, trust_env=False)
    body = {"provider": "custom", "email": USER, "app_password": PW, "display_name": "Tester",
            "imap_host": "127.0.0.1", "imap_port": 1143, "imap_security": "none",
            "smtp_host": "127.0.0.1", "smtp_port": 1025, "smtp_security": "none"}
    r = c.post(B + "/sentinel/api/connections/gmail/credential", json={**body, "app_password": "wrong"}, headers=H)
    check("API: wrong password rejected, nothing saved", r.status_code == 400, r.text)
    r = c.post(B + "/sentinel/api/connections/gmail/credential", json={**body, "imap_host": "10.0.0.5"}, headers=H)
    check("API: unencrypted non-loopback refused", r.status_code == 400 and "localhost" in r.text, r.text)
    r = c.post(B + "/sentinel/api/connections/gmail/credential", json=body, headers=H)
    check("API: custom IMAP connected", r.status_code == 200 and r.json()["ok"], r.text)
    conn = c.get(B + "/sentinel/api/connections", headers=H).json()
    gm = next(x for x in conn["connections"] if x["name"] == "gmail")
    acc = next((a for a in gm["accounts"] if a["email"] == USER), None)
    check("API: account listed with provider", acc and acc["provider"] == "custom" and acc["ready"], gm["accounts"])
    check("API: presets exposed", any(p["key"] == "outlook" and p["auth"] == "oauth" for p in gm["providers"]), "")
    r = c.post(B + "/sentinel/api/connections/gmail/test", json={"account": acc["id"]}, headers=H).json()
    check("API: test endpoint", r.get("ok") and r["test"]["provider"] == "custom", r)
    r = c.post(B + "/sentinel/api/connections/gmail/oauth/start", json={"email": "me@outlook.com", "client_id": "nope"}, headers=H)
    check("API: Outlook bad client id rejected", r.status_code == 400 and "GUID" in r.text, r.text)
    r = c.post(B + "/sentinel/api/connections/gmail/credential", json={"email": "me@outlook.com", "app_password": "x" * 16}, headers=H)
    check("API: Outlook requires Microsoft sign-in", r.status_code == 400 and "Microsoft" in r.text, r.text)
    c.delete(B + f"/sentinel/api/connections/gmail/accounts/{acc['id']}", headers=H)


if __name__ == "__main__":
    main()
