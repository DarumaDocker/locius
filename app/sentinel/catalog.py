"""Connector manifests: every external tool the agent may propose.

Sentinel is the single source of truth for which tools exist, what capability
(permission) they need and their base risk. The runtime fetches this catalog and
exposes the schemas to the LLM.
"""
from __future__ import annotations

S = "string"


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


TOOLS: dict[str, dict] = {
    # ------------------------------------------------------------------ Gmail
    "gmail_search": {
        "connector": "gmail", "capability": "read", "operation": "search", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "搜索 Gmail 邮件。Search Gmail using Gmail query syntax, e.g. 'in:inbox newer_than:7d', "
                       "'from:john is:unread', 'subject:invoice after:2026/09/01', 'category:promotions newer_than:1d'. Returns id, thread_id, from, "
                       "subject, date, snippet, account (which mailbox), and `unsubscribe` (one-click/email/link) when the email can be "
                       "unsubscribed via gmail_unsubscribe. Searches ALL connected mailboxes unless `account` is given. "
                       "Ids may carry a mailbox prefix like 'g2:123…' — always pass ids back exactly as returned.",
        "parameters": _obj({"query": {"type": S, "description": "Gmail search query"},
                            "max_results": {"type": "integer", "description": "1-30 per mailbox, default 10"},
                            "account": {"type": S, "description": "optional: email address of one mailbox; default all"}}, ["query"]),
    },
    "gmail_get_message": {
        "connector": "gmail", "capability": "read", "operation": "read", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "读取一封邮件全文。Read one email (body, attachments list) by message id from gmail_search.",
        "parameters": _obj({"message_id": {"type": S}}, ["message_id"]),
    },
    "gmail_get_thread": {
        "connector": "gmail", "capability": "read", "operation": "read", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "读取整个邮件会话。Read a whole email thread by thread_id.",
        "parameters": _obj({"thread_id": {"type": S}}, ["thread_id"]),
    },
    "gmail_list_labels": {
        "connector": "gmail", "capability": "read", "operation": "read", "risk": "low", "data_class": "PERSONAL",
        "description": "列出 Gmail 标签。List Gmail labels/folders of a mailbox.",
        "parameters": _obj({"account": {"type": S, "description": "optional mailbox email; default mailbox"}}),
    },
    "gmail_create_draft": {
        "connector": "gmail", "capability": "draft", "operation": "create", "risk": "medium", "data_class": "CONFIDENTIAL",
        "description": "在 Gmail 草稿箱创建草稿（不会发送）。Create a draft (NOT sent). For a reply pass reply_to_message_id; "
                       "'to' and 'subject' may be left empty for replies and will be filled from the original email.",
        "parameters": _obj({"to": {"type": S}, "subject": {"type": S}, "body": {"type": S}, "cc": {"type": S},
                            "reply_to_message_id": {"type": S},
                            "from_account": {"type": S, "description": "optional: which of the user's mailboxes (email) to use; default = the default mailbox; replies always use the mailbox of the original email"}}, ["body"]),
    },
    "gmail_send": {
        "connector": "gmail", "capability": "send", "operation": "send", "risk": "high", "data_class": "CONFIDENTIAL",
        "description": "发送邮件（需要用户在审批界面批准）。Send an email. Always requires the user's approval in the approval UI; "
                       "do not ask for permission in chat, just call this tool. For replies pass reply_to_message_id.",
        "parameters": _obj({"to": {"type": S}, "subject": {"type": S}, "body": {"type": S}, "cc": {"type": S},
                            "reply_to_message_id": {"type": S},
                            "from_account": {"type": S, "description": "optional: send from this mailbox (email); default = the default mailbox"}}, ["body"]),
    },
    "gmail_reply": {
        "connector": "gmail", "capability": "send", "operation": "send", "risk": "high", "data_class": "CONFIDENTIAL",
        "description": "回复一封邮件并发送（需要审批）。Reply to an email and send it (requires approval). reply_all includes other recipients.",
        "parameters": _obj({"message_id": {"type": S}, "body": {"type": S}, "reply_all": {"type": "boolean"}},
                           ["message_id", "body"]),
    },
    "gmail_forward": {
        "connector": "gmail", "capability": "send", "operation": "send", "risk": "high", "data_class": "CONFIDENTIAL",
        "description": "转发邮件（需要审批）。Forward an email with an optional note (requires approval).",
        "parameters": _obj({"message_id": {"type": S}, "to": {"type": S}, "note": {"type": S}}, ["message_id", "to"]),
    },
    "gmail_unsubscribe": {
        "connector": "gmail", "capability": "send", "operation": "send", "risk": "high", "data_class": "PERSONAL",
        "description": "退订邮件列表/营销邮件（需要用户审批）。Unsubscribe from newsletters/marketing senders using each email's "
                       "List-Unsubscribe header (one-click POST, unsubscribe email, or unsubscribe link). Pass ALL candidate "
                       "message_ids in ONE call — the approval dialog lists every email and the user can untick ones to keep, "
                       "so do NOT ask for confirmation in chat. Only ids whose gmail_search result has an `unsubscribe` field work. "
                       "Set archive=true to also move those emails out of the inbox. Returns a per-email result.",
        "parameters": _obj({"message_ids": {"type": "array", "items": {"type": S}, "description": "up to 30 ids"},
                            "archive": {"type": "boolean", "description": "also archive these emails"}}, ["message_ids"]),
    },
    "gmail_archive": {
        "connector": "gmail", "capability": "organize", "operation": "update", "risk": "medium", "data_class": "PERSONAL",
        "description": "归档邮件（移出收件箱）。Archive emails (remove from inbox).",
        "parameters": _obj({"message_ids": {"type": "array", "items": {"type": S}}}, ["message_ids"]),
    },
    "gmail_label": {
        "connector": "gmail", "capability": "organize", "operation": "update", "risk": "medium", "data_class": "PERSONAL",
        "description": "给邮件加/去标签，或标记已读。Add/remove labels; use mark_read true/false to change read state.",
        "parameters": _obj({"message_ids": {"type": "array", "items": {"type": S}},
                            "add_labels": {"type": "array", "items": {"type": S}},
                            "remove_labels": {"type": "array", "items": {"type": S}},
                            "mark_read": {"type": "boolean"}}, ["message_ids"]),
    },
    # ------------------------------------------------------------------ Browser
    "browser_navigate": {
        "connector": "browser", "capability": "browse", "operation": "navigate", "risk": "low", "data_class": "PUBLIC",
        "description": "在浏览器中打开网址，返回页面快照。Open a URL in the agent's browser; returns an accessibility snapshot "
                       "where interactive elements have refs like [e12].",
        "parameters": _obj({"url": {"type": S}}, ["url"]),
    },
    "browser_snapshot": {
        "connector": "browser", "capability": "browse", "operation": "read", "risk": "low", "data_class": "PUBLIC",
        "description": "获取当前页面快照（可交互元素和文字）。Get the current page snapshot (interactive elements with refs + text).",
        "parameters": _obj({"max_chars": {"type": "integer", "description": "default 12000"}}),
    },
    "browser_click": {
        "connector": "browser", "capability": "interact", "operation": "click", "risk": "low", "data_class": "PUBLIC",
        "description": "点击元素（用快照里的 ref）。Click an element by ref from the latest snapshot. Clicking submit/pay/send/delete-like "
                       "buttons automatically triggers a user approval.",
        "parameters": _obj({"ref": {"type": S}}, ["ref"]),
    },
    "browser_type": {
        "connector": "browser", "capability": "interact", "operation": "type", "risk": "low", "data_class": "PUBLIC",
        "description": "在输入框中输入文字。Type text into an input by ref. Set submit=true to press Enter afterwards. "
                       "Never type passwords; call browser_request_takeover for logins instead.",
        "parameters": _obj({"ref": {"type": S}, "text": {"type": S}, "submit": {"type": "boolean"}}, ["ref", "text"]),
    },
    "browser_select": {
        "connector": "browser", "capability": "interact", "operation": "select", "risk": "low", "data_class": "PUBLIC",
        "description": "选择下拉框选项。Select an option (by label or value) in a <select> element by ref.",
        "parameters": _obj({"ref": {"type": S}, "value": {"type": S}}, ["ref", "value"]),
    },
    "browser_press": {
        "connector": "browser", "capability": "interact", "operation": "press", "risk": "low", "data_class": "PUBLIC",
        "description": "按键，如 Enter / Escape / Tab / ArrowDown。Press a keyboard key on the page.",
        "parameters": _obj({"key": {"type": S}}, ["key"]),
    },
    "browser_find": {
        "connector": "browser", "capability": "browse", "operation": "read", "risk": "low", "data_class": "PUBLIC",
        "description": "在整个页面里按文字查找元素（商品标题、按钮如 Add to Cart 等），返回 ref 和周围信息（如价格）。"
                       "Find elements anywhere on the page by their text — product titles, buttons like 'Add to Cart', links — even when "
                       "the snapshot was truncated. Returns refs you can click plus nearby context such as the price.",
        "parameters": _obj({"query": {"type": S, "description": "words to look for, e.g. 'iPhone 17 Pro Max case' or 'Add to Cart'"}}, ["query"]),
    },
    "browser_look": {
        "connector": "browser", "capability": "browse", "operation": "read", "risk": "low", "data_class": "PUBLIC",
        "description": "用视觉看当前页面：截图并给每个可点元素标上 [eN]，由视觉模型回答你的问题。"
                       "LOOK at the page with vision: takes a screenshot of the visible part, labels every clickable element with its "
                       "ref [eN], and a vision model answers your question (what products/prices/buttons are shown, which one "
                       "matches, where to click). Use it when the text snapshot is unclear, for images/visual choices "
                       "(e.g. 'which case looks nicest'), or to confirm what happened. Then click the ref it names.",
        "parameters": _obj({"question": {"type": S, "description": "what you want to know about the visible page"}}, ["question"]),
    },
    "browser_locate": {
        "connector": "browser", "capability": "browse", "operation": "read", "risk": "low", "data_class": "PUBLIC",
        "description": "用视觉在截图上定位一个东西（例如右下角的客服聊天气泡），返回它的屏幕坐标。"
                       "LOCATE something on the screen with vision and get its x/y position — for things that have no ref: "
                       "icons, chat bubbles and chat windows inside iframes or widgets, canvas maps, custom buttons. Describe it "
                       "the way a person would ('the blue round chat bubble in the bottom-right corner', 'the message box of the "
                       "chat window'). Then use browser_click_at with the returned x/y.",
        "parameters": _obj({"target": {"type": S, "description": "what to find, described visually (colour, shape, position, text on it)"}},
                           ["target"]),
    },
    "browser_click_at": {
        "connector": "browser", "capability": "interact", "operation": "click", "risk": "low", "data_class": "PUBLIC",
        "description": "按屏幕坐标点击（坐标来自 browser_locate），可选：点击后输入文字并回车发送。"
                       "Click at x/y screen coordinates from browser_locate (works inside iframes and chat widgets). Optionally "
                       "type text into what was clicked and press Enter (submit=true) — e.g. to send a chat message. Sending, "
                       "buying and other consequential clicks trigger the user's approval.",
        "parameters": _obj({"x": {"type": "number"}, "y": {"type": "number"},
                            "text": {"type": S, "description": "optional text to type after clicking"},
                            "submit": {"type": "boolean", "description": "press Enter after typing"}}, ["x", "y"]),
    },
    "browser_scroll": {
        "connector": "browser", "capability": "browse", "operation": "scroll", "risk": "low", "data_class": "PUBLIC",
        "description": "滚动页面，返回新位置附近的内容。Scroll the page; the returned snapshot shows the part of the page around the new position.",
        "parameters": _obj({"direction": {"type": S, "enum": ["up", "down", "top", "bottom"]}}, ["direction"]),
    },
    "browser_back": {
        "connector": "browser", "capability": "browse", "operation": "navigate", "risk": "low", "data_class": "PUBLIC",
        "description": "浏览器后退。Go back to the previous page.",
        "parameters": _obj({}),
    },
    "browser_wait": {
        "connector": "browser", "capability": "browse", "operation": "wait", "risk": "low", "data_class": "PUBLIC",
        "description": "等待若干秒（例如等待客服回复），然后返回新快照。Wait N seconds (1-120) e.g. for a support agent to reply, then snapshot.",
        "parameters": _obj({"seconds": {"type": "integer"}}, ["seconds"]),
    },
    "browser_upload": {
        "connector": "browser", "capability": "upload", "operation": "upload", "risk": "high", "data_class": "PUBLIC",
        "description": "上传工作区文件到网页文件框（需要审批）。Upload a workspace file into a file input by ref (requires approval).",
        "parameters": _obj({"ref": {"type": S}, "path": {"type": S, "description": "path inside the workspace"}}, ["ref", "path"]),
    },
    "browser_downloads": {
        "connector": "browser", "capability": "download", "operation": "read", "risk": "low", "data_class": "PUBLIC",
        "description": "列出浏览器下载的文件（已隔离扫描）。List files downloaded by the browser (quarantined + scanned, saved under downloads/).",
        "parameters": _obj({}),
    },
    "browser_request_takeover": {
        "connector": "browser", "capability": "browse", "operation": "takeover", "risk": "low", "data_class": "PUBLIC",
        "description": "请用户接管浏览器（登录、验证码、支付、需要人工判断时）。Ask the user to take over the browser (login, CAPTCHA, "
                       "payment, anything needing a human). The task pauses until the user hands control back.",
        "parameters": _obj({"reason": {"type": S}}, ["reason"]),
    },
    # ------------------------------------------------------------------ Notion
    "notion_search": {
        "connector": "notion", "capability": "read", "operation": "search", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "搜索 Notion 页面和数据库（只能看到已共享给 Locius 集成的内容）。Search Notion pages/databases shared with the "
                       "Locius integration. Returns id, title, url, last_edited_time. kind: page | database (optional).",
        "parameters": _obj({"query": {"type": S}, "kind": {"type": S, "enum": ["page", "database"]},
                            "max_results": {"type": "integer", "description": "1-50, default 10"}}),
    },
    "notion_get_page": {
        "connector": "notion", "capability": "read", "operation": "read", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "读取 Notion 页面全文（Markdown）。Read a Notion page (properties + content as Markdown) by id or URL.",
        "parameters": _obj({"page_id": {"type": S, "description": "page id or notion.so URL"}}, ["page_id"]),
    },
    "notion_query_database": {
        "connector": "notion", "capability": "read", "operation": "read", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "查询 Notion 数据库的行。Query rows of a Notion database. Optional `filter`/`sorts` use the Notion API JSON format, "
                       "e.g. {\"property\": \"Status\", \"status\": {\"equals\": \"Doing\"}}. Returns the column schema too.",
        "parameters": _obj({"database_id": {"type": S, "description": "database id or URL"}, "filter": {"type": "object"},
                            "sorts": {"type": "array", "items": {"type": "object"}},
                            "max_results": {"type": "integer", "description": "1-100, default 20"}}, ["database_id"]),
    },
    "notion_create_page": {
        "connector": "notion", "capability": "write", "operation": "create", "risk": "medium", "data_class": "CONFIDENTIAL",
        "description": "在 Notion 新建页面或数据库行。Create a page under a parent page, or a row in a database (parent = database id). "
                       "content is Markdown (headings, lists, to-dos, code). For database rows, `properties` maps column name -> "
                       "simple value (text, number, true/false, 'YYYY-MM-DD', select option name, list for multi-select).",
        "parameters": _obj({"parent_id": {"type": S, "description": "parent page id/URL or database id/URL"},
                            "title": {"type": S}, "content": {"type": S, "description": "Markdown body"},
                            "properties": {"type": "object"}}, ["parent_id", "title"]),
    },
    "notion_append": {
        "connector": "notion", "capability": "write", "operation": "update", "risk": "medium", "data_class": "CONFIDENTIAL",
        "description": "在 Notion 页面末尾追加内容。Append Markdown content to the end of a page.",
        "parameters": _obj({"page_id": {"type": S}, "content": {"type": S}}, ["page_id", "content"]),
    },
    "notion_update_page": {
        "connector": "notion", "capability": "write", "operation": "update", "risk": "medium", "data_class": "CONFIDENTIAL",
        "description": "修改 Notion 页面标题/数据库行的属性，或归档（archived=true，需要审批）。Update a page's title or a database row's "
                       "properties (same simple values as notion_create_page), or archive it (archived=true, requires approval).",
        "parameters": _obj({"page_id": {"type": S}, "title": {"type": S}, "properties": {"type": "object"},
                            "archived": {"type": "boolean"}}, ["page_id"]),
    },
    # ------------------------------------------------------------------ Slack
    "slack_list_channels": {
        "connector": "slack", "capability": "read", "operation": "read", "risk": "low", "data_class": "PERSONAL",
        "description": "列出 Slack 频道和私信。List Slack channels/DMs visible to the connected token (is_member shows where it can read).",
        "parameters": _obj({}),
    },
    "slack_read_channel": {
        "connector": "slack", "capability": "read", "operation": "read", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "读取 Slack 频道最近的消息。Read recent messages of a channel (name like #general or id). oldest = Unix ts to "
                       "start from. Messages with reply_count > 0 have threads: read them with slack_read_thread.",
        "parameters": _obj({"channel": {"type": S}, "limit": {"type": "integer", "description": "1-100, default 20"},
                            "oldest": {"type": S}}, ["channel"]),
    },
    "slack_read_thread": {
        "connector": "slack", "capability": "read", "operation": "read", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "读取 Slack 讨论串。Read a thread (channel + parent message ts).",
        "parameters": _obj({"channel": {"type": S}, "ts": {"type": S}}, ["channel", "ts"]),
    },
    "slack_search": {
        "connector": "slack", "capability": "read", "operation": "search", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "搜索 Slack 消息（需要用户令牌 xoxp-）。Search Slack messages with Slack search syntax, e.g. 'from:@john in:#sales "
                       "after:2026-09-01'. Only works with a user token.",
        "parameters": _obj({"query": {"type": S}, "limit": {"type": "integer"}}, ["query"]),
    },
    "slack_send_message": {
        "connector": "slack", "capability": "send", "operation": "send", "risk": "high", "data_class": "CONFIDENTIAL",
        "description": "发送 Slack 消息（需要用户审批）。Post a message to a channel/DM, optionally as a thread reply (thread_ts). "
                       "Always requires the user's approval — do not ask in chat, just call it. Slack mrkdwn: *bold*, _italic_, `code`.",
        "parameters": _obj({"channel": {"type": S}, "text": {"type": S}, "thread_ts": {"type": S}}, ["channel", "text"]),
    },
    # ------------------------------------------------------------------ Google Calendar
    "calendar_list_events": {
        "connector": "calendar", "capability": "read", "operation": "read", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "查看 Google 日历里的日程。List events on the user's Google Calendar between time_min and time_max "
                       "(default: now → 7 days). Times like '2026-10-03 09:00' are in the calendar's time zone. Optional query "
                       "filters by text (title, location, attendees). Returns id, title, start, end, location, attendees, meeting link.",
        "parameters": _obj({"time_min": {"type": S}, "time_max": {"type": S}, "query": {"type": S},
                            "calendar_id": {"type": S, "description": "default primary"},
                            "max_results": {"type": "integer", "description": "1-100, default 25"}}),
    },
    "calendar_free_slots": {
        "connector": "calendar", "capability": "read", "operation": "read", "risk": "low", "data_class": "CONFIDENTIAL",
        "description": "找空闲时间。Find free time slots of at least duration_minutes between time_min and time_max within "
                       "working hours (default 09:00–18:00, calendar time zone). Use it before proposing meeting times or bookings.",
        "parameters": _obj({"time_min": {"type": S}, "time_max": {"type": S}, "duration_minutes": {"type": "integer"},
                            "day_start": {"type": S, "description": "HH:MM, default 09:00"},
                            "day_end": {"type": S, "description": "HH:MM, default 18:00"}}, ["time_min", "time_max"]),
    },
    "calendar_create_event": {
        "connector": "calendar", "capability": "write", "operation": "create", "risk": "medium", "data_class": "CONFIDENTIAL",
        "description": "在 Google 日历里新建日程（有参会人时会发邀请，需要审批）。Create an event. start/end like "
                       "'2026-10-03 19:00' (calendar time zone) or all_day=true with dates. attendees = emails to invite "
                       "(invites are sent — this needs the user's approval; just call it). reminder_minutes = popup reminder.",
        "parameters": _obj({"title": {"type": S}, "start": {"type": S}, "end": {"type": S}, "all_day": {"type": "boolean"},
                            "location": {"type": S}, "description": {"type": S},
                            "attendees": {"type": "array", "items": {"type": S}},
                            "reminder_minutes": {"type": "integer"}, "time_zone": {"type": S, "description": "IANA zone, optional"},
                            "calendar_id": {"type": S}}, ["title", "start"]),
    },
    "calendar_update_event": {
        "connector": "calendar", "capability": "write", "operation": "update", "risk": "high", "data_class": "CONFIDENTIAL",
        "description": "修改日程（需要审批）。Change an existing event's title/time/location/description/attendees by event_id "
                       "(from calendar_list_events). Giving only a new start keeps the duration. Attendees are notified.",
        "parameters": _obj({"event_id": {"type": S}, "title": {"type": S}, "start": {"type": S}, "end": {"type": S},
                            "location": {"type": S}, "description": {"type": S},
                            "attendees": {"type": "array", "items": {"type": S}}, "calendar_id": {"type": S}}, ["event_id"]),
    },
    "calendar_delete_event": {
        "connector": "calendar", "capability": "write", "operation": "delete", "risk": "high", "data_class": "CONFIDENTIAL",
        "description": "删除/取消日程（需要审批）。Delete (cancel) an event by event_id; attendees get a cancellation.",
        "parameters": _obj({"event_id": {"type": S}, "calendar_id": {"type": S}}, ["event_id"]),
    },
    # ------------------------------------------------------------------ Notify
    "notify_telegram": {
        "connector": "telegram", "capability": "notify", "operation": "send", "risk": "low", "data_class": "PERSONAL",
        "description": "通过 Telegram 给用户本人发送通知。Send a notification to the user's own Telegram chat (configured in Connections).",
        "parameters": _obj({"text": {"type": S}}, ["text"]),
    },
}


def llm_schemas(enabled_connectors: set[str] | None = None) -> list[dict]:
    out = []
    for name, t in TOOLS.items():
        if enabled_connectors is not None and t["connector"] not in enabled_connectors:
            continue
        out.append({"type": "function", "function": {"name": name, "description": t["description"],
                                                     "parameters": t["parameters"]}})
    return out
