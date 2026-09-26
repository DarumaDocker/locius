---
name: web-research-compare
description: 网页调研并做对比（如酒店、产品）Research several options on the web and produce a comparison table.
---
# Web research & comparison / 调研对比

1. Decide the candidate list (from the user, or by searching https://duckduckgo.com/html/?q=<query>).
2. For 3+ candidates, use delegate(role="researcher", task="Research <candidate>: price, location, rating, key pros/cons, source URLs")
   once per candidate so each gets focused attention; or browse them yourself for 1–2 candidates.
3. Collect comparable fields (price with currency and date checked, rating & review count, distance/location, policies).
4. Produce a Markdown comparison table + a short recommendation that uses the user's known preferences from memory.
5. Save the report to the workspace with files_write (e.g. reports/<topic>-<date>.md) and mention the path.
