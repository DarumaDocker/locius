"""Vault: ID / membership / card numbers the user stores once and approves on every single use.

Values are Fernet-encrypted in the `secrets` table (handle ``vault_<id>``); only metadata lives in
``vault_items``. Nothing here ever returns a value to the runtime or the web UI: the agent asks for a
fill with ``browser_fill_secret(ref, item_id, field)``, the user approves that one fill, and Sentinel
types the value into the page itself. Afterwards every page text that leaves Sentinel is scrubbed of
vault numbers, and vision (browser_look / browser_locate) is refused on a page that holds a filled value.
"""
from __future__ import annotations

import re
import time

from app.common.util import dumps, loads, new_id, now_ts

KINDS: dict[str, dict] = {
    "id_document": {"label": "证件 ID document", "fields": ["number", "name", "expiry", "country"]},
    "membership": {"label": "会员 Membership", "fields": ["number", "name", "program"]},
    "card": {"label": "银行卡 Card", "fields": ["number", "expiry", "cvc", "holder"]},
    "other": {"label": "其他 Other", "fields": ["value"]},
}
FIELD_LABELS = {"number": "号码 number", "name": "姓名 name", "expiry": "有效期 expiry", "country": "国家 country",
                "program": "计划 program", "cvc": "安全码 CVC", "holder": "持卡人 holder", "value": "内容 value"}
# fields whose values are scrubbed from page text after use (short or common values like CVC / names are not,
# snapshot.js already hides card-security fields)
SCRUB_FIELDS = ("number", "value")

SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_items (
  id TEXT PRIMARY KEY, label TEXT, kind TEXT, masked TEXT, fields TEXT, domains TEXT,
  created_at REAL, updated_at REAL, last_used REAL, uses INTEGER DEFAULT 0
);
"""

_filled: dict[str, dict] = {}      # task_id -> {"urls": set[str], "ts": float}
_patterns: list[re.Pattern] | None = None


def init(store) -> None:
    store.db.script(SCHEMA)


def _mask(v: str) -> str:
    s = re.sub(r"[\s-]", "", str(v or ""))
    if not s:
        return ""
    return "•••• " + s[-4:] if len(s) > 4 else "•" * len(s)


def _clean_domains(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[\s,]+", v)
    out = []
    for d in v or []:
        d = str(d).strip().lower()
        d = re.sub(r"^https?://", "", d).split("/")[0].removeprefix("www.")
        if d and re.fullmatch(r"[a-z0-9.-]+", d):
            out.append(d)
    return out[:20]


def _row(r: dict) -> dict:
    return {"id": r["id"], "label": r["label"], "kind": r["kind"], "kind_label": KINDS.get(r["kind"], {}).get("label", r["kind"]),
            "masked": r["masked"], "fields": loads(r["fields"], []), "domains": loads(r["domains"], []),
            "created_at": r["created_at"], "updated_at": r["updated_at"], "last_used": r["last_used"], "uses": r["uses"] or 0}


def list_items(store) -> list[dict]:
    return [_row(r) for r in store.db.all("SELECT * FROM vault_items ORDER BY created_at")]


def item(store, iid: str) -> dict | None:
    r = store.db.one("SELECT * FROM vault_items WHERE id=?", (str(iid),))
    return _row(r) if r else None


def save_item(store, data: dict, iid: str | None = None) -> dict:
    """Create or update an item. On update, empty field values keep the stored ones."""
    global _patterns
    kind = str(data.get("kind") or "other")
    if kind not in KINDS:
        raise ValueError("unknown kind")
    label = str(data.get("label") or "").strip()[:80]
    if not label:
        raise ValueError("请填写名称 label is required")
    old = (store.get_secret(f"vault_{iid}") or {}) if iid else {}
    if iid and not item(store, iid):
        raise KeyError(iid)
    vals = {}
    for f in KINDS[kind]["fields"]:
        v = str((data.get("values") or {}).get(f) or "").strip()[:200]
        vals[f] = v or old.get(f, "")
    main = "value" if kind == "other" else "number"
    if not vals.get(main):
        raise ValueError("请填写号码 the number is required")
    iid = iid or new_id("vlt")
    store.put_secret("vault", vals, handle=f"vault_{iid}")
    row = {"label": label, "kind": kind, "masked": _mask(vals[main]), "fields": dumps([f for f, v in vals.items() if v]),
           "domains": dumps(_clean_domains(data.get("domains"))), "updated_at": now_ts()}
    if store.db.one("SELECT id FROM vault_items WHERE id=?", (iid,)):
        store.db.update("vault_items", "id", iid, row)
    else:
        store.db.insert("vault_items", {"id": iid, "created_at": now_ts(), "last_used": None, "uses": 0, **row})
    _patterns = None
    return item(store, iid)


def delete_item(store, iid: str) -> bool:
    global _patterns
    if not item(store, iid):
        return False
    store.db.execute("DELETE FROM vault_items WHERE id=?", (iid,))
    store.delete_secret(f"vault_{iid}")
    _patterns = None
    return True


def value(store, iid: str, field: str) -> str:
    sec = store.get_secret(f"vault_{iid}") or {}
    return str(sec.get(field) or "")


def mark_used(store, iid: str) -> None:
    store.db.execute("UPDATE vault_items SET last_used=?, uses=COALESCE(uses,0)+1 WHERE id=?", (now_ts(), iid))


def domain_ok(it: dict, dom: str) -> bool:
    allowed = it.get("domains") or []
    if not allowed:
        return True
    dom = (dom or "").lower().removeprefix("www.")
    return any(dom == d or dom.endswith("." + d) for d in allowed)


# ------------------------------------------------------------------ after-fill protection
def note_fill(task_id: str, url: str) -> None:
    rec = _filled.setdefault(task_id, {"urls": set(), "ts": 0.0})
    rec["urls"].add(_page_key(url))
    rec["ts"] = time.time()


def _page_key(url: str) -> str:
    return str(url or "").split("#")[0]


def filled_here(task_id: str, url: str) -> bool:
    rec = _filled.get(task_id)
    if not rec:
        return False
    if time.time() - rec["ts"] > 6 * 3600:
        _filled.pop(task_id, None)
        return False
    return _page_key(url) in rec["urls"]


def _compile(store) -> list[re.Pattern]:
    global _patterns
    if _patterns is not None:
        return _patterns
    pats = []
    for it in list_items(store):
        sec = store.get_secret(f"vault_{it['id']}") or {}
        for f in SCRUB_FIELDS:
            v = str(sec.get(f) or "").strip()
            compact = re.sub(r"[\s-]", "", v)
            if len(compact) < 5:
                continue
            if compact.isdigit() or re.fullmatch(r"[A-Za-z0-9]+", compact):
                # tolerate the spacing / dashes sites add ("4111 1111 1111 1111", "4111-1111-…")
                pats.append(re.compile(r"(?<![A-Za-z0-9])" + r"[\s-]?".join(re.escape(c) for c in compact) + r"(?![A-Za-z0-9])", re.I))
            else:
                pats.append(re.compile(re.escape(v), re.I))
    _patterns = pats
    return pats


def scrub(store, obj, _depth: int = 0):
    """Remove every stored vault number from strings inside obj (dicts / lists). Images are left alone."""
    pats = _compile(store)
    if not pats or _depth > 6:
        return obj
    if isinstance(obj, str):
        for p in pats:
            obj = p.sub("[VAULT_VALUE]", obj)
        return obj
    if isinstance(obj, dict):
        return {k: (v if k in ("image_b64",) else scrub(store, v, _depth + 1)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(store, v, _depth + 1) for v in obj]
    return obj


def summary_text(it: dict, field: str) -> str:
    f = FIELD_LABELS.get(field, field)
    shown = it["masked"] if field in ("number", "value") else "（已隐藏 hidden）"
    return f"{it['label']} · {f} · {shown}"
