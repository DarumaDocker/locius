"""Transaction ledger (roadmap batch 3): every purchase OMuse made for the user — merchant, order number, amount, card
(masked), status and the proof behind each status — so "cancel / return / where is my order" always points at a real
order (2026-10-04: a follow-up about the Decathlon order went to an Amazon order instead).

Life of an entry:
  approved          the user approved the purchase card (purchase_confirm) — nothing placed yet
  placed            the task ended with an order number the confirmation page showed
  unconfirmed       the task ended without an order number (check the merchant's site / email)
  shipped / delivered / cancelled / refunded     from the merchant's emails (reconcile) or the merchant's page
  cancel_requested / return_requested            OMuse clicked cancel / return for it, approved by the user
"""
from __future__ import annotations

import re
import time

from app.common.util import dumps, loads, new_id

OPEN = ("approved", "placed", "shipped", "unconfirmed", "cancel_requested", "return_requested")
LABEL = {"approved": "已批准，未下单 approved", "placed": "已下单 placed", "unconfirmed": "未确认 unconfirmed",
         "shipped": "已发货 shipped", "delivered": "已送达 delivered", "cancel_requested": "已申请取消 cancel requested",
         "return_requested": "已申请退货 return requested", "cancelled": "已取消 cancelled", "refunded": "已退款 refunded"}
# what a merchant's email says, most final first
_MAIL_STATUS = [
    ("refunded", re.compile(r"refund(ed| (has been |was )?(issued|processed|completed))|已退款|退款(已|成功|完成)", re.I)),
    ("cancelled", re.compile(r"(order|booking)\b.{0,40}\b(has been |was |is )?cancel(l)?ed|cancellation confirmed|订单已取消|已取消", re.I)),
    ("delivered", re.compile(r"\bdelivered\b|has arrived|已送达|已签收|派送完成", re.I)),
    ("shipped", re.compile(r"\b(shipped|dispatched|on (its|the) way|out for delivery|in transit)\b|已发货|已出库|派送中|运输中", re.I)),
    ("placed", re.compile(r"order (confirmation|confirmed|received)|thank(s| you) for your (order|purchase)|订单确认|已收到(你|您)的订单", re.I)),
]
_RANK = {"approved": 0, "unconfirmed": 1, "placed": 2, "cancel_requested": 3, "return_requested": 3, "shipped": 4,
         "delivered": 5, "cancelled": 6, "refunded": 7}


def _row(r: dict | None) -> dict | None:
    if not r:
        return None
    for k, d in (("items", []), ("evidence", []), ("history", [])):
        r[k] = loads(r.get(k), d) or d
    r["status_label"] = LABEL.get(r["status"], r["status"])
    return r


def add(store, task_id: str, merchant: str, total: float, currency: str, items=None, card: str = "", delivery: str = "",
        purchase_id: str = "", approval_id: str = "") -> dict:
    lid = new_id("ord")
    now = time.time()
    store.db.insert("ledger", {"id": lid, "task_id": task_id or "", "purchase_id": purchase_id, "approval_id": approval_id,
                               "merchant": merchant or "", "order_number": "", "items": dumps(items or []),
                               "total": float(total or 0), "currency": currency or "", "card": card or "",
                               "delivery": delivery or "", "status": "approved", "evidence": "[]",
                               "history": dumps([{"ts": now, "status": "approved", "by": "user",
                                                  "note": f"批准购买 approval {approval_id}".strip()}]),
                               "created_at": now, "updated_at": now})
    return get(store, lid)


def get(store, lid: str) -> dict | None:
    return _row(store.db.one("SELECT * FROM ledger WHERE id=?", (lid,)))


def entries(store, limit: int = 100, status: str | None = None) -> list[dict]:
    if status == "open":
        rows = store.db.all(f"SELECT * FROM ledger WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY created_at DESC LIMIT ?",
                            (*OPEN, limit))
    else:
        rows = store.db.all("SELECT * FROM ledger ORDER BY created_at DESC LIMIT ?", (limit,))
    return [_row(r) for r in rows]


def for_task(store, task_id: str) -> list[dict]:
    return [_row(r) for r in store.db.all("SELECT * FROM ledger WHERE task_id=? ORDER BY created_at", (task_id,))]


def by_number(store, number: str) -> dict | None:
    n = re.sub(r"\W", "", str(number or "")).upper()
    if len(n) < 5:
        return None
    for r in store.db.all("SELECT * FROM ledger WHERE order_number!=''"):
        if re.sub(r"\W", "", r["order_number"]).upper() == n:
            return _row(r)
    return None


def in_text(store, text: str) -> list[dict]:
    """Ledger orders whose number appears on a page / in an email."""
    t = re.sub(r"[\s\-]", "", str(text or "")).upper()
    out = []
    for r in store.db.all("SELECT * FROM ledger WHERE order_number!=''"):
        n = re.sub(r"[\s\-]", "", r["order_number"]).upper()
        if len(n) >= 5 and n in t:
            out.append(_row(r))
    return out


def set_status(store, lid: str, status: str, by: str, note: str = "", evidence: dict | None = None,
               only_forward: bool = False) -> dict | None:
    r = get(store, lid)
    if not r:
        return None
    if only_forward and _RANK.get(status, 0) <= _RANK.get(r["status"], 0):
        status = r["status"]
    now = time.time()
    hist = r["history"] + [{"ts": now, "status": status, "by": by, "note": note[:300]}]
    ev = r["evidence"] + ([{**evidence, "ts": now}] if evidence else [])
    store.db.update("ledger", "id", lid, {"status": status, "history": dumps(hist[-40:]), "evidence": dumps(ev[-40:]),
                                         "updated_at": now})
    return get(store, lid)


def confirm(store, task_id: str, order_number: str = "", total: float | None = None, evidence: dict | None = None) -> list[dict]:
    """The purchase task finished: record the order number it got (or that it got none)."""
    out = []
    for r in for_task(store, task_id):
        if r["status"] != "approved":
            continue
        upd = {}
        if order_number:
            upd["order_number"] = order_number.strip()[:40]
        if total is not None:
            upd["total"] = float(total)
        if upd:
            store.db.update("ledger", "id", r["id"], upd)
        out.append(set_status(store, r["id"], "placed" if order_number else "unconfirmed", "omuse",
                              f"订单号 {order_number}" if order_number else "任务结束时没有拿到订单号 no order number",
                              evidence))
    return out


def status_from_mail(subject: str, snippet: str = "") -> str:
    text = f"{subject}\n{snippet}"
    for st, rx in _MAIL_STATUS:
        if rx.search(text):
            return st
    return ""


def reconcile(store, search) -> dict:
    """Match merchants' emails to open orders by order number. `search(query) -> [{id, subject, snippet, from, date}]`."""
    checked, changed = 0, []
    for r in entries(store, 200, "open"):
        if not r["order_number"]:
            continue
        checked += 1
        try:
            msgs = search(f'"{r["order_number"]}" newer_than:90d') or []
        except Exception:
            continue
        best, best_msg = "", None
        for m in msgs:
            st = status_from_mail(str(m.get("subject") or ""), str(m.get("snippet") or ""))
            if st and _RANK.get(st, 0) > _RANK.get(best, -1):
                best, best_msg = st, m
        if best and _RANK.get(best, 0) > _RANK.get(r["status"], 0):
            ev = {"kind": "email", "id": str(best_msg.get("id") or ""), "subject": str(best_msg.get("subject") or "")[:200],
                  "from": str(best_msg.get("from") or "")[:120], "date": str(best_msg.get("date") or "")}
            set_status(store, r["id"], best, "email", f"邮件：{ev['subject']}", ev)
            changed.append({"id": r["id"], "order_number": r["order_number"], "from": r["status"], "to": best})
    return {"checked": checked, "changed": changed}


def backfill(store) -> list[str]:
    """Ledger entries for purchases approved before the ledger existed (from the one-card purchase records)."""
    made = []
    for p in store.db.all("SELECT * FROM purchases ORDER BY created_at"):
        if store.db.one("SELECT id FROM ledger WHERE purchase_id=? OR (task_id=? AND task_id!='')", (p["id"], p["task_id"])):
            continue
        summ = loads(p.get("summary"), {}) or {}
        ap = store.db.one("SELECT id FROM approvals WHERE task_id=? AND tool='purchase_confirm' AND status='approved' "
                          "ORDER BY resolved_at DESC", (p["task_id"],))
        r = add(store, p["task_id"], p["domain"], p["max_total"], p["currency"], summ.get("items") or [], "",
                str(summ.get("delivery") or ""), p["id"], ap["id"] if ap else "")
        store.db.update("ledger", "id", r["id"], {"created_at": p["created_at"]})
        made.append(r["id"])
    return made
