"""Outcome evidence: a task that changed something in the world (an order, a booking, an email, a calendar event, a call)
is only "done" when there is proof — an order number on the confirmation page, a message id, an event id.

`assess()` reads what the task actually did (its events) and what the tools returned, and compares that with what the final
answer claims. It is pure (no I/O) so it can be tested and re-run on old tasks.

    verified    every consequential action has evidence
    unverified  the answer says something was done, but the proof is missing (or nothing was done at all)
    none        the task changed nothing and claims nothing (a question, a search, a summary)
"""
from __future__ import annotations

import re

SEND_TOOLS = {"gmail_send", "gmail_reply", "gmail_forward", "slack_send_message"}
CALENDAR_TOOLS = {"calendar_create_event", "calendar_update_event", "calendar_delete_event"}
NOTION_TOOLS = {"notion_create_page", "notion_update_page", "notion_append"}
CALL_TOOLS = {"phone_call"}

# "I did it" in an answer. Kept to past-tense / result wording so "I can book it for you" is not a claim.
_CLAIM = {
    "purchase": re.compile(r"下单成功|已下单|已经下单|订单已提交|已完成(购买|支付|付款)|支付成功|付款成功|购买成功|"
                           r"order (has been |was )?(placed|confirmed|submitted)|purchase (is )?complete|payment (was )?successful",
                           re.I),
    "booking": re.compile(r"预订成功|已预订|已经预订|订好了|已订好|预约成功|已预约|"
                          r"\b(booked|reserved)\b|booking (is )?confirmed|reservation (is )?confirmed", re.I),
    "send": re.compile(r"已发送|已经发送|已发出|已回复|已转发|邮件已发|\b(sent|replied|forwarded) (the |your |an? )?(e-?mail|message|reply)",
                       re.I),
    "calendar": re.compile(r"已(添加|加入|创建|建立|更新|修改|删除|取消)[^。\n]{0,12}(日历|日程|事件|会议)|"
                           r"(日历|日程)[^。\n]{0,8}已(添加|创建|更新|删除)|(added|created|updated|deleted) (it |the event )?(to|on|in|from) (your )?calendar",
                           re.I),
    "call": re.compile(r"已(经)?(拨打|打了|打过)(电话)?|电话已(打|拨)|\bI (called|phoned)\b", re.I),
}
# not a claim when the sentence says it did NOT happen
_NEGATED = re.compile(r"(没有|未|尚未|还没|无法|不能|并未|没能|失败|not|n't|unable|couldn't|failed)", re.I)

# an order / booking / confirmation number as people write it next to its label
_REF = re.compile(r"(?:订单号|订单编号|订单|预订号|确认号|确认码|预约号|参考号|order\s*(?:no\.?|number|#|id)?|booking\s*(?:ref(?:erence)?|no\.?|number|id|code)?|"
                  r"confirmation\s*(?:no\.?|number|code|#)?|reference\s*(?:no\.?|number)?|ref\.?)"
                  r"\s*\**\s*[:：#]?\s*\**\s*([A-Z0-9][A-Z0-9\-]{4,24})", re.I)


# what the request asks to have done: an answer's claim only counts for these (a summary of emails saying "Jennifer has
# replied" is not OMuse claiming it sent something)
_INTENT = {
    "purchase": re.compile(r"买|购买|下单|订购|结账|付款|\b(buy|order|purchase|checkout|pay)\b", re.I),
    "booking": re.compile(r"预订|订位|订座|订餐|订票|订房|预约|订[^，。,.]{0,10}(餐厅|位子|座位|酒店|机票|门票|桌)|"
                          r"\b(book|reserve|reservation)\b", re.I),
    "send": re.compile(r"发(送|给|一封|封|邮件|消息|信)|回复|回信|转发|\b(send|reply|forward|e-?mail)\b", re.I),
    "calendar": re.compile(r"日历|日程|会议|提醒我|\b(calendar|schedule|meeting|event)\b", re.I),
    "call": re.compile(r"打(个)?电话|致电|拨打|\b(call|phone)\b", re.I),
}


def intents(goal: str) -> set[str]:
    return {k for k, rx in _INTENT.items() if rx.search(str(goal or ""))}


def _claims(final: str) -> set[str]:
    out = set()
    for line in re.split(r"[\n。！!？?]", str(final or "")):
        for kind, rx in _CLAIM.items():
            m = rx.search(line)
            if m and not _NEGATED.search(line[:m.start()][-12:]):
                out.add(kind)
    return out


def refs_in(text: str) -> list[str]:
    """Order / booking / confirmation numbers written next to their label."""
    out = []
    for m in _REF.finditer(str(text or "")):
        ref = m.group(1).strip("-")
        if re.search(r"\d", ref) and ref.upper() not in {r.upper() for r in out}:
            out.append(ref)
    return out


def _approved(events: list[dict], tool: str) -> bool:
    """A tool call that waited for the user's approval and got it (purchase_confirm, a booking click)."""
    waiting = {}
    for e in events:
        d = e.get("data") or {}
        if e.get("type") == "waiting" and d.get("type") == "approval":
            waiting[d.get("approval_id")] = (d.get("summary") or {}).get("tool") or d.get("tool")
        if e.get("type") == "approval_resolved" and d.get("decision") == "approved" and waiting.get(d.get("approval_id")) == tool:
            return True
    return False


def actions_done(events: list[dict]) -> list[dict]:
    """Consequential tool calls that ran successfully, in order: {kind, tool, idx, preview}."""
    calls = {}
    out = []
    purchase_at = None
    for i, e in enumerate(events):
        d = e.get("data") or {}
        if e.get("type") == "tool_call":
            calls[d.get("call_id")] = d
            continue
        # a call run after the user's approval reports status "ok" instead of ok=True
        if e.get("type") != "tool_result" or not (d.get("ok") or d.get("status") == "ok") or d.get("skipped"):
            continue
        name = d.get("name") or ""
        prev = str(d.get("preview") or "")
        if name in SEND_TOOLS:
            out.append({"kind": "send", "tool": name, "idx": i, "preview": prev})
        elif name in CALENDAR_TOOLS:
            out.append({"kind": "calendar", "tool": name, "idx": i, "preview": prev})
        elif name in NOTION_TOOLS:
            out.append({"kind": "notion", "tool": name, "idx": i, "preview": prev})
        elif name in CALL_TOOLS:
            out.append({"kind": "call", "tool": name, "idx": i, "preview": prev})
        elif name == "purchase_confirm" and "approved" in prev:
            purchase_at = i
        elif name in ("browser_click", "browser_click_at") and purchase_at is not None:
            # a click after the user confirmed the purchase: the place-order / pay click (recorded once)
            if not any(a["kind"] == "purchase" for a in out):
                out.append({"kind": "purchase", "tool": name, "idx": i, "preview": prev})
    return out


def assess(events: list[dict], final: str, tool_texts: list[str] | None = None, goal: str | None = None) -> dict:
    """{status, actions: [...], claims: [...], evidence: [...], missing: [...]}. tool_texts = the full tool results of
    the task (newest last); without them the event previews are used."""
    acts = actions_done(events)
    claims = _claims(final)
    if goal is not None:   # only what the request asked for, or what the task actually did
        claims &= intents(goal) | {a["kind"] for a in acts}
    texts = list(tool_texts or [])
    if not texts:
        texts = [str((e.get("data") or {}).get("preview") or "") for e in events if e.get("type") == "tool_result"]
    evidence, missing = [], []
    for a in acts:
        if a["kind"] == "purchase":
            # proof = an order number the answer gives that a page after the order click actually shows
            seen = "\n".join(texts).upper()
            nums = [r for r in refs_in(final) if r.upper() in seen]
            if nums:
                evidence.append({"kind": "purchase", "order_number": nums[0]})
            else:
                missing.append("purchase")
        elif a["kind"] == "send":
            mid = re.search(r"(?:message_?id|\"id\")\W{0,4}([A-Za-z0-9_\-]{8,})", a["preview"])
            evidence.append({"kind": "send", "tool": a["tool"], "id": mid.group(1) if mid else ""})
        elif a["kind"] == "calendar":
            eid = re.search(r"(?:event_?id|\"id\")\W{0,4}([A-Za-z0-9_\-@.]{6,})", a["preview"])
            evidence.append({"kind": "calendar", "tool": a["tool"], "id": eid.group(1) if eid else ""})
        elif a["kind"] == "call":
            cid = re.search(r"(?:call_?id)\W{0,4}([A-Za-z0-9_\-]{6,})", a["preview"])
            evidence.append({"kind": "call", "id": cid.group(1) if cid else ""})
        else:
            evidence.append({"kind": a["kind"], "tool": a["tool"]})
    done_kinds = {a["kind"] for a in acts}
    # a booking is done through an approved click on the booking site: accept it when the answer carries a reference
    # number that a page showed
    if "booking" in claims and "booking" not in done_kinds:
        seen = "\n".join(texts).upper()
        nums = [r for r in refs_in(final) if r.upper() in seen]
        if nums and _approved(events, "browser_click"):
            evidence.append({"kind": "booking", "reference": nums[0]})
            done_kinds.add("booking")
    claimed_not_done = sorted(k for k in claims if k not in done_kinds and not (k == "booking" and "purchase" in done_kinds))
    missing += [f"{k}:not_done" for k in claimed_not_done]
    if missing:
        status = "unverified"
    elif acts or "booking" in done_kinds:
        status = "verified"
    else:
        status = "none"
    return {"status": status, "actions": [{"kind": a["kind"], "tool": a["tool"]} for a in acts], "claims": sorted(claims),
            "evidence": evidence, "missing": missing}


def nudge(result: dict, zh: bool = True) -> str:
    """What to tell the model before it finishes, when the proof is missing ('' = nothing to fix)."""
    if result["status"] != "unverified":
        return ""
    parts = []
    if "purchase" in result["missing"]:
        parts.append(("下单之后没有拿到订单号：用 browser_snapshot 读一下现在的页面（确认页），把页面上的订单号原样写进回答；"
                      "如果页面没有显示订单号或下单没有成功，就如实说「未能确认订单是否提交」，不要说下单成功。")
                     if zh else
                     ("After the order click there is no order number yet: browser_snapshot the page you are on (the "
                      "confirmation page) and copy its order number into the answer as shown; if the page shows none or the order "
                      "failed, say plainly that the order could not be confirmed — do not say it was placed."))
    nd = [m.split(":")[0] for m in result["missing"] if m.endswith(":not_done")]
    if nd:
        what = {"purchase": "下单", "booking": "预订", "send": "发送", "calendar": "修改日历", "call": "打电话"}
        what_en = {"purchase": "placed an order", "booking": "made a booking", "send": "sent a message",
                   "calendar": "changed the calendar", "call": "made a call"}
        parts.append(("你的回答说已经" + "、".join(what.get(k, k) for k in nd) + "，但这个任务里没有成功执行过对应的操作。"
                      "改写回答：只写实际完成了什么、停在了哪一步。")
                     if zh else
                     ("Your answer says you " + ", ".join(what_en.get(k, k) for k in nd) + ", but no such action ran successfully "
                      "in this task. Rewrite the answer: say only what was actually done and where it stopped."))
    return ("（系统）" if zh else "(System) ") + " ".join(parts)


def notice(result: dict, zh: bool = True) -> str:
    """A short line added under the answer when the proof is still missing after the nudge."""
    if result["status"] != "unverified":
        return ""
    if zh:
        return "\n\n> ⚠️ 结果未经确认：没有找到能证明这一步已完成的订单号或确认信息，请到对应网站或邮箱核对。"
    return "\n\n> ⚠️ Not confirmed: no order number or confirmation was found that proves this step was completed — please check on the site or in your email."
