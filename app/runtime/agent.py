"""Agent Runtime: task state machine, planner, executor loop, sub-agents, local tools.

The runtime holds no credentials and has no raw network tool: every external action is
proposed to Sentinel via /internal/act, which decides ALLOW / DENY / ASK_USER.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import mimetypes
import os
import re
import time
import traceback

import httpx

from app.common.util import dumps, now_ts, truncate
from app.runtime import prompts
from app.runtime.llm import LLM, LLMError, extract_json
from app.runtime.store import RStore

SENTINEL_URL = os.environ.get("SENTINEL_URL", "http://127.0.0.1:8080")
RUNTIME_TOKEN = os.environ.get("RUNTIME_TOKEN", "")
WORKSPACE = os.path.realpath(os.environ.get("WORKSPACE", "/workspace"))
SKILLS_DIR = os.environ.get("SKILLS_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills"))

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
WAITING = {"WAITING_APPROVAL", "WAITING_EXTERNAL", "PAUSED"}
RESULT_LIMIT = 9000
SEND_FILE_MAX = 200 * 1024 * 1024    # chat download; Telegram's own bot limit (50 MB) is checked by Sentinel
SUBAGENT_TOOLS = {"gmail_search", "gmail_get_message", "gmail_get_thread", "browser_navigate", "browser_snapshot",
                  "browser_click", "browser_type", "browser_press", "browser_scroll", "browser_back", "browser_wait",
                  "browser_select", "files_read", "files_list", "files_search", "memory_search"}
# Loop guard: small local models sometimes repeat the exact same call until the step budget is gone
# (e.g. opening one RSS feed 24 times in a row). Identical calls are refused after REPEAT_STREAK in a row,
# and read-type calls after REPEAT_TOTAL anywhere in the task. Tools where repeating is normal are exempt from the streak rule.
REPEAT_STREAK = 2
REPEAT_TOTAL = 3
REPEAT_STREAK_OK = {"browser_scroll", "browser_press", "browser_wait", "browser_click", "browser_back", "update_plan"}
REPEAT_READ = re.compile(r"navigate|search|read|list|_get|fetch|query")


def _call_sig(name: str, args) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            pass
    return name + " " + json.dumps(args or {}, ensure_ascii=False, sort_keys=True)


def repeat_guard(transcript: list[dict], call: dict) -> str | None:
    """Refusal text if `call` repeats an identical earlier call too often, else None."""
    name = call["name"]
    if name == "update_plan":
        return None
    sigs = []
    for m in transcript:
        for tc in m.get("tool_calls") or []:
            if tc.get("id") == call["id"]:
                break
            fn = tc.get("function") or {}
            sigs.append(_call_sig(fn.get("name", ""), fn.get("arguments")))
        else:
            continue
        break
    me = _call_sig(name, call.get("args") or {})
    streak = 0
    for sgn in reversed(sigs):
        if sgn != me:
            break
        streak += 1
    total = sigs.count(me)
    if (name not in REPEAT_STREAK_OK and streak >= REPEAT_STREAK) or (REPEAT_READ.search(name) and total >= REPEAT_TOTAL):
        n = total + 1
        return (f"ERROR: 重复调用已拦截 — 这是第 {n} 次用完全相同的参数调用 {name}，结果不会改变，本次没有执行。"
                "请直接使用前面已经拿到的结果继续下一步；如果这个来源读不到需要的内容，就跳过它，换别的来源，或者用已有的内容完成任务。"
                f" Repeated identical call blocked ({name}, {n}x): the result will not change. Use what you already have, "
                "skip this source, and move on to the next step.")
    return None


def _fn(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props, "required": required or []}}}


S = {"type": "string"}
LOCAL_TOOLS = [
    _fn("update_plan", "更新任务计划 Update the task plan: mark steps done/failed/running or replace the steps when re-planning.",
        {"steps": {"type": "array", "items": {"type": "object", "properties": {
            "id": S, "description": S, "status": {"type": "string", "enum": ["pending", "running", "done", "failed", "skipped"]}}}},
         "objective": S, "note": S}, ["steps"]),
    _fn("files_list", "列出工作区文件 List files in the workspace (Olares Files → Data/persona/workspace).", {"path": S}),
    _fn("files_read", "读取工作区文件（文本/Markdown/CSV/JSON/PDF）Read a workspace file.", {"path": S, "max_chars": {"type": "integer"}}, ["path"]),
    _fn("files_write", "写入工作区文件（报告、笔记、数据）Write a text file in the workspace (creates folders).",
        {"path": S, "content": S, "append": {"type": "boolean"}}, ["path", "content"]),
    _fn("files_search", "在工作区文件中搜索文字 Search text inside workspace files.", {"query": S}, ["query"]),
    _fn("send_file", "把工作区里的文件发到对话里，用户可以直接点击下载（报告、PDF、图片、表格等）；如果对话来自 Telegram，也会发到 Telegram。"
        "用户说「发给我」「让我下载」时用它，不要让用户自己去 Files 里找。"
        " Send a workspace file to the user as a downloadable attachment in this chat (and to Telegram when the chat "
        "came from Telegram). Use it whenever the user wants a file you made or downloaded.",
        {"path": S, "note": {"type": "string", "description": "一句说明 one-line caption"}}, ["path"]),
    _fn("make_pdf", "在本机把工作区文件（.md / .txt / .html）或一段 Markdown 生成 PDF（支持中文、表格、图片，A4，带页码）。"
        "所有文件都在本机处理——绝对不要把用户的文件上传到在线转换网站。生成后如果用户要文件，用 send_file 发给他。"
        " Make a PDF locally from a workspace file (.md/.txt/.html) or Markdown text. Never upload the user's documents "
        "to online converters. Then use send_file if the user wants the file.",
        {"source": {"type": "string", "description": "工作区里的源文件 workspace path of the source (.md/.txt/.html)"},
         "markdown": {"type": "string", "description": "或者直接给 Markdown 内容 or Markdown text instead of a file"},
         "output": {"type": "string", "description": "输出路径，默认与源文件同名 .pdf output path (default: next to source)"},
         "title": S}),
    _fn("make_xlsx", "在本机生成 Excel 表格（.xlsx）：表头加粗、首行冻结、可筛选、数字按数字存。可以有多个工作表。"
        "数据用 sheets 给出，或者用 source 指定工作区里的 CSV / 含 Markdown 表格的文件。生成后用 send_file 发给用户。"
        " Make an Excel workbook locally. Give sheets=[{name, columns:[...], rows:[[...], ...]}] or source (a workspace .csv, "
        "or a .md file containing a Markdown table). Then use send_file if the user wants the file.",
        {"output": {"type": "string", "description": "工作区里的输出路径 output path ending in .xlsx"},
         "sheets": {"type": "array", "items": {"type": "object", "properties": {
             "name": S, "columns": {"type": "array", "items": S},
             "rows": {"type": "array", "items": {"type": "array", "items": {}}}}}},
         "source": {"type": "string", "description": "或者：工作区里的 .csv / .md 文件 or a workspace .csv/.md file"},
         "formulas": {"type": "boolean", "description": "保留以 = 开头的公式（如 =SUM(B2:B9)）keep simple formulas; default false"}},
        ["output"]),
    _fn("memory_search", "搜索长期记忆 Search long-term memory about the user.", {"query": S}, ["query"]),
    _fn("memory_remember", "记住用户明确要求记住的事实 Save a durable fact the user explicitly asked to remember.",
        {"fact": S, "category": S, "entity": S}, ["fact"]),
    _fn("memory_forget", "删除一条记忆 Forget a memory by id (from memory_search).", {"id": S}, ["id"]),
    _fn("schedule_create", "创建定时/周期任务 Create a recurring background task. kind=cron (spec like '0 8 * * *') or "
        "interval (spec = minutes). goal = full standalone instruction for each run.",
        {"name": S, "goal": S, "kind": {"type": "string", "enum": ["cron", "interval"]}, "spec": S}, ["name", "goal", "kind", "spec"]),
    _fn("trigger_create", "创建事件触发器：当某事发生时自动运行一个任务 Create an event trigger (\"when X happens, do Y\"). "
        "source: gmail.new_email (params: query e.g. 'from:boss@acme.com', account) | slack.new_message (params: channel, "
        "keyword, mentions_only) | notion.db_changed (params: database_id). goal = full standalone instruction for each run; "
        "the new items are handed to that run as data. every = poll minutes (default 3).",
        {"name": S, "goal": S, "source": {"type": "string", "enum": ["gmail.new_email", "slack.new_message", "notion.db_changed"]},
         "params": {"type": "object"}, "every": {"type": "number"}}, ["name", "goal", "source"]),
    _fn("goal_create", "创建长期目标（场景目标）：Locius 会定期检查并持续推进，直到达成 Create a long-running goal that Locius keeps "
        "working on until achieved (e.g. 'get John to confirm the contract by Friday', 'keep inbox under 20 unread'). "
        "check_kind: interval (check_spec = minutes, >= 5) | cron (e.g. '0 9 * * *') | event (check_spec = JSON like trigger_create: "
        "{\"source\": ..., \"params\": {...}}). deadline: optional 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'.",
        {"title": S, "objective": S, "success_criteria": S, "check_kind": {"type": "string", "enum": ["interval", "cron", "event"]},
         "check_spec": S, "deadline": S}, ["title", "objective", "check_kind", "check_spec"]),
    _fn("goal_list", "列出长期目标 List goals and their status.", {}),
    _fn("goal_update", "记录本次目标检查的结果（目标检查任务结束前必须调用）Record the outcome of this goal check (MUST be called "
        "at the end of every goal run). status: active (still working) | blocked (need the user's help/decision) | "
        "achieved (success criteria met) | failed (cannot be achieved). progress = what you found/did, concise.",
        {"status": {"type": "string", "enum": ["active", "blocked", "achieved", "failed"]}, "progress": S}, ["status", "progress"]),
    _fn("schedule_list", "列出定时任务 List schedules.", {}),
    _fn("schedule_delete", "删除定时任务 Delete a schedule by id.", {"id": S}, ["id"]),
    _fn("schedule_state_get", "读取本定时任务上次保存的状态 Read state saved by previous runs of this schedule.", {}),
    _fn("schedule_state_set", "保存本定时任务的状态（用于下次对比变化）Save state for the next run of this schedule.",
        {"key": S, "value": S}, ["key", "value"]),
    _fn("notify_user", "给用户发通知（应用内 + Telegram 如已配置）Notify the user (in-app + Telegram if configured).",
        {"title": S, "message": S}, ["title", "message"]),
    _fn("delegate", "派出子 Agent 独立完成一个调研类子任务（只读工具），返回报告 Spawn a read-only sub-agent for a focused sub-task.",
        {"role": S, "task": S}, ["role", "task"]),
    _fn("load_skill", "加载技能说明 Load a skill's detailed instructions by name.", {"name": S}, ["name"]),
]
LOCAL_NAMES = {t["function"]["name"] for t in LOCAL_TOOLS}

# step budget: keep the last steps for producing / sending what the user asked for
BUDGET_RESERVE = 5
RESEARCH_NUDGE_PAGES = 10
BUDGET_MARK = "step budget"
BUDGET_MARK_PAGES = "research check"
FINISH_TOOLS = {"update_plan", "files_write", "files_read", "files_list", "make_pdf", "make_xlsx", "send_file", "notify_user",
                "memory_remember", "goal_update", "schedule_state_set", "gmail_send", "gmail_reply", "gmail_create_draft",
                "slack_send_message", "notion_create_page", "notion_append", "calendar_create_event"}


class Suspend(Exception):
    def __init__(self, status: str, waiting: dict):
        self.status = status
        self.waiting = waiting


class Runtime:
    def __init__(self, data_dir: str, publish):
        self.store = RStore(data_dir)
        self.publish = publish            # async fn(event: dict)
        self.llm = LLM(self.store.settings, on_call=self._on_llm_call)
        self.running: dict[str, asyncio.Task] = {}
        self.cancel_flags: set[str] = set()
        self.pause_flags: set[str] = set()
        self._catalog_cache = (0.0, None)
        os.makedirs(WORKSPACE, exist_ok=True)

    # ================================================================ sentinel client
    async def sentinel(self, method: str, path: str, payload: dict | None = None, timeout: float = 240.0):
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, SENTINEL_URL + path, json=payload, headers={"X-Persona-Runtime": RUNTIME_TOKEN})
            r.raise_for_status()
            return r.json()

    async def audit(self, actor: str, action: str, task_id: str = "", **kw):
        try:
            await self.sentinel("POST", "/internal/audit", {"actor": actor, "action": action, "task_id": task_id, **kw}, timeout=10)
        except Exception:
            pass

    async def _on_llm_call(self, info: dict):
        await self.audit("llm", f"model.{info['purpose']}", info.get("task_id", ""), resource=info.get("model", ""),
                         result="success", detail=info)

    async def catalog(self, force=False) -> dict:
        ts, cat = self._catalog_cache
        if cat and not force and time.time() - ts < 20:
            return cat
        try:
            cat = await self.sentinel("GET", "/internal/catalog", timeout=15)
        except Exception:
            cat = {"tools": [], "connections": {}}
        self._catalog_cache = (time.time(), cat)
        return cat

    # ================================================================ events
    async def event(self, task_id: str, type_: str, data: dict):
        ev = self.store.add_event(task_id, type_, data)
        await self.publish({"kind": "task_event", **ev})

    async def set_status(self, task_id: str, status: str, **kw):
        self.store.update_task(task_id, status=status, **kw)
        t = self.store.task(task_id)
        await self.publish({"kind": "task_update", "task": self.task_brief(t)})
        await self.audit("runtime", "task.status", task_id, result=status, detail={"status": status, **{k: v for k, v in kw.items() if k in ("error",)}})

    @staticmethod
    def task_brief(t: dict) -> dict:
        return {k: t.get(k) for k in ("id", "conv_id", "goal", "status", "plan", "result", "error", "source", "schedule_id",
                                       "parent_id", "steps", "waiting", "created_at", "updated_at", "finished_at")}

    # ================================================================ public entry points
    async def submit(self, conv_id: str, goal: str, source="chat", schedule_id="") -> dict:
        t = self.store.create_task(goal, conv_id, source, schedule_id)
        await self.publish({"kind": "task_update", "task": self.task_brief(t)})
        await self.audit("runtime", "task.create", t["id"], detail={"goal": truncate(goal, 500), "source": source})
        self.start(t["id"])
        return t

    def start(self, task_id: str):
        cur = self.running.get(task_id)
        if cur and not cur.done():
            return
        self.running[task_id] = asyncio.create_task(self._run_guarded(task_id))

    async def _run_guarded(self, task_id: str):
        try:
            await self.run(task_id)
        except Exception as e:
            traceback.print_exc()
            await self.set_status(task_id, "FAILED", error=f"{type(e).__name__}: {e}", finished_at=now_ts())
            await self.event(task_id, "error", {"message": str(e)[:500]})
            t = self.store.task(task_id)
            if t:
                en = self.store.settings().get("language") == "en"
                self.store.add_msg(t["conv_id"], "assistant", f"⚠️ Task failed: {e}" if en else f"⚠️ 任务失败：{e}", task_id)
                await self.publish({"kind": "conv_update", "conv_id": t["conv_id"]})
                await self._warn_unattended(t, ("运行出错", "failed with an error"), f"{type(e).__name__}: {e}")

    async def _warn_unattended(self, t: dict, what: tuple[str, str], detail: str):
        """A scheduled / triggered run went wrong while nobody was watching: tell the user (app + Telegram).
        `what` = (Chinese, English) — the notice follows the app language."""
        if t.get("source") != "schedule":
            return
        sch = self.store.schedule(t["schedule_id"]) if t.get("schedule_id") else None
        name = (sch or {}).get("name") or truncate(t["goal"], 40)
        if self.store.settings().get("language") == "en":
            title = f"⚠️ \"{name}\" {what[1]}"
            body = f"{truncate(detail, 600)}\nOpen the task to see what happened, or run it again with ▶ Run now in Automations."
        else:
            title = f"⚠️ 「{name}」这次{what[0]}"
            body = f"{truncate(detail, 600)}\n可以在「自动化」里点 ▶ 立即运行 重试，或在任务详情里查看过程。"
        try:
            n = self.store.notify(title, body, task_id=t["id"], level="warning")
            await self.publish({"kind": "notification", "notification": n})
            await self.sentinel("POST", "/internal/notify", {"task_id": t["id"], "text": f"{title}\n{body}"}, timeout=20)
        except Exception:
            pass

    async def cancel(self, task_id: str, reason: str = ""):
        self.cancel_flags.add(task_id)
        t = self.store.task(task_id)
        if t and t["status"] not in TERMINAL:
            await self.set_status(task_id, "CANCELLED", finished_at=now_ts(), **({"error": reason} if reason else {}))
            await self.event(task_id, "cancelled", {"reason": reason} if reason else {})
            if t["status"] == "WAITING_APPROVAL":
                # don't leave an orphan approval behind: approving it later would run an action for a dead task
                try:
                    await self.sentinel("POST", "/internal/expire_approvals",
                                        {"task_id": task_id, "reason": reason or "任务已取消 (task cancelled)"}, timeout=15)
                except Exception:
                    pass

    async def pause(self, task_id: str):
        self.pause_flags.add(task_id)
        t = self.store.task(task_id)
        if t and t["status"] in ("RUNNING", "PLANNING", "CREATED"):
            await self.event(task_id, "pause_requested", {})

    async def resume(self, task_id: str):
        self.pause_flags.discard(task_id)
        t = self.store.task(task_id)
        if t and t["status"] == "PAUSED":
            await self.set_status(task_id, "RUNNING", waiting=None)
            self.start(task_id)

    async def on_approval_resolved(self, payload: dict):
        task_id = payload.get("task_id", "")
        t = self.store.task(task_id)
        if not t or t["status"] != "WAITING_APPROVAL":
            return
        pend = t["pending"] or {}
        if pend.get("approval_id") and pend["approval_id"] != payload.get("approval_id"):
            return
        pend["resolved"] = {"call_id": payload.get("call_id"), "decision": payload.get("decision"), "result": payload.get("result")}
        self.store.update_task(task_id, pending=pend)
        await self.event(task_id, "approval_resolved", {"approval_id": payload.get("approval_id"), "decision": payload.get("decision")})
        await self.set_status(task_id, "RUNNING", waiting=None)
        self.start(task_id)

    async def on_takeover_ended(self, payload: dict):
        for t in self.store.tasks(status="WAITING_EXTERNAL", limit=50):
            w = t.get("waiting") or {}
            if w.get("type") in ("takeover", "takeover_requested"):
                full = self.store.task(t["id"])
                pend = full["pending"] or {}
                pend["resolved"] = {"call_id": pend.get("call_id"), "decision": "takeover_ended",
                                    "result": {"status": "ok", "result": prompts.L(
                                        agent_lang(self.store.settings()),
                                        "用户已完成接管并交还浏览器控制权。请先调用 browser_snapshot 查看当前页面状态再继续。",
                                        f"{TAKEOVER_DONE_EN}: the user handed browser control back. Take a browser_snapshot first to see "
                                        "the current page, then continue.")}}
                self.store.update_task(t["id"], pending=pend)
                await self.event(t["id"], "takeover_ended", {})
                await self.set_status(t["id"], "RUNNING", waiting=None)
                self.start(t["id"])

    async def recover(self):
        """On startup: tasks that were mid-flight are resumed; waiting ones stay waiting."""
        for t in self.store.tasks(status="RUNNING,PLANNING,CREATED", limit=50):
            await self.event(t["id"], "recovered", {"note": "runtime restarted"})
            self.start(t["id"])

    # ================================================================ the loop
    def _history(self, conv_id: str, exclude_task: str) -> tuple[list[dict], str]:
        msgs = [m for m in self.store.msgs(conv_id, 30) if m["task_id"] != exclude_task or m["role"] != "user"]
        hist, lines = [], []
        for m in msgs[-12:]:
            if m["role"] not in ("user", "assistant"):
                continue
            c = truncate(m["content"], 2500)
            hist.append({"role": m["role"], "content": c})
            lines.append(f"{m['role']}: {truncate(m['content'], 400)}")
        # drop the current user message (it's appended as the goal)
        if hist and hist[-1]["role"] == "user":
            hist.pop()
            lines.pop()
        while hist and hist[0]["role"] != "user":
            hist.pop(0)
        return hist, "\n".join(lines[-8:])

    def skills(self) -> list[dict]:
        out = []
        if not os.path.isdir(SKILLS_DIR):
            return out
        for name in sorted(os.listdir(SKILLS_DIR)):
            p = os.path.join(SKILLS_DIR, name, "SKILL.md")
            if os.path.isfile(p):
                txt = open(p, encoding="utf-8").read()
                m = re.search(r"^description:\s*(.+)$", txt, re.M)
                out.append({"name": name, "description": m.group(1).strip() if m else "", "path": p})
        return out

    def _facts_for(self, goal: str) -> list[dict]:
        found = self.store.search_facts(goal, 10)
        prefs = [f for f in self.store.facts(60) if f["category"] in ("preference", "person", "profile")][:10]
        seen, out = set(), []
        for f in found + prefs:
            if f["id"] not in seen:
                seen.add(f["id"])
                out.append(f)
        return out[:15]

    @staticmethod
    def reply_lang(task: dict, settings: dict) -> str:
        # one setting decides the agent's language (reasoning, plans, notes, answers) — see Settings → Language
        return prompts.lang_name(agent_lang(settings))

    async def _plan(self, task: dict, facts: list[dict], history_txt: str, state: str = "") -> dict:
        s = self.store.settings()
        mcp = ((await self.catalog()).get("connections") or {}).get("mcp") or {}
        live = [f"{x['name']} (mcp:{x['id']})" for x in mcp.get("servers") or [] if x.get("enabled") and x.get("tools")]
        sys_prompt = (prompts.language_rule(agent_lang(s)) + "\n\n" + prompts.PLANNER_SYSTEM
                      + (f"\nConnected MCP servers: {', '.join(live)}." if live else ""))
        msgs = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompts.planner_user(task["goal"], history_txt, facts, state,
                                                                 reply_lang=self.reply_lang(task, s))}]
        try:
            r = await self.llm.chat(msgs, purpose="planner", task_id=task["id"], max_tokens=1500, temperature=0.2,
                                    model=s.get("planner_model") or None, no_think=True)
            plan = extract_json(r["content"]) or {}
        except LLMError as e:
            await self.event(task["id"], "planner_error", {"message": str(e)[:300]})
            plan = {}
        if not isinstance(plan, dict):
            plan = {}
        steps = []
        for i, st in enumerate(plan.get("steps") or []):
            if isinstance(st, dict) and st.get("description"):
                steps.append({"id": str(st.get("id") or f"s{i + 1}"), "description": str(st["description"])[:300],
                              "tool_hint": str(st.get("tool_hint", ""))[:40], "risk": str(st.get("risk", "read")),
                              "status": "pending"})
        return {"objective": str(plan.get("objective") or task["goal"])[:300], "steps": steps[:10], "version": 1}

    def _tools(self, catalog: dict, allow: set[str] | None = None, schedule: bool = False, goal: bool = False) -> list[dict]:
        tools = list(catalog.get("tools") or []) + LOCAL_TOOLS
        if not schedule:
            tools = [t for t in tools if t["function"]["name"] not in ("schedule_state_get", "schedule_state_set")]
        if not goal:
            tools = [t for t in tools if t["function"]["name"] != "goal_update"]
        if allow is not None:
            tools = [t for t in tools if t["function"]["name"] in allow]
        return tools

    @staticmethod
    def _budget(transcript: list[dict], tools: list[dict], remaining: int, lang: str = "zh") -> list[dict]:
        """Keep the last steps for delivering what the user asked for.

        A research-heavy task used to spend every step reading pages and hit the limit before writing the PDF / sending
        the email. Now: (1) after many page visits, one nudge to start writing; (2) with BUDGET_RESERVE steps left, a note
        to stop gathering and deliver; (3) in the last 2 steps only delivery tools remain."""
        said = "\n".join(str(m.get("content") or "") for m in transcript if m.get("role") == "user")
        pages = sum(1 for m in transcript if m.get("role") == "assistant"
                    for c in m.get("tool_calls") or [] if c.get("function", {}).get("name") == "browser_navigate")
        if pages >= RESEARCH_NUDGE_PAGES and BUDGET_MARK_PAGES not in said and remaining > BUDGET_RESERVE:
            transcript.append({"role": "user", "content": prompts.L(
                lang, f"（系统）[{BUDGET_MARK_PAGES}] 你已经打开了 {pages} 个网页。如果信息已经够用，"
                      "现在就开始写结果/交付物；还缺的话最多再看 2–3 个页面，或者用 delegate 交给子 Agent 去查。",
                f"(System) [{BUDGET_MARK_PAGES}] You have opened {pages} pages. If you have enough, start writing the result now; "
                "otherwise read at most 2–3 more pages or delegate the rest to a sub-agent.")})
        if remaining <= BUDGET_RESERVE and BUDGET_MARK not in said:
            transcript.append({"role": "user", "content": prompts.L(
                lang, f"（系统）[{BUDGET_MARK}] 只剩 {remaining} 步了。停止继续搜集资料，用已有的信息马上完成"
                      "用户要的交付物（写文件、生成 PDF/Excel、send_file、发邮件等），然后给出最终回答，并说明哪些没来得及核实。",
                f"(System) [{BUDGET_MARK}] Only {remaining} steps left: stop gathering, produce the deliverable the user asked for "
                "with what you have (write the file, make_pdf/make_xlsx, send_file, send the email…), then give the final answer "
                "and note anything left unverified.")})
        if remaining <= 2:
            tools = [x for x in tools if x["function"]["name"] in FINISH_TOOLS] or tools
        return tools

    def _compress(self, transcript: list[dict]) -> list[dict]:
        """Keep the context small: shrink old tool results, keep the last few intact."""
        total = sum(len(str(m.get("content") or "")) for m in transcript)
        if total < 70000:
            return transcript
        tool_idx = [i for i, m in enumerate(transcript) if m.get("role") == "tool"]
        for i in tool_idx[:-4]:
            c = str(transcript[i].get("content") or "")
            if len(c) > 600:
                transcript[i]["content"] = c[:500] + "\n…[较早的工具结果已压缩 older result compressed]"
        return transcript

    async def run(self, task_id: str):
        t = self.store.task(task_id)
        if not t or t["status"] in TERMINAL:
            return
        s = self.store.settings()
        catalog = await self.catalog(force=True)
        facts = self._facts_for(t["goal"])
        history, history_txt = self._history(t["conv_id"], task_id)

        if t["status"] in ("CREATED", "PLANNING") and not t["plan"].get("steps") and not t["transcript"]:
            await self.set_status(task_id, "PLANNING")
            plan = await self._plan(t, facts, history_txt)
            self.store.update_task(task_id, plan=plan)
            await self.event(task_id, "plan", plan)
            t = self.store.task(task_id)
        if t["status"] != "RUNNING":
            await self.set_status(task_id, "RUNNING")

        transcript: list[dict] = t["transcript"]
        extra = ""
        from app.runtime import goals as G
        goal = G.goal_for_task(self.store, t)
        if t["source"] == "schedule" and t["schedule_id"]:
            sch = self.store.schedule(t["schedule_id"])
            if goal:
                extra = ("\n## Goal check\nThis task is an automatic check of a long-running goal the user set (the goal, its success "
                         "criteria and the progress history are in the user message). The user is not watching. Take the next useful "
                         "step toward the goal (actions that send/submit still need approval — just call the tool). Do not repeat work "
                         "already recorded in the progress history. You MUST call goal_update exactly once at the end.")
            elif sch:
                st = {k: v for k, v in (sch["state"] or {}).items() if not str(k).startswith("_")}
                kind = "event trigger" if sch["kind"] == "event" else "schedule"
                extra = (f"\n## Scheduled run\nThis task is an automatic run of {kind} 「{sch['name']}」. The user is not watching. "
                         f"State saved by previous runs: {dumps(st)}. Use schedule_state_set to save what you observed; "
                         f"call notify_user only if something the user cares about happened.")
        if not transcript:
            transcript = [{"role": "system", "content": ""}] + history + [{"role": "user", "content": t["goal"]}]

        # ---------------------------------------------------------- resume after approval / takeover
        pend = t["pending"]
        if pend and pend.get("calls"):
            resolved = pend.get("resolved")
            if not resolved:
                return  # still waiting
            first = pend["calls"][0]
            transcript.append({"role": "tool", "tool_call_id": first["id"],
                               "content": self._format_external(first["name"], resolved.get("result") or {})})
            await self.event(task_id, "tool_result", {"call_id": first["id"], "name": first["name"],
                                                      "status": (resolved.get("result") or {}).get("status", resolved.get("decision")),
                                                      "preview": truncate(transcript[-1]["content"], 800)})
            rest = pend["calls"][1:]
            self.store.update_task(task_id, pending=None, transcript=transcript)
            try:
                for call in rest:
                    await self._exec_call(t, call, transcript, catalog)
            except Suspend as sp:
                return await self._suspend(task_id, sp, transcript)

        steps = int(t["steps"] or 0)
        max_steps = int(s.get("max_steps") or 30)
        consecutive_errors = 0
        nudged = False
        while True:
            if task_id in self.cancel_flags:
                return
            if task_id in self.pause_flags:
                self.pause_flags.discard(task_id)
                self.store.update_task(task_id, transcript=transcript)
                await self.set_status(task_id, "PAUSED", waiting={"type": "paused"})
                return
            t = self.store.task(task_id)
            transcript[0] = {"role": "system", "content": prompts.executor_system(
                user_name=s["user_name"], tz=s["timezone"], connections=catalog.get("connections", {}), plan=t["plan"],
                facts=facts, skills=self.skills(), extra=extra, language=agent_lang(s),
                reply_lang=self.reply_lang(t, s))}
            transcript = self._compress(transcript)
            force_final = steps >= max_steps
            tools = None if force_final else self._tools(catalog, schedule=bool(t["schedule_id"]) and not goal, goal=bool(goal))
            lg = agent_lang(s)
            if not force_final:
                tools = self._budget(transcript, tools, max_steps - steps, lg)
                if lg == "en":
                    tools = prompts.strip_tools_en(tools)
            if force_final:
                transcript.append({"role": "user", "content": prompts.L(
                    lg, "（系统）已达到步数上限。请停止调用工具，总结目前完成的内容、结果和未完成的部分。",
                    "(System) Step limit reached: stop calling tools and summarize what is done, the results and what is left.")})
            await self.event(task_id, "thinking", {"step": steps + 1})
            try:
                resp = await self.llm.chat(transcript, tools, purpose="executor", task_id=task_id)
            except LLMError as e:
                self.store.update_task(task_id, transcript=transcript)
                raise
            steps += 1
            self.store.update_task(task_id, steps=steps)
            if resp["reasoning"]:
                await self.event(task_id, "reasoning", {"text": truncate(resp["reasoning"], 1500)})
            calls = resp["tool_calls"] if not force_final else []
            if calls:
                transcript.append({"role": "assistant", "content": resp["content"] or "",
                                   "tool_calls": [{"id": c["id"], "type": "function",
                                                   "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                                                  for c in calls]})
                if resp["content"]:
                    await self.event(task_id, "message", {"text": truncate(resp["content"], 2000)})
                self.store.update_task(task_id, transcript=transcript)
                try:
                    for i, call in enumerate(calls):
                        ok = await self._exec_call(t, call, transcript, catalog, remaining=calls[i + 1:])
                        consecutive_errors = 0 if ok else consecutive_errors + 1
                except Suspend as sp:
                    return await self._suspend(task_id, sp, transcript)
                self.store.update_task(task_id, transcript=transcript)
                if consecutive_errors >= 3:
                    await self._replan(task_id, transcript, facts, history_txt)
                    consecutive_errors = 0
                continue
            final = resp["content"]
            if not final and not nudged:
                nudged = True
                transcript.append({"role": "user", "content": prompts.L(agent_lang(s), "（系统）请给出最终回答。",
                                                                        "(System) Please write the final answer now.")})
                continue
            final = final or "（任务已结束，但模型没有返回文字说明。）"
            if agent_lang(s) == "en" and prompts.cjk_share(final) > 0.5:
                final = await self._rewrite_in_english(task_id, transcript, final)
            transcript.append({"role": "assistant", "content": final})
            plan = t["plan"]
            for st in plan.get("steps", []):
                if st.get("status") in ("pending", "running"):
                    st["status"] = "done" if not force_final else st["status"]
            self.store.update_task(task_id, transcript=transcript, result=final, plan=plan, finished_at=now_ts())
            if force_final:
                # ran out of steps: this is not a success — say so instead of quietly marking it completed
                why = (f"Stopped at the step limit ({max_steps} steps) before finishing" if s.get("language") == "en"
                       else f"达到步数上限（{max_steps} 步），任务没有做完")
                await self.set_status(task_id, "FAILED", error=why)
            else:
                await self.set_status(task_id, "COMPLETED")
            await self.event(task_id, "final", {"text": truncate(final, 4000)})
            self.store.add_msg(t["conv_id"], "assistant", final, task_id)
            await self.publish({"kind": "conv_update", "conv_id": t["conv_id"]})
            if force_final:
                await self._warn_unattended(t, (f"达到 {max_steps} 步上限，没有做完", f"hit the {max_steps}-step limit and didn't finish"), final)
                return
            self.store.add_episode(task_id, f"{t['goal'][:200]} → {final[:600]}")
            if t["source"] == "schedule":
                sch = self.store.schedule(t["schedule_id"]) if t["schedule_id"] else None
                self.store.db.execute("UPDATE schedules SET last_task=? WHERE id=?", (task_id, t["schedule_id"]))
                g = G.goal_for_task(self.store, t)
                if g and g["status"] == "active" and not any(p.get("task_id") == task_id for p in g.get("progress") or []):
                    auto = "(auto-recorded) " if s.get("language") == "en" else "（自动记录 auto）"
                    await G.apply_update(self, g, "active", auto + truncate(final, 500), task_id)
            if s.get("memory_extraction") and t["source"] == "chat":
                asyncio.create_task(self._extract_memory(t))
            return

    async def _rewrite_in_english(self, task_id: str, transcript: list[dict], final: str) -> str:
        """English mode but the model answered in Chinese (e.g. memory says the user likes Chinese): ask once for English."""
        msgs = transcript + [{"role": "assistant", "content": final},
                             {"role": "user", "content": "(System) Settings → Language is English. Rewrite your complete final answer "
                                                         "in English now — same content, same structure, no tool calls. Keep "
                                                         "names, email subjects and quotes in their original language only where needed."}]
        try:
            r = await self.llm.chat(msgs, None, purpose="executor", task_id=task_id)
        except LLMError:
            return final
        text = (r.get("content") or "").strip()
        if text and prompts.cjk_share(text) < prompts.cjk_share(final):
            await self.event(task_id, "language_fixed", {"from": "zh", "to": "en"})
            return text
        return final

    async def _replan(self, task_id: str, transcript: list[dict], facts, history_txt):
        t = self.store.task(task_id)
        recent = [m for m in transcript if m.get("role") == "tool"][-4:]
        state = "\n".join(truncate(str(m.get("content")), 400) for m in recent)
        await self.event(task_id, "replanning", {"reason": "连续失败 consecutive failures"})
        plan = await self._plan(t, facts, history_txt, state=f"Previous plan: {dumps(t['plan'])}\nRecent failures:\n{state}")
        plan["version"] = int(t["plan"].get("version", 1)) + 1
        self.store.update_task(task_id, plan=plan)
        await self.event(task_id, "plan", plan)
        transcript.append({"role": "user", "content": prompts.L(agent_lang(self.store.settings()),
                                                                "（系统）多次失败后已重新规划，请按新计划换一种方法继续。",
                                                                "(System) Several steps failed; a new plan was made — try a different approach.")})

    async def _suspend(self, task_id: str, sp: Suspend, transcript: list[dict]):
        self.store.update_task(task_id, transcript=transcript, pending=sp.waiting.pop("_pending"), waiting=sp.waiting)
        await self.set_status(task_id, sp.status, waiting=sp.waiting)
        await self.event(task_id, "waiting", sp.waiting)
        t = self.store.task(task_id)
        if sp.status == "WAITING_APPROVAL":
            self.store.add_msg(t["conv_id"], "system", dumps({"type": "approval", "approval_id": sp.waiting.get("approval_id"),
                                                               "title": (sp.waiting.get("summary") or {}).get("title", ""),
                                                               "task_id": task_id}), task_id)
            await self.publish({"kind": "approval_requested", "task_id": task_id, "approval_id": sp.waiting.get("approval_id")})
        elif sp.waiting.get("type") == "takeover_requested":
            self.store.add_msg(t["conv_id"], "system", dumps({"type": "takeover", "reason": sp.waiting.get("reason", ""),
                                                               "task_id": task_id}), task_id)
            await self.publish({"kind": "takeover_requested", "task_id": task_id, "reason": sp.waiting.get("reason", "")})
        await self.publish({"kind": "conv_update", "conv_id": t["conv_id"]})

    # ================================================================ tool execution
    def _format_external(self, name: str, res: dict) -> str:
        st = res.get("status")
        if st == "ok":
            body = res.get("result")
            txt = json.dumps(body, ensure_ascii=False, indent=None, default=str)
            if isinstance(body, dict) and body.get("trust") == "untrusted" or (isinstance(body, dict) and "messages" in body):
                src = body.get("source", name) if isinstance(body, dict) else name
                warn = ""
                if isinstance(body, dict) and body.get("injection_warning"):
                    warn = f" injection_warning=\"{','.join(body['injection_warning'])}\""
                return f"<untrusted_content source=\"{truncate(str(src), 120)}\"{warn}>\n{truncate(txt, RESULT_LIMIT)}\n</untrusted_content>"
            return truncate(txt, RESULT_LIMIT)
        if st == "denied":
            return (f"DENIED by Sentinel: {res.get('reason') or res.get('error')}. "
                    "不要重试相同操作 Do not retry the same action; explain to the user or choose a different approach.")
        if st == "approved":
            return self._format_external(name, res.get("result") or {})
        return (f"ERROR: {res.get('error') or res.get('reason') or res}. "
                "请分析原因并换一种方法 Diagnose the cause and try a different approach (e.g. take a new snapshot).")

    async def _exec_call(self, t: dict, call: dict, transcript: list[dict], catalog: dict, remaining: list | None = None,
                         allow: set[str] | None = None, sub: bool = False) -> bool:
        task_id = t["id"]
        name, args = call["name"], call.get("args") or {}
        await self.event(task_id, "tool_call", {"call_id": call["id"], "name": name, "args": _preview_args(args), "sub": sub})
        ext_names = {x["function"]["name"] for x in catalog.get("tools", [])}
        ok = True
        repeated = repeat_guard(transcript, call)
        if repeated:
            content = repeated
            ok = False
            await self.audit("executor", name, task_id, resource="loop_guard", risk="low", decision="DENY",
                             result="repeat_blocked", detail={"args": _preview_args(args)})
        elif allow is not None and name not in allow:
            content = f"ERROR: tool {name} is not available to this agent."
            ok = False
        elif name in LOCAL_NAMES:
            try:
                content = await self._local(t, name, args)
            except Suspend:
                raise
            except Exception as e:
                content = f"ERROR: {type(e).__name__}: {e}"
                ok = False
            await self.audit("executor", name, task_id, resource="local", risk="low", decision="ALLOW",
                             result="success" if ok else "error", detail={"args": _preview_args(args)})
        elif name == "browser_navigate" and (blocked := site_blocked(transcript, str(args.get("url", "")))):
            content = (f"ERROR: [SITE BLOCKED site={blocked}] 这个网站之前已经拦截了自动浏览器，换网址也一样，不再重试。"
                       "请换一个有同样信息的来源（例如订餐厅：Google 地图、Chope、TableCheck、餐厅官网），"
                       "或者如果一定要用这个网站，调用 browser_request_takeover 请用户自己通过验证。"
                       f" {blocked} already blocked automated browsing in this task; other URLs on it will be blocked too. "
                       "Use another source, or browser_request_takeover if this exact site is essential.")
            ok = False
        elif name in ext_names:
            try:
                res = await self.sentinel("POST", "/internal/act", {"task_id": task_id, "call_id": call["id"], "tool": name,
                                                                    "args": args, "no_ask": sub})
            except Exception as e:
                res = {"status": "error", "error": f"Sentinel 不可用: {e}"}
            st = res.get("status")
            if st == "approval_required" and not sub:
                raise Suspend("WAITING_APPROVAL", {"type": "approval", "approval_id": res.get("approval_id"),
                                                   "summary": res.get("summary"), "reason": res.get("reason"), "tool": name,
                                                   "_pending": {"approval_id": res.get("approval_id"), "call_id": call["id"],
                                                                "calls": [call] + list(remaining or [])}})
            if st == "approval_required" and sub:
                res = {"status": "denied", "reason": "子 Agent 不能执行需要审批的操作 (sub-agents cannot run actions that need approval)"}
            if st in ("paused", "waiting_user") and not sub:
                wt = "takeover_requested" if st == "waiting_user" else "takeover"
                raise Suspend("WAITING_EXTERNAL", {"type": wt, "reason": args.get("reason") or res.get("error", ""), "tool": name,
                                                   "_pending": {"call_id": call["id"], "calls": [call] + list(remaining or [])}})
            content = self._format_external(name, res)
            ok = st == "ok"
            blk = (res.get("result") or {}).get("blocked") if st == "ok" and isinstance(res.get("result"), dict) else None
            if blk:
                site = site_brand((res.get("result") or {}).get("url") or args.get("url", ""))
                content = (f"[SITE BLOCKED site={site}] 这个页面是反机器人拦截页（{blk.get('detail')}），不是网站的真实内容。"
                           "整个网站都会拦截自动浏览器：不要再换网址重试。改用其他有同样信息的来源；"
                           "如果一定要用这个网站，调用 browser_request_takeover，请用户自己完成验证后再继续。不要尝试破解验证码。"
                           f" This is a bot-protection wall ({blk.get('detail')}), not the site's content. Do not retry other URLs "
                           "on this site; switch to another source, or request a takeover if this site is essential.\n" + content)
                ok = False
                await self.event(task_id, "site_blocked", {"site": site, "kind": blk.get("kind"), "detail": blk.get("detail")})
        else:
            content = f"ERROR: 未知工具 unknown tool '{name}'. Available tools are listed in the tool schema."
            ok = False
        if not ok and agent_lang(self.store.settings()) == "en":
            content = prompts.system_text_en(content)
        transcript.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        await self.event(task_id, "tool_result", {"call_id": call["id"], "name": name, "ok": ok, "sub": sub,
                                                  "preview": truncate(content, 800)})
        return ok

    # ---------------------------------------------------------------- local tools
    def _path(self, rel: str) -> str:
        rel = (rel or "").strip().lstrip("/")
        full = os.path.realpath(os.path.join(WORKSPACE, rel))
        if full != WORKSPACE and not full.startswith(WORKSPACE + os.sep):
            raise ValueError("路径必须在工作区内 (path must be inside the workspace)")
        return full

    async def _send_file(self, t: dict, a: dict) -> str:
        """Post a workspace file into the conversation as a download card (+ Telegram when the chat came from there)."""
        p = self._path(a.get("path", ""))
        if os.sep + ".quarantine" in p:
            return "ERROR: 隔离区里的文件不能发送（可能不安全）Files in quarantine can't be sent."
        if not os.path.isfile(p):
            return f"ERROR: 文件不存在 file not found: {a.get('path', '')}. 用 files_list 确认路径 Check the path with files_list."
        size = os.path.getsize(p)
        if size > SEND_FILE_MAX:
            return f"ERROR: 文件太大 ({size // 1_000_000} MB > {SEND_FILE_MAX // 1_000_000} MB) file too large to send."
        rel = os.path.relpath(p, WORKSPACE)
        note = truncate(str(a.get("note") or ""), 300)
        info = {"type": "file", "path": rel, "name": os.path.basename(p), "size": size,
                "mime": mimetypes.guess_type(p)[0] or "application/octet-stream", "note": note, "task_id": t["id"]}
        self.store.add_msg(t["conv_id"], "system", dumps(info), t["id"])
        await self.publish({"kind": "conv_update", "conv_id": t["conv_id"]})
        extra = ""
        try:
            r = await self.sentinel("POST", "/internal/send_file", {"task_id": t["id"], "conv_id": t["conv_id"], "path": rel,
                                                                    "name": info["name"], "caption": note}, timeout=120)
            if r.get("sent"):
                extra = " 也已发到 Telegram (also sent to Telegram)."
            elif r.get("error"):
                extra = f" Telegram 没有发出 (not sent to Telegram): {r['error']}"
        except Exception:
            pass
        return (f"已发送到对话，用户可以直接点击下载 Sent to the chat as a download: {rel} ({size} bytes).{extra} "
                "不需要再告诉用户去 Files 里找 No need to tell the user where to find it.")

    def _make_xlsx(self, a: dict) -> str:
        from app.common import xlsx
        out = str(a.get("output") or "").strip()
        if not out:
            return "ERROR: 需要 output（.xlsx 路径）Give an output path ending in .xlsx"
        if not out.lower().endswith(".xlsx"):
            out += ".xlsx"
        p = self._path(out)
        if os.sep + ".quarantine" in p:
            return "ERROR: 不能写入隔离区"
        sheets = a.get("sheets") or []
        if not sheets and a.get("source"):
            src = self._path(str(a["source"]))
            if not os.path.isfile(src):
                return f"ERROR: 文件不存在 file not found: {a['source']}"
            text = open(src, encoding="utf-8", errors="replace").read()
            rows = xlsx.rows_from_csv(text) if src.lower().endswith((".csv", ".tsv")) else xlsx.rows_from_markdown(text)
            if not rows:
                return "ERROR: 源文件里没有找到表格 no CSV rows / Markdown table found in source"
            sheets = [{"name": os.path.splitext(os.path.basename(src))[0], "columns": rows[0], "rows": rows[1:]}]
        if not isinstance(sheets, list) or not sheets:
            return "ERROR: 需要 sheets 或 source Give sheets=[{name, columns, rows}] or a source file."
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            info = xlsx.make(p, [x for x in sheets if isinstance(x, dict)], formulas=bool(a.get("formulas")))
        except Exception as e:
            return f"ERROR: Excel 生成失败 (xlsx export failed): {e}"
        rel = os.path.relpath(p, WORKSPACE)
        return (f"Excel 已在本机生成 created locally: {rel} ({os.path.getsize(p) // 1024 or 1} KB, sheets: "
                f"{', '.join(info['sheets'])}, {info['rows']} rows)。如果用户要这个文件，用 send_file 发给他 Use send_file to give it to the user.")

    async def _local(self, t: dict, name: str, a: dict) -> str:
        tid = t["id"]
        if name == "update_plan":
            plan = self.store.task(tid)["plan"] or {}
            new_steps = a.get("steps") or []
            if new_steps and all(isinstance(x, dict) for x in new_steps):
                by_id = {s["id"]: s for s in plan.get("steps", [])}
                replace = any(x.get("id") not in by_id for x in new_steps) and any(x.get("description") for x in new_steps)
                if replace:
                    plan["steps"] = [{"id": str(x.get("id") or f"s{i + 1}"), "description": str(x.get("description", ""))[:300],
                                      "status": x.get("status", "pending")} for i, x in enumerate(new_steps)]
                    plan["version"] = int(plan.get("version", 1)) + 1
                else:
                    for x in new_steps:
                        s = by_id.get(x.get("id"))
                        if s:
                            if x.get("status"):
                                s["status"] = x["status"]
                            if x.get("description"):
                                s["description"] = str(x["description"])[:300]
            if a.get("objective"):
                plan["objective"] = str(a["objective"])[:300]
            if a.get("note"):
                plan["note"] = str(a["note"])[:500]
            self.store.update_task(tid, plan=plan)
            await self.event(tid, "plan", plan)
            return "计划已更新 plan updated"
        if name == "files_list":
            p = self._path(a.get("path", ""))
            if not os.path.isdir(p):
                return f"ERROR: 不是目录 not a directory: {a.get('path')}"
            items = []
            for n in sorted(os.listdir(p))[:300]:
                if n.startswith("."):
                    continue
                fp = os.path.join(p, n)
                items.append(f"{'📁' if os.path.isdir(fp) else '📄'} {os.path.relpath(fp, WORKSPACE)}"
                             + ("" if os.path.isdir(fp) else f"  ({os.path.getsize(fp)} bytes)"))
            return "\n".join(items) or "(空 empty)"
        if name == "files_read":
            p = self._path(a["path"])
            if not os.path.isfile(p):
                return f"ERROR: 文件不存在 file not found: {a['path']}"
            if os.sep + ".quarantine" + os.sep in p:
                return "ERROR: 隔离区文件不可读取 (quarantined file)"
            limit = max(1000, min(int(a.get("max_chars") or 12000), 40000))
            if p.lower().endswith((".xlsx", ".xlsm")):
                try:
                    from app.common import xlsx
                    txt = xlsx.read_text(p, limit)
                except Exception as e:
                    return f"ERROR: 无法读取 Excel 文件 (cannot read workbook): {e}"
            elif p.lower().endswith(".pdf"):
                try:
                    from pypdf import PdfReader
                    txt = "\n".join((pg.extract_text() or "") for pg in PdfReader(p).pages[:60])
                except Exception as e:
                    return f"ERROR: 无法解析 PDF: {e}"
            else:
                with open(p, "rb") as f:
                    raw = f.read(limit * 4)
                if b"\x00" in raw[:2000]:
                    return "ERROR: 二进制文件无法按文本读取 (binary file)"
                txt = raw.decode("utf-8", errors="replace")
            return f"<untrusted_content source=\"file {a['path']}\">\n{truncate(txt, limit)}\n</untrusted_content>"
        if name == "files_write":
            p = self._path(a["path"])
            if os.sep + ".quarantine" in p:
                return "ERROR: 不能写入隔离区"
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a" if a.get("append") else "w", encoding="utf-8") as f:
                f.write(str(a.get("content", "")))
            return f"已写入 written: {os.path.relpath(p, WORKSPACE)} ({os.path.getsize(p)} bytes)"
        if name == "send_file":
            return await self._send_file(t, a)
        if name == "make_pdf":
            if not (a.get("source") or a.get("markdown")):
                return "ERROR: 需要 source（工作区文件）或 markdown（内容）Give either source or markdown."
            r = await self.sentinel("POST", "/internal/render_pdf", {"task_id": tid, **{k: a.get(k) or "" for k in
                                                                                    ("source", "markdown", "output", "title")}},
                                    timeout=180)
            if r.get("error") or not r.get("path"):
                return f"ERROR: PDF 生成失败 (PDF export failed): {r.get('error') or r}"
            return (f"PDF 已在本机生成 created locally: {r['path']} ({r.get('size', 0) // 1024} KB)。"
                    "如果用户要这个文件，用 send_file 发给他 Use send_file to give it to the user.")
        if name == "make_xlsx":
            return self._make_xlsx(a)
        if name == "files_search":
            q = str(a.get("query", "")).lower()
            hits = []
            for root, dirs, files in os.walk(WORKSPACE):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fn in files:
                    fp = os.path.join(root, fn)
                    rel = os.path.relpath(fp, WORKSPACE)
                    if q in fn.lower():
                        hits.append(f"{rel} (文件名匹配 name match)")
                    if fn.lower().endswith((".pdf", ".xlsx", ".xlsm", ".png", ".jpg", ".zip")) or os.path.getsize(fp) > 5_000_000:
                        continue
                    try:
                        with open(fp, encoding="utf-8", errors="ignore") as f:
                            for i, line in enumerate(f):
                                if q in line.lower():
                                    hits.append(f"{rel}:{i + 1}: {truncate(line.strip(), 200)}")
                                    break
                    except Exception:
                        pass
                    if len(hits) > 60:
                        break
            return "\n".join(hits[:60]) or "没有找到 no matches"
        if name == "memory_search":
            rows = self.store.search_facts(str(a.get("query", "")), 15)
            eps = [e for e in self.store.episodes(200) if any(w in e["summary"] for w in str(a.get("query", "")).split() if len(w) > 1)][:5]
            out = [f"[{r['id']}] {r['fact']}" for r in rows]
            out += [f"(episode {time.strftime('%Y-%m-%d', time.localtime(e['ts']))}) {truncate(e['summary'], 300)}" for e in eps]
            return "\n".join(out) or "没有相关记忆 no memories found"
        if name == "memory_remember":
            r = self.store.add_fact(str(a["fact"]), str(a.get("category") or "other"), str(a.get("entity") or ""),
                                    source=f"user-request:{tid}", confidence=0.95)
            await self.publish({"kind": "memory_update"})
            return f"已记住 remembered: {r}" if r else "ERROR: empty fact"
        if name == "memory_forget":
            self.store.delete_fact(str(a["id"]))
            await self.publish({"kind": "memory_update"})
            return "已删除 forgotten"
        if name == "schedule_create":
            from app.runtime.scheduler import create_schedule
            sch = create_schedule(self.store, str(a["name"]), str(a["goal"]), str(a["kind"]), str(a["spec"]),
                                  self.store.settings()["timezone"])
            await self.publish({"kind": "schedule_update"})
            await self.audit("executor", "schedule.create", tid, detail=sch)
            return f"已创建定时任务 schedule created: id={sch['id']}, next run {time.strftime('%Y-%m-%d %H:%M', time.localtime(sch['next_run']))}"
        if name == "trigger_create":
            from app.runtime.scheduler import create_schedule
            spec = {"source": a.get("source"), "params": a.get("params") or {}, "every": a.get("every") or 3}
            try:
                sch = create_schedule(self.store, str(a["name"]), str(a["goal"]), "event", dumps(spec), self.store.settings()["timezone"])
            except ValueError as e:
                return f"ERROR: {e}"
            await self.publish({"kind": "schedule_update"})
            await self.audit("executor", "trigger.create", tid, resource=sch["id"], detail={"name": sch["name"], "spec": sch["spec"]})
            return (f"已创建事件触发器 trigger created: id={sch['id']}. 第一次检查只记录当前状态，之后出现的新事件才会触发 "
                    f"(existing items won't fire; only new ones).")
        if name == "goal_create":
            from app.runtime import goals as G
            try:
                g = G.create_goal(self.store, title=str(a.get("title", "")), objective=str(a.get("objective", "")),
                                  criteria=str(a.get("success_criteria", "")), kind=str(a.get("check_kind", "interval")),
                                  spec=str(a.get("check_spec", "")), tz=self.store.settings()["timezone"], deadline=a.get("deadline"))
            except ValueError as e:
                return f"ERROR: {e}"
            await self.publish({"kind": "goal_update", "goal_id": g["id"]})
            await self.audit("executor", "goal.create", tid, resource=g["id"], detail={"title": g["title"]})
            sch = self.store.schedule(g["schedule_id"])
            return (f"已创建目标 goal created: id={g['id']}「{g['title']}」, first check "
                    f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(sch['next_run']))}. 用户可在「自动化 Automations」页查看进展。")
        if name == "goal_list":
            from app.runtime import goals as G
            return "\n".join(f"{g['id']}: {g['title']} [{G.STATUS_ZH.get(g['status'], g['status'])}] — "
                             f"{truncate((g['progress'][-1]['note'] if g['progress'] else g['objective']), 150)}"
                             for g in self.store.goals()) or "(无 none)"
        if name == "goal_update":
            from app.runtime import goals as G
            g = G.goal_for_task(self.store, t)
            if not g:
                return "ERROR: 只能在目标检查任务中使用 (only inside a goal check)"
            if any(p.get("task_id") == tid for p in g.get("progress") or []):
                return "ERROR: 本次检查已经记录过了 (already recorded for this run)"
            return await G.apply_update(self, g, str(a.get("status", "active")), str(a.get("progress", "")), tid)
        if name == "schedule_list":
            return "\n".join(f"{x['id']}: {x['name']} [{x['kind']} {x['spec']}] {'on' if x['enabled'] else 'off'} — {truncate(x['goal'], 120)}"
                             for x in self.store.schedules()) or "(无 none)"
        if name == "schedule_delete":
            self.store.db.execute("DELETE FROM schedules WHERE id=?", (str(a["id"]),))
            await self.publish({"kind": "schedule_update"})
            return "已删除 deleted"
        if name in ("schedule_state_get", "schedule_state_set"):
            sid = t.get("schedule_id")
            if not sid:
                return "ERROR: 只能在定时任务运行中使用 (only inside a scheduled run)"
            sch = self.store.schedule(sid)
            if not sch:
                return "ERROR: schedule not found"
            if name == "schedule_state_get":
                return dumps(sch["state"])
            st = sch["state"]
            st[str(a["key"])[:80]] = str(a["value"])[:2000]
            st["_updated"] = time.strftime("%Y-%m-%d %H:%M")
            self.store.db.execute("UPDATE schedules SET state=? WHERE id=?", (dumps(st), sid))
            return "已保存 saved"
        if name == "notify_user":
            n = self.store.notify(str(a["title"])[:120], str(a["message"])[:2000], tid)
            await self.publish({"kind": "notification", "notification": n})
            try:
                await self.sentinel("POST", "/internal/notify", {"task_id": tid, "text": f"🔔 {a['title']}\n{a['message']}"}, timeout=20)
            except Exception:
                pass
            return "已通知用户 user notified"
        if name == "load_skill":
            for sk in self.skills():
                if sk["name"] == str(a.get("name", "")).strip():
                    return open(sk["path"], encoding="utf-8").read()
            return "ERROR: 没有这个技能 unknown skill. Available: " + ", ".join(s["name"] for s in self.skills())
        if name == "delegate":
            return await self._subagent(t, str(a.get("role", "researcher")), str(a.get("task", "")))
        return f"ERROR: unknown local tool {name}"

    # ---------------------------------------------------------------- sub-agents
    async def _subagent(self, parent: dict, role: str, task: str) -> str:
        count = sum(1 for e in self.store.events(parent["id"]) if e["type"] == "subagent_start")
        if count >= 5:
            return "ERROR: 子 Agent 数量已达上限 (max 5 sub-agents per task)"
        s = self.store.settings()
        catalog = await self.catalog()
        await self.event(parent["id"], "subagent_start", {"role": role, "task": truncate(task, 500)})
        lg = agent_lang(s)
        transcript = [{"role": "system", "content": prompts.SUBAGENT_SYSTEM.format(
                          role=role, now=prompts.now_str(s["timezone"], lg), lang_rule=prompts.language_rule(lg))},
                      {"role": "user", "content": task}]
        tools = self._tools(catalog, allow=SUBAGENT_TOOLS)
        if lg == "en":
            tools = prompts.strip_tools_en(tools)
        for step in range(12):
            force = step == 11
            resp = await self.llm.chat(transcript, None if force else tools, purpose="subagent", task_id=parent["id"])
            if resp["tool_calls"] and not force:
                transcript.append({"role": "assistant", "content": resp["content"] or "",
                                   "tool_calls": [{"id": c["id"], "type": "function",
                                                   "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                                                  for c in resp["tool_calls"]]})
                for c in resp["tool_calls"]:
                    await self._exec_call(parent, c, transcript, catalog, allow=SUBAGENT_TOOLS, sub=True)
                transcript = self._compress(transcript)
                continue
            if force and not resp["content"]:
                continue
            report = resp["content"] or "(子 Agent 没有返回内容)"
            await self.event(parent["id"], "subagent_done", {"role": role, "report": truncate(report, 1500)})
            return f"<subagent_report role=\"{role}\">\n{truncate(report, 6000)}\n</subagent_report>"
        return "ERROR: 子 Agent 未能完成 (sub-agent ran out of steps)"

    # ---------------------------------------------------------------- memory extraction
    async def _extract_memory(self, t: dict):
        user_msgs = [m["content"] for m in self.store.msgs(t["conv_id"], 20) if m["role"] == "user"][-4:]
        text = "\n".join(f"- {truncate(x, 600)}" for x in user_msgs)
        if len(text) < 15:
            return
        try:
            r = await self.llm.chat([{"role": "user", "content": prompts.MEMORY_EXTRACT + text}], purpose="memory",
                                    task_id=t["id"], max_tokens=800, temperature=0.1, no_think=True)
            data = extract_json(r["content"]) or {}
        except Exception:
            return
        added = []
        for f in (data.get("facts") if isinstance(data, dict) else []) or []:
            if isinstance(f, dict) and f.get("fact") and len(str(f["fact"])) < 300:
                res = self.store.add_fact(str(f["fact"]), str(f.get("category") or "other"), str(f.get("entity") or ""),
                                          source=f"extracted:{t['id']}", confidence=0.7)
                if res and not res.get("duplicate"):
                    added.append(res["fact"])
        if added:
            await self.event(t["id"], "memory_saved", {"facts": added})
            await self.audit("memory", "memory.extract", t["id"], result="success", detail={"facts": added})
            await self.publish({"kind": "memory_update"})


def agent_lang(settings: dict) -> str:
    """The agent's language: 'en' or 'zh' (Settings → Language; unset = Chinese, as before)."""
    return "en" if (settings or {}).get("language") == "en" else "zh"


_CC_SLD = {"co", "com", "net", "org", "gov", "ac", "edu", "or", "ne", "go"}
TAKEOVER_DONE_MARK = "用户已完成接管"
TAKEOVER_DONE_EN = "Takeover finished"


def site_brand(url: str) -> str:
    """opentable.com / opentable.sg / m.opentable.sg → 'opentable' (one site, many domains)."""
    from urllib.parse import urlparse
    u = url if "://" in (url or "") else "https://" + (url or "")
    host = (urlparse(u).hostname or "").lower().strip(".")
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _CC_SLD:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else host


def site_blocked(transcript: list[dict], url: str) -> str:
    """The site of `url` showed a bot wall earlier in this task and the user hasn't taken over since → its brand."""
    brand = site_brand(url)
    if not brand:
        return ""
    mark = f"[SITE BLOCKED site={brand}]"
    for m in reversed(transcript):
        c = str(m.get("content") or "")
        if m.get("role") == "tool" and (TAKEOVER_DONE_MARK in c or TAKEOVER_DONE_EN in c):
            return ""
        if m.get("role") == "tool" and c.startswith(mark):
            return brand
    return ""


def _preview_args(a: dict) -> dict:
    out = {}
    for k, v in (a or {}).items():
        out[k] = truncate(v, 300) if isinstance(v, str) else v
    return out
