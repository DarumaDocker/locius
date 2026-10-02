<p align="center"><img src="deploy/market/assets/icon.png" width="96" alt="OMuse"></p>

<h1 align="center">OMuse</h1>
<p align="center"><b>Your private AI agent that lives on your Olares — and asks before it acts.</b><br>
运行在你 Olares 上的私人 AI 智能体——重要的事，先问你。</p>

![OMuse](deploy/market/assets/featured.webp)

[English](#english) · [中文](#中文)

---

## English

OMuse is a local-first AI agent for [Olares](https://www.olares.com). It doesn't just chat: it turns a request into a plan and carries it out across your email, Notion, Slack and the web. Anything that sends, posts, pays or deletes waits for your approval.

### Features

- **Chat → plan → act** – every step and tool call is visible.
- **Email** (several mailboxes: Gmail, Outlook / Hotmail via OAuth 2.0, Yahoo, iCloud, QQ Mail, NetEase 163 / 126, Zoho, AOL, any IMAP / SMTP server) – search, read, draft, reply, send, one-click unsubscribe. Sign-in codes and reset links are hidden from the model.
- **Notion & Slack** – look things up, write reports, update boards, read and post messages.
- **Web browser** – its own Chromium with a restricted API, live view, and human takeover for logins/CAPTCHAs.
- **MCP connectors** – plug in any MCP (Model Context Protocol) server; tool definitions are pinned.
- **Automations** – multi-day goals, event triggers (new email / Slack message / Notion change) and schedules.
- **Telegram remote control** – give tasks and approve actions from your phone.
- **Memory & skills**, **English / 中文 UI**.

### Safe by design

| Layer | What it does |
|---|---|
| **Sentinel** | Independent guard service: ALLOW / DENY / ASK for every action. |
| **Vault** | App passwords and tokens are encrypted in Sentinel; the agent and the model never see them. |
| **Prompt-injection defences** | Emails, pages and tool output are untrusted; suspicious content escalates writes to human approval. |
| **Audit trail** | Append-only, hash-chained log of every model call, tool call, decision and approval. |

### Architecture

```
 browser / Telegram ──► Sentinel :8080  (UI, policy, vault, approvals, audit, connectors)
                            │
                            ├──► Runtime :8081  (planner / executor, scheduler, goals, memory)
                            │        └──► LLM (OpenAI-compatible, default: Olares router)
                            └──► Browser broker :8082  (Playwright + Xvfb)
```

### Install

- **Olares Market**: search for *OMuse* (after the listing is approved).
- **Manual**: `python3 build.py omuse` → upload `dist/omuse-<version>.tgz` in Olares Market ▸ *Upload custom chart*.

The model endpoint defaults to `https://router.<your-olares-name>.olares.com/v1`; change it in the app's settings or the `PERSONA_MODEL_URL` / `PERSONA_MODEL` environment values. If the configured model isn't served, OMuse picks an available one.

### Development

```bash
pip install -r requirements.txt -r requirements-browser.txt
bash tests/run_local.sh            # fake LLM + all services on localhost
python3 -m pytest -q tests/test_units.py
python3 tests/integration.py        # and the other *_e2e.py suites
bash tests/stop_local.sh
```

---

## 中文

OMuse 是为 [Olares](https://www.olares.com) 打造的本地优先（local-first）AI 智能体（AI Agent）。它不只是聊天：会把你的需求拆成计划，在邮件、Notion、Slack 和网页之间把事情做完。凡是发送、发布、付款、删除这类操作，都会先等你批准。

### 功能

- **对话 → 计划 → 执行**：每一步、每次工具调用都看得见。
- **邮箱（支持多个）**：Gmail、Outlook / Hotmail（OAuth 2.0 授权）、Yahoo、iCloud、QQ 邮箱、网易 163 / 126、Zoho、AOL 及任何 IMAP / SMTP 邮箱；搜索、阅读、草稿、回复、发送、一键退订；验证码和重置链接对模型自动屏蔽。
- **Notion 与 Slack**：查资料、写报告、更新任务表、读取和发送消息。
- **浏览器**：独立 Chromium，受限 API，实时画面，登录 / 验证码（CAPTCHA）可由你接管。
- **MCP 连接器**：接入任意 MCP（Model Context Protocol，模型上下文协议）服务器，工具定义锁定防篡改。
- **自动化**：持续多天推进的场景目标、事件触发（新邮件 / Slack 新消息 / Notion 变化）、定时任务。
- **Telegram 遥控**：在手机上布置任务、一键审批。
- **记忆与技能**，**中文 / English 界面**。

### 安全设计

| 层 | 作用 |
|---|---|
| **Sentinel 哨兵** | 独立的权限服务，对每个操作做 允许 / 拒绝 / 询问（ALLOW / DENY / ASK）决策。 |
| **凭据保险箱（Vault）** | 应用专用密码和令牌（token）加密保存在 Sentinel，Agent 和模型都看不到。 |
| **防提示注入（Prompt Injection）** | 邮件、网页、工具结果一律视为不可信；发现可疑内容时，所有写操作升级为人工审批。 |
| **审计记录（Audit Trail）** | 只追加、哈希链（hash chain）防篡改的日志，记录每次模型调用、工具调用、决策和审批。 |

### 安装

- **Olares 应用商店**：审核通过后搜索 *OMuse*。
- **手动安装**：`python3 build.py omuse`，然后在 Olares 应用商店 ▸ *上传自定义 Chart* 中上传 `dist/omuse-<版本>.tgz`。

模型接口默认是 `https://router.<你的 Olares 名称>.olares.com/v1`，可在应用设置或环境变量 `PERSONA_MODEL_URL` / `PERSONA_MODEL` 中修改；如果配置的模型不存在，OMuse 会自动选择一个可用模型。

---

License: [MIT](LICENSE) © 2026 Lucas Lu
