---
name: customer-support-chat
description: 在网站上联系客服（网页聊天）Contact a company's customer support via its website chat and get an answer.
---
# Contact customer support via web chat / 网页客服

1. Open the company's help/contact page (search `<company> contact support chat` on https://duckduckgo.com/html/?q=... if the URL is unknown).
2. If a login is required, call browser_request_takeover("请登录 <site>") and wait. Never type passwords.
3. Open the chat widget (look for buttons like Chat / Live chat / Contact us / Help / 在线客服). Widgets are often inside an iframe;
   refs from iframes look like [f1e3].
4. Write short, polite messages that state the user's case (order number, dates) exactly as the user gave them.
   Each message you send may require approval from the user; that is expected.
5. After sending, use browser_wait (20–60 s) and browser_snapshot to read replies. Repeat up to ~10 rounds.
   If the agent asks for identity verification, card numbers or passwords → request takeover.
6. Never agree to refunds, cancellations, account changes or payments the user didn't explicitly ask for.
7. Final answer: what support said (quote key sentences), case/ticket number, promised dates, next steps.
