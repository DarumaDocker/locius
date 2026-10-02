---
name: web-research-compare
description: 网页调研并做对比（如酒店、产品）Research several options on the web and produce a comparison table.
---
# Web research & comparison / 调研对比

1. Decide the candidate list (from the user, or with one browser_search call).
2. Read the sources with browser_read — up to 4 URLs per call, in parallel (official pages, a review or comparison page).
   Use delegate(role="researcher", ...) only when each candidate needs several pages of digging; it is much slower.
3. Collect comparable fields (price with currency and date checked, rating & review count, distance/location, policies).
4. Produce a Markdown comparison table + a short recommendation that uses the user's known preferences from memory.
5. Make a file (make_pdf / make_docx / make_xlsx) only if the user asked for one; otherwise the table goes in the answer.

## Presenting the comparison
- For a pick-one decision, also call present_choices(kind="comparison") with exact excerpts (names, prices, specs) from the
  pages you read and their source_url. OMuse refuses details it can't find in those pages — copy, don't paraphrase.
