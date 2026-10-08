---
name: order-tracking
description: 定期检查订单/网页状态变化并通知 Monitor an order or page on a schedule and notify when it changes.
---
# Order / page monitoring / 订单监控

Orders OMuse placed itself are in the ledger: call **orders_list** first ("那个订单", "my socks order", cancel, return,
where is it). Use exactly that merchant and order number — never pick an order from another shop's emails. The ledger is
also updated from the merchants' emails every few hours (shipped / delivered / refunded).

Setup (when the user asks "每天检查…如果…告诉我"):
1. Confirm the URL or how to find the order (email search or site). Do one check now to see the current state.
2. Call schedule_create with a clear goal that includes the URL and the condition, e.g.
   "检查 https://… 的订单状态；如果状态变成已发货(shipped)就通知我，并附上物流单号" and a cron like `0 9 * * *`.
3. Store the current state with schedule_state_set only inside scheduled runs.

Scheduled run:
1. Read the previous state (given in the task context or via schedule_state_get).
2. Check the page/email again. Extract the status in a normalized form (e.g. "processing", "shipped: <tracking no>").
3. If the status changed in the way the user cares about → notify_user with the details. Always schedule_state_set the new status.
4. If login is needed, notify_user asking them to log in via the Browser tab (takeover), don't loop.
