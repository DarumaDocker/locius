"""Policy engine: ALLOW / DENY / ASK_USER for every proposed action."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.sentinel import guard
from app.sentinel.catalog import TOOLS

ALLOW, DENY, ASK = "ALLOW", "DENY", "ASK_USER"
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass
class Decision:
    decision: str
    risk: str
    reason: str = ""
    destination: str = ""
    grant_id: str = ""
    notes: list[str] = field(default_factory=list)


def _bump(risk: str, to: str) -> str:
    return to if RISK_ORDER[to] > RISK_ORDER[risk] else risk


def destination(tool: str, args: dict, elem: dict | None = None, page: dict | None = None) -> str:
    if tool in ("gmail_send", "gmail_create_draft", "gmail_forward"):
        rec = ",".join(sorted(x.strip().lower() for x in f"{args.get('to', '')},{args.get('cc', '')}".split(",") if x.strip()))
        return rec
    if tool == "gmail_reply":
        return f"reply:{args.get('message_id', '')}"
    if tool.startswith("browser_"):
        url = args.get("url") or (page or {}).get("url", "")
        return guard.domain_of(url)
    if tool == "slack_send_message":
        return f"slack:{str(args.get('channel', '')).lstrip('#').lower()}"
    if tool.startswith("notion_") and tool not in ("notion_search", "notion_get_page", "notion_query_database"):
        return f"notion:{args.get('parent_id') or args.get('page_id') or ''}"
    return ""


def decide(store, tool: str, args: dict, task_id: str, *, elem: dict | None = None, page: dict | None = None,
           gmail_ready: bool = True) -> Decision:
    t = TOOLS.get(tool)
    if not t:
        return Decision(DENY, "high", f"未知工具 unknown tool: {tool}")
    if str(t["connector"]).startswith("mcp:"):
        return _decide_mcp(store, tool, t, args, task_id)
    conn = store.connection(t["connector"])
    if not conn["enabled"]:
        return Decision(DENY, t["risk"], f"连接「{t['connector']}」未启用 (connector disabled)")
    if not conn["permissions"].get(t["capability"], False):
        return Decision(DENY, t["risk"], f"权限「{t['connector']}.{t['capability']}」已关闭 (permission off in Connections)")
    if t["connector"] == "gmail" and not gmail_ready:
        return Decision(DENY, t["risk"], "Gmail 尚未配置：请在「连接 Connections」页填写邮箱和应用专用密码 (App Password)")

    risk = t["risk"]
    reasons: list[str] = []
    ctx = store.task_ctx(task_id)
    tainted = guard.LEVELS.get(ctx["taint"], 0) >= guard.LEVELS["CONFIDENTIAL"]
    dest = destination(tool, args, elem, page)

    # ---------------------------------------------------------------- browser rules
    if tool == "browser_navigate":
        url = str(args.get("url", ""))
        ok, why = guard.check_url(url)
        if not ok:
            return Decision(DENY, "high", why)
        dom = guard.domain_of(url)
        cfg = conn["config"]
        if dom in set(cfg.get("blocked_domains") or []):
            return Decision(DENY, "high", f"域名 {dom} 在黑名单中 (blocked domain)")
        p = urlparse(url)
        carries_data = bool(p.query) or len(p.path or "") > 60
        trusted = dom in set(cfg.get("allowed_domains") or [])
        if tainted and dom not in ctx["domains"] and not trusted and carries_data:
            risk = _bump(risk, "high")
            reasons.append("本任务已读取机密数据（邮件等），且此网址带有参数，可能造成数据外泄 (data egress check)")
        elif ctx["injection"] and dom not in ctx["domains"] and not trusted and carries_data:
            risk = _bump(risk, "high")
            reasons.append("本任务读到疑似提示注入的内容后，要访问一个带参数的新网址 (possible exfiltration after injection)")
    if tool in ("browser_click", "browser_click_at", "browser_type", "browser_select", "browser_press", "browser_upload") and page:
        dom = guard.domain_of(page.get("url", ""))
        if dom and dom in set(conn["config"].get("blocked_domains") or []):
            return Decision(DENY, "high", f"域名 {dom} 在黑名单中 (blocked domain)")
    if tool == "browser_click_at" and not (elem and elem.get("tag")):
        risk = _bump(risk, "high")
        reasons.append("无法确认这个位置上是什么元素 (can't tell what is at this position)")
    if tool in ("browser_click", "browser_click_at") and elem:
        # a <button> reports type "submit" by default; outside a <form> it submits nothing (chat launchers, menus)
        itype = elem.get("input_type", "") if elem.get("in_form", True) else ""
        if guard.click_is_risky(elem.get("role", ""), elem.get("name", ""), itype):
            risk = _bump(risk, "high")
            reasons.append(f"点击的按钮「{elem.get('name', '')}」可能提交/购买/发送/删除 (consequential click)")
    if tool == "browser_type" or (tool == "browser_click_at" and args.get("text")):
        if elem and (elem.get("input_type", "").lower() == "password" or elem.get("is_password")):
            return Decision(DENY, "high", "Agent 不能输入密码。请调用 browser_request_takeover 让用户接管输入 (use takeover for passwords)")
        text = str(args.get("text", ""))
        is_search = elem and guard.looks_like_search(elem.get("role", ""), elem.get("name", ""))
        if args.get("submit") and not is_search:
            risk = _bump(risk, "high")
            reasons.append("输入后会按回车提交表单 (submits a form)")
        elif tainted and len(text) > 40 and not is_search:
            risk = _bump(risk, "high")
            reasons.append("本任务读取过机密数据，正在把较长文本输入网页 (possible data egress)")
        elif not is_search:
            risk = _bump(risk, "medium")
    if tool == "browser_press" and str(args.get("key", "")).lower() in ("enter", "return"):
        if elem and not guard.looks_like_search(elem.get("role", ""), elem.get("name", "")) and elem.get("in_form"):
            risk = _bump(risk, "high")
            reasons.append("回车会提交表单 (Enter submits a form)")
    if tool == "browser_upload":
        reasons.append("上传本地文件到网站 (uploads a local file)")

    # ---------------------------------------------------------------- gmail rules
    if tool in ("gmail_send", "gmail_reply", "gmail_forward"):
        reasons.append("对外发送邮件 (sends email on your behalf)")
    if tool == "gmail_unsubscribe":
        reasons.append(f"将代表你退订 {len(args.get('message_ids') or [])} 个发件方（发送退订请求/退订邮件）(unsubscribes on your behalf)")

    # ---------------------------------------------------------------- slack / notion rules
    if t["connector"] in ("notion", "slack") and not store.has_secret(f"cred_{t['connector']}_1"):
        return Decision(DENY, t["risk"], f"{t['connector'].title()} 尚未连接：请在「连接 Connections」页设置 (not connected)")
    if tool == "slack_send_message":
        reasons.append("代表你在 Slack 发送消息 (posts to Slack on your behalf)")
    if tool == "notion_update_page" and args.get("archived"):
        risk = _bump(risk, "high")
        reasons.append("将归档（删除）一个 Notion 页面 (archives a Notion page)")

    # ---------------------------------------------------------------- calendar rules
    if t["connector"] == "calendar":
        if not store.has_secret("cred_calendar_1"):
            return Decision(DENY, t["risk"], "Google 日历尚未连接：请在「连接 Connections」页设置 (not connected)")
        if tool == "calendar_create_event" and args.get("attendees"):
            risk = _bump(risk, "high")
            reasons.append("会给参会人发送日历邀请邮件 (sends calendar invitations)")
        elif tool == "calendar_create_event" and tainted and len(str(args.get("description") or "")) > 300:
            risk = _bump(risk, "high")
            reasons.append("本任务读取过机密数据，这次要把较长的内容写进日程 (possible data egress)")
        if tool == "calendar_update_event":
            reasons.append("修改已有日程，参会人会收到更新 (changes an existing event)")
        if tool == "calendar_delete_event":
            reasons.append("删除日程，参会人会收到取消通知 (deletes an event)")

    # ---------------------------------------------------------------- prompt-injection escalation
    if ctx["injection"] and RISK_ORDER[risk] >= RISK_ORDER["medium"]:
        risk = _bump(risk, "high")
        reasons.append("本任务读到的外部内容疑似包含提示注入，所有写操作需要人工确认 "
                       f"(prompt-injection suspected: {', '.join(ctx['injection'])})")

    return _finish(store, tool, task_id, risk, reasons, dest, ctx)


def _finish(store, tool: str, task_id: str, risk: str, reasons: list[str], dest: str, ctx: dict) -> Decision:
    reason = "；".join(reasons)
    if RISK_ORDER[risk] < RISK_ORDER["high"]:
        return Decision(ALLOW, risk, reason, dest)

    # ---------------------------------------------------------------- grants for high-risk actions
    if not ctx["injection"]:
        for g in store.active_grants():
            if g["tool"] != tool:
                continue
            if g["scope"] == "TASK" and g["task_id"] != task_id:
                continue
            want = (g["match"] or {}).get("destination")
            if want and want != dest:
                continue
            return Decision(ALLOW, risk, f"已有授权 (granted: {g['scope']})", dest, grant_id=g["id"])
    return Decision(ASK, risk, reason or "高风险操作需要你的批准 (high-risk action)", dest)


def _decide_mcp(store, tool: str, t: dict, args: dict, task_id: str) -> Decision:
    """MCP tools: the per-tool mode the user set (auto / ask / off) plus taint & injection escalation."""
    from app.sentinel import mcp_hub
    s, rec = mcp_hub.tool_info(store, tool)
    if not s or not rec:
        return Decision(DENY, "high", "这个 MCP 工具已被移除 (tool removed)")
    if not s.get("enabled", True):
        return Decision(DENY, t["risk"], f"MCP 服务器「{s['name']}」已停用 (server disabled in Connections)")
    if rec.get("mode") == "off":
        return Decision(DENY, t["risk"], f"MCP 工具「{rec['name']}」已关闭 (tool off in Connections)")
    if rec.get("status") != "ok":
        return Decision(DENY, "high", f"MCP 工具「{rec['name']}」的定义有变化，需要你在「连接」页检查后才能使用 "
                                      "(tool definition changed — review it in Connections)")
    read = rec.get("kind") == "read"
    risk = "low" if read else "medium"
    reasons: list[str] = []
    ctx = store.task_ctx(task_id)
    tainted = guard.LEVELS.get(ctx["taint"], 0) >= guard.LEVELS["CONFIDENTIAL"]
    size = len(str(args or ""))
    if rec.get("mode") == "ask":
        risk = "high"
        reasons.append(f"你设置了「{s['name']} · {rec['name']}」每次都需要审批 (set to ask every time)")
    if not read:
        reasons.append(f"会修改「{s['name']}」里的数据 (writes to {s['name']})")
        if tainted and guard.LEVELS.get(ctx["taint"], 0) > guard.LEVELS.get(s.get("data_class") or "CONFIDENTIAL", 2):
            risk = _bump(risk, "high")
            reasons.append("本任务读取过更机密的数据，现在要写入一个外部服务 (data egress check)")
        elif tainted and size > 300:
            risk = _bump(risk, "high")
            reasons.append("本任务读取过机密数据（邮件等），且这次要写入较多内容 (possible data egress)")
    elif tainted and size > 500 and guard.LEVELS.get(ctx["taint"], 0) > guard.LEVELS.get(s.get("data_class") or "CONFIDENTIAL", 2):
        risk = _bump(risk, "high")
        reasons.append("本任务读取过机密数据，这次查询参数很长，可能把数据带出去 (possible data egress in query)")
    if ctx["injection"] and not read:
        risk = _bump(risk, "high")
        reasons.append("本任务读到的外部内容疑似包含提示注入，所有写操作需要人工确认 "
                       f"(prompt-injection suspected: {', '.join(ctx['injection'])})")
    return _finish(store, tool, task_id, risk, reasons, s["id"], ctx)
