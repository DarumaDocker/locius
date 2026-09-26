---
name: reply-email
description: 回复某人的邮件 Find the right thread and reply to someone (draft → approval → send).
---
# Reply to an email / 回复邮件

1. Identify the person and topic from the request. Search: `from:<name or address> newer_than:30d` (add subject words if given).
   If several threads match, pick the most recent one that is waiting for the user's answer; if still ambiguous, ask the user.
2. Read the latest message in the thread with gmail_get_message (or gmail_get_thread for context).
3. Write the reply in the language and tone of the thread; keep the user's exact commitments (dates, times, numbers)
   exactly as the user stated them. Sign with the user's first name.
4. Call gmail_reply (message_id = latest message, body = your text). Sentinel will show the approval dialog with an editable body;
   do not ask for permission in chat.
5. After the result: if sent, confirm who received it and the subject. If denied, say so and offer to save as a draft instead.
