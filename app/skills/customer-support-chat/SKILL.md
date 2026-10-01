---
name: customer-support-chat
description: 通过网页在线客服处理售后：退货退款、取消订阅、账单/费用、订单和物流问题 Handle customer service over web chat — returns & refunds, cancelling subscriptions, billing, order and delivery issues.
---
# Customer service via web chat / 在线客服

If phone calls are set up (the phone_call tool exists), calling is an option too — see the phone-call skill. Otherwise work through the company's website — self-service account pages, live chat, chat bots,
help-center forms — or by email. Try them in that order unless the user says otherwise.

## 1. Prepare the case (before contacting anyone)
- Collect the facts from Gmail: gmail_search e.g. `from:<company> (order OR receipt OR invoice OR subscription) newer_than:1y`
  and read the relevant emails: order / account number, dates, amounts, items, delivery status, earlier tickets.
- Write down the goal and what outcomes are acceptable, from the user's words (e.g. "full refund, store credit NOT ok";
  "cancel, not pause"; "lower the bill to ≤ S$50, otherwise cancel").
- A key fact is missing (which order? which outcome?) → ask the user one short question first.

## 2. Find the channel
- Search `https://duckduckgo.com/html/?q=<company>+contact+live+chat` (or `+cancel+subscription`, `+return+refund`).
- Self-service first: Account → Subscriptions / Orders → Return or Cancel is usually faster than chat.
- Login needed → browser_request_takeover("请登录 <site>"). Never type passwords or one-time codes.
- Chat widgets usually live in an iframe (refs look like [f1e3]) or a shadow root. Look for Chat / Live chat / Messaging /
  Contact us / Help / 在线客服.
- The chat bubble often has no ref or no name (an icon in the bottom-right corner). Don't search for it with browser_find
  over and over — use vision: browser_locate("the round chat bubble in the bottom-right corner") → browser_click_at(x, y).
  Same for the message box once the chat window is open: browser_locate("the message input box of the chat window") →
  browser_click_at(x, y, text="…", submit=true) sends the message (approval).

## 3. Chat
- First message: one short paragraph — name on the account, order/account number, what happened, the outcome wanted.
- Every message goes through Sentinel's approval dialog (the user can edit it, or choose "本任务 This task" to allow the
  rest of this chat). Just send it; don't ask in chat.
- After sending: browser_wait 15–45 s, then browser_snapshot (or browser_look("what did the chat reply?") if the chat
  window is not in the snapshot) to read the reply. Up to ~15 rounds.
  With bots, use their menu words ("Talk to an agent", "Cancel subscription", "Return an item").
- Be polite, factual and brief. Use only facts from the user or their emails — never invent order details.
- Retention offers ("50% off for 3 months", "pause instead"): don't accept or refuse on your own unless the user already
  said what they want — report the offer and ask.
- Anything that commits money or changes the account (accepting an offer, confirming a cancellation, choosing a refund
  method, agreeing to a fee) must match what the user asked for; the final confirm click goes through approval.
- Asked for passwords or OTP codes → browser_request_takeover so the user answers directly. Asked for an ID / membership / card number inside a web form → vault_list, then browser_fill_secret (the user approves it); never type such numbers into a chat box — take over instead.
- Chat closed or not offered → the help-center form, or an email to the support address (gmail_send, approval).

## 4. Common cases
- **Cancel subscriptions** — find what the user pays for: gmail_search
  `(subscription OR receipt OR renewal OR membership OR "your plan") newer_than:6m`; list service · price · cycle ·
  next renewal. Cancel only the ones the user picked; get the confirmation (page or email) and the date access ends.
  (Unwanted newsletters are different — use gmail_unsubscribe for those.)
- **Returns / refunds** — check the return window and conditions (order email, policy page); get the return label / RMA,
  drop-off or pickup instructions, refund amount and timing.
- **Bills / lowering a price** — note the current plan and price; look up the company's current public offers (web search)
  to mention; ask politely for a better rate; report any offer before accepting.
- **Delivery problems** — tracking number and status; ask for a resend or refund as the user prefers.

## 5. Finish
- Save a record: files_write `support/<company>-<YYYY-MM-DD>.md` with the key messages (short quotes), case/ticket
  number, agent name, promises and dates.
- A follow-up date (e.g. "refund in 5–7 days") → offer a goal that checks for it (goal_create), or create it if the user
  asked for follow-up.
- Final answer: the outcome, case/ticket number, what they promised and by when, what the user still needs to do.
