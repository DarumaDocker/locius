"""Content guards: secret redaction, prompt-injection heuristics, URL safety, data classes.

Everything that leaves Sentinel towards the Agent Runtime (and therefore the LLM)
passes through `redact_secrets`. Everything that comes from the outside world is
scanned by `scan_injection` and wrapped as untrusted.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse, parse_qsl

# ---------------------------------------------------------------- data classes
PUBLIC, PERSONAL, CONFIDENTIAL, SECRET = "PUBLIC", "PERSONAL", "CONFIDENTIAL", "SECRET"
LEVELS = {PUBLIC: 0, PERSONAL: 1, CONFIDENTIAL: 2, SECRET: 3}


def max_class(a: str, b: str) -> str:
    return a if LEVELS.get(a, 0) >= LEVELS.get(b, 0) else b


# ---------------------------------------------------------------- secret redaction
_OTP_CONTEXT = re.compile(
    r"(verification|one[- ]?time|otp|passcode|security code|sign[- ]?in code|login code|2fa|two[- ]factor|"
    r"auth(entication)? code|confirm(ation)? code|验证码|校验码|动态码|认证码|確認コード|認証コード|ワンタイム)",
    re.I,
)
_CODE = re.compile(r"(?<![\w/.\-])(\d{4,8}|[A-Z0-9]{3,4}-[A-Z0-9]{3,4})(?![\w/\-]|\.\d)")
_RECOVERY = re.compile(r"(recovery|backup|恢复|备用)\s*(code|codes|码)", re.I)
_KEY_PATTERNS = [
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\b\d{8,10}:AA[0-9A-Za-z_\-]{30,}\b"),  # telegram bot token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|access[_-]?token|client[_-]?secret|password|passwd|pwd)\b\s*[:=]\s*\S{6,}"),
]
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_SENSITIVE_PARAM = re.compile(r"^(token|code|otp|key|sig|signature|auth|magic|reset|access_token|id_token|oobcode|ticket|nonce|session|sid)$", re.I)
_SENSITIVE_PATH = re.compile(r"(reset[-_]?password|password[-_]?reset|magic[-_]?link|verify[-_]?email|/login/(token|link)|/auth/(callback|confirm)|/confirm|/activate|/signin/[A-Za-z0-9_\-]{16,})", re.I)
_SECURITY_SUBJECT = re.compile(
    r"(verification code|security code|password reset|reset your password|sign[- ]in (link|code|attempt)|"
    r"magic link|one[- ]time|2fa|two[- ]factor|login link|验证码|重置密码|登录链接|安全码|確認コード|パスワード再設定)",
    re.I,
)


def _looks_like_token(seg: str) -> bool:
    """Opaque high-entropy strings (tokens), not readable slugs like 'excel-now-supports-multiple-values'."""
    if len(seg) < 32 or not re.fullmatch(r"[A-Za-z0-9_\-.=~]+", seg):
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,}", seg):
        return True
    words = [w for w in re.split(r"[-_.]", seg) if w]
    if len(words) >= 3 and all(w.isalpha() and len(w) <= 15 for w in words):
        return False  # human-readable slug
    has_digit = any(c.isdigit() for c in seg)
    has_upper = any(c.isupper() for c in seg)
    has_lower = any(c.islower() for c in seg)
    return has_digit and has_upper and has_lower


def _redact_url(url: str) -> str:
    try:
        p = urlparse(url)
    except Exception:
        return "[REDACTED_LINK]"
    if _SENSITIVE_PATH.search(p.path or ""):
        return f"[REDACTED_LINK {p.netloc}]"
    params = parse_qsl(p.query, keep_blank_values=True)
    for k, v in params:
        if _SENSITIVE_PARAM.match(k) and len(v) >= 6:
            return f"[REDACTED_LINK {p.netloc}]"
    # very long opaque path segments look like tokens
    for seg in (p.path or "").split("/"):
        if _looks_like_token(seg):
            return f"[REDACTED_LINK {p.netloc}]"
    return url


def redact_secrets(text: str) -> tuple[str, int]:
    """Return (redacted_text, count). Never lets OTPs, magic links, keys, recovery codes through."""
    if not text:
        return text or "", 0
    count = 0

    def sub_key(m: re.Match) -> str:
        nonlocal count
        count += 1
        return "[REDACTED_SECRET]"

    for pat in _KEY_PATTERNS:
        text = pat.sub(sub_key, text)

    def sub_url(m: re.Match) -> str:
        nonlocal count
        new = _redact_url(m.group(0))
        if new != m.group(0):
            count += 1
        return new

    text = _URL.sub(sub_url, text)

    # OTP codes: redact numeric codes that appear on a line near an OTP keyword
    lines = text.split("\n")
    for i, line in enumerate(lines):
        window = " ".join(lines[max(0, i - 2): i + 3])
        if _OTP_CONTEXT.search(window) or _RECOVERY.search(window):
            new_line, n = _CODE.subn("[REDACTED_CODE]", line)
            if n:
                count += n
                lines[i] = new_line
    text = "\n".join(lines)
    return text, count


def is_security_message(subject: str, sender: str = "") -> bool:
    return bool(_SECURITY_SUBJECT.search(subject or ""))


# ---------------------------------------------------------------- injection heuristics
_INJECTION_PATTERNS = [
    (r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|rules)", "ignore-previous"),
    (r"disregard (all |any |the )?(previous|prior|above|your) (instructions|rules|guidelines)", "disregard"),
    (r"forget (all |your )?(previous|prior) (instructions|rules)", "forget-previous"),
    (r"\b(system|developer) (message|prompt|instruction)s?\s*[:：]", "fake-system"),
    (r"<\|?(im_start|im_end|system|endoftext)\|?>", "special-tokens"),
    (r"\byou are now\b", "role-hijack"),
    (r"\bnew instructions?\s*[:：]", "new-instructions"),
    (r"\b(ai|llm|assistant|agent)s?\b.{0,40}\b(must|should|need to|have to)\b.{0,60}\b(send|forward|upload|transfer|email|delete|pay|click)", "agent-directive"),
    (r"\bforward (all|every|the last|recent)\b.{0,40}\b(e-?mails?|messages?|files?)", "bulk-forward"),
    (r"\b(send|upload|exfiltrate|post)\b.{0,40}\b(password|credentials?|api key|token|private|confidential|\.pdf|documents?)\b.{0,40}\bto\b", "exfil"),
    (r"(忽略|无视|忘记)(之前|以上|先前|所有)的?(指令|指示|规则|提示)", "zh-ignore"),
    (r"(系统|开发者)(消息|提示|指令)\s*[:：]", "zh-fake-system"),
    (r"(把|将).{0,20}(邮件|文件|密码|资料).{0,20}(转发|发送|上传)(到|给)", "zh-exfil"),
    (r"(以前の|これまでの)(指示|命令)を無視", "ja-ignore"),
]
_INJ = [(re.compile(p, re.I | re.S), name) for p, name in _INJECTION_PATTERNS]


def scan_injection(text: str) -> list[str]:
    if not text:
        return []
    hits = []
    sample = text[:50000]
    for pat, name in _INJ:
        if pat.search(sample):
            hits.append(name)
    return hits


def wrap_untrusted(source: str, content, *, meta: dict | None = None) -> dict:
    """Envelope for any external content handed to the runtime."""
    text = content if isinstance(content, str) else None
    flags = scan_injection(text) if text else []
    env = {"trust": "untrusted", "source": source, **(meta or {})}
    if flags:
        env["injection_warning"] = flags
    env["content"] = content
    return env


# ---------------------------------------------------------------- URL / network safety
_BLOCKED_SCHEMES = {"file", "chrome", "chrome-extension", "javascript", "data", "about", "view-source", "devtools", "ftp", "ws", "wss"}
_BLOCKED_HOST_SUFFIX = (".local", ".localhost", ".internal", ".svc", ".cluster.local", ".svc.cluster.local", ".lan", ".home.arpa")


def host_is_private(host: str) -> bool:
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host:
        return True
    if host in ("localhost", "metadata.google.internal", "metadata"):
        return True
    if host.endswith(_BLOCKED_HOST_SUFFIX):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    except ValueError:
        pass
    if "." not in host:  # bare service names inside the cluster
        return True
    return False


def check_url(url: str) -> tuple[bool, str]:
    try:
        p = urlparse(url.strip())
    except Exception:
        return False, "无法解析的 URL (unparseable URL)"
    scheme = (p.scheme or "").lower()
    if scheme in _BLOCKED_SCHEMES:
        return False, f"禁止的协议 scheme '{scheme}'"
    if scheme not in ("http", "https"):
        return False, "只允许 http/https (only http/https allowed)"
    if host_is_private(p.hostname or ""):
        return False, "禁止访问内网/本机地址 (private or cluster-internal address blocked)"
    return True, ""


def domain_of(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


# ---------------------------------------------------------------- risky UI words
_RISKY_CLICK = re.compile(
    r"(submit|send|pay|purchase|buy|checkout|check out|place order|order now|confirm|delete|remove|cancel (my )?(subscription|order|account|plan)|"
    r"unsubscribe|transfer|withdraw|donate|book now|reserve|sign up|register|apply|agree|accept|authorize|approve|refund|post|publish|"
    r"提交|发送|支付|付款|购买|下单|确认|删除|移除|取消订阅|注销|转账|提现|预订|注册|申请|同意|授权|发布|退款|"
    r"送信|購入|注文|確定|削除|支払|申し込)",
    re.I,
)
_SEARCH_WORDS = re.compile(r"(search|find|query|filter|搜索|查找|搜寻|検索)", re.I)


# Putting something in a shopping cart is reversible and costs nothing, but shops (Amazon included) build the button as
# <input type="submit">. Treat a plain "add to cart/basket/bag" as a normal click; buying / checkout still need approval.
_CART_ADD = re.compile(r"^\s*(add to (cart|basket|bag|trolley)|add to shopping (cart|bag)|加入购物车|加入購物車|添加到购物车|放入购物车|"
                       r"カートに入れる|장바구니 담기)"
                       # Amazon appends its keyboard shortcut to the accessible name: "Add to cart, shift, Alt, K"
                       r"(\s*,\s*(shift|alt|ctrl|control|cmd|command|option|meta|[a-z0-9]))*\s*$", re.I)


# Cookie / consent banners: refusing optional cookies is the privacy-preserving choice and needs no approval
# (accepting them still does). The 2026-10-02 research test stalled on an "Accept all" approval.
_COOKIE_DECLINE = re.compile(r"^\s*(reject( all)?( cookies)?|decline( all)?( cookies)?|deny( all)?|refuse( all)?|"
                             r"(use |accept )?(only )?(strictly )?(necessary|essential)( cookies)?( only)?|continue without accepting|"
                             r"拒绝(全部|所有)?(cookie|Cookie)?|仅(接受)?必要(的)?(cookie|Cookie)?|只接受必要|全部拒绝|"
                             r"すべて拒否|拒否する|必要なもののみ)\s*$", re.I)


# Marketplace badges that contain a risky word but are not actions (2026-10-04 M3-21: a Carousell listing card
# "Buyer Protection · MacBook Air M5 · S$2,450" asked for approval)
_BADGES = re.compile(r"(buyer|purchase|payment) protection|free (returns|delivery)|apply (coupon|voucher)s? at checkout", re.I)


_AGREE = re.compile(r"agree|accept|consent|terms|authori[sz]e|同意|接受|授权|条款", re.I)


def click_is_risky(role: str, name: str, input_type: str = "") -> bool:
    label = _BADGES.sub(" ", f"{name or ''}")
    r = (role or "").lower()
    # choosing an option is not submitting (2026-10-04 V3-08: the "Pay monthly / Pay yearly" switch on a pricing page
    # needed approval); ticking "I agree to the terms" still does
    if r in ("radio", "tab", "option", "switch", "menuitemradio", "day") or (r == "checkbox" and not _AGREE.search(label)):
        return False
    if (role or "").lower() == "link" and ("\n" in label.strip() or len(label) > 80):
        # a whole product / listing card is one link: judge it by its first line, like a person reading the title
        label = label.strip().split("\n")[0][:80]
    if _CART_ADD.search(label) or _COOKIE_DECLINE.search(label):
        return False
    if (input_type or "").lower() == "submit":
        return True
    if _SEARCH_WORDS.search(label):
        return False
    return bool(_RISKY_CLICK.search(label))


_NAMED_NEVER = re.compile(
    r"pay|payment|buy|purchase|order|checkout|check out|place|send|transfer|donate|subscribe|book|reserve|confirm|sign|agree|"
    r"accept|delete account|close account|withdraw|付款|支付|购买|下单|结账|结算|发送|转账|捐|订阅|预订|预约|订位|确认|签|同意|注销|提现",
    re.I)
_NEGATED = re.compile(r"(不要|别|不用|勿|先别|don'?t|do not|never|not)\s*(点|按|click|press|hit|tap)?\s*(击)?\s*[「『\"'“]?\s*$", re.I)


_SUBMIT_VERBS = ("提交", "submit", "添加", "新增", "加一条", "add a", "add the", "保存", "save")
_GENERIC_BTN = {"submit": _SUBMIT_VERBS, "save": _SUBMIT_VERBS, "add": _SUBMIT_VERBS, "ok": _SUBMIT_VERBS,
                "apply": _SUBMIT_VERBS, "done": _SUBMIT_VERBS, "提交": _SUBMIT_VERBS, "保存": _SUBMIT_VERBS, "添加": _SUBMIT_VERBS}


def _asked_verb(request: str, verbs) -> bool:
    low = str(request or "").lower()
    if verbs is _SUBMIT_VERBS and re.search(r"(不要|别|先别|不用|勿|don'?t|do not|never)\s*(点|按|click|press)?\s*[「『\"'“*]*\s*(提交|submit)", low):
        return False
    for v in verbs:
        i = low.find(v)
        while i >= 0:
            if not _NEGATED.search(request[max(0, i - 12):i]):
                return True
            i = low.find(v, i + 1)
    return False


def user_asked_upload(request: str, page_url: str, path: str) -> bool:
    """The user's own request asks to upload one of their attached files on the site they named (2026-10-04: test pages
    stopped at an approval card for the file the user had just attached and told OMuse to upload there)."""
    req = str(request or "")
    dom = domain_of(page_url or "")
    if not dom or not str(path or "").startswith("uploads/"):
        return False
    named = dom in req.lower() or dom.removeprefix("www.") in req.lower()
    return named and _asked_verb(req, ("上传", "upload", "选择附件", "选好文件", "选择文件", "attach"))


def user_named_click(request: str, label: str) -> bool:
    """The user's own request explicitly asks to click this button (e.g. "先点 Remove") and the button is not about money,
    sending, booking, signing or accepting terms. Then the consequential-click approval is skipped: the user already
    said so. 2026-10-04 (Lucas): "先放宽一些吧，尽量让这个流程往下走" after "click Remove" on a test page needed approval."""
    lab = re.sub(r"\s+", " ", str(label or "")).strip()
    req = str(request or "")
    if len(lab) < 2 or len(lab) > 40 or _NAMED_NEVER.search(lab):
        return False
    low = req.lower()
    # generic form buttons count as named when the user asked for that operation in words ("添加一条记录" → Submit)
    if lab.lower() in _GENERIC_BTN and _asked_verb(req, _GENERIC_BTN[lab.lower()]):
        return True
    i = low.find(lab.lower())
    while i >= 0:
        before = req[max(0, i - 12):i]
        if not _NEGATED.search(before):
            return True
        i = low.find(lab.lower(), i + 1)
    return False


def looks_like_search(role: str, name: str) -> bool:
    return (role or "").lower() in ("searchbox", "combobox") or bool(_SEARCH_WORDS.search(name or ""))
