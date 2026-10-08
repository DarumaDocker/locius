---
name: online-shopping
description: 网购：搜索商品、用视觉挑选、加入购物车（不结账）Shop online — search products, choose (with vision), add to cart; never checks out without approval.
---
# Online shopping / 网购（搜索、挑选、加入购物车）

1. **Search with a direct URL** (fast, no typing):
   Amazon: `https://www.amazon.<sg|com|co.jp…>/s?k=<words+joined+by+plus>` · Lazada: `https://www.lazada.sg/catalog/?q=<words>` ·
   Shopee: `https://shopee.sg/search?keyword=<words>` · other shops: their search box (browser_type with submit=true).
   FairPrice: `https://www.fairprice.com.sg/search?query=<words>` · Lazada / Shopee may show a robot check → takeover.
   **A shopping list of several items** (2026-10-04 V3-03: 12 items were searched three times over and nothing was written
   down): read 3–4 search URLs per browser_read call, and as soon as you have an item's best match and price, write it down
   with update_plan(note=…) or files_write — older results are compressed out of your context. Never search an item again
   that is already in your notes; when the list is done, answer from the notes.
   **Match the list exactly** (2026-10-05 S3-06: "Meiji milk 2L" was priced with a smaller pack, "Nescafe Gold 200g" at
   S$2.49, and qty 2 became 1): the product you pick must have the listed size / weight / pack count; note its title,
   size and price as shown on the page; multiply by the qty column; totals with calculate. No matching size → say so.
   **A shop's own search finds nothing, or the shop blocks you** (Cloudflare "You have been blocked", robot check): do not
   conclude the product is not sold there. Search the web once — browser_search("<product> site:<shop domain>", e.g.
   "Dyson V15 Detect site:courts.com.sg") — and browser_read the product page it returns. Still nothing → say "not
   verified at <shop>" (not "not sold"). Only use shop domains you have seen in search results; never guess one.
   **"The cheapest X"**: sorting by lowest price (Amazon `&s=price-asc-rank`) lists accessories first (cases, covers,
   cables "for Anker …"). Keep only results whose title is the product itself (brand + product type + spec, e.g. "Anker
   Power Bank 20000mAh"), using browser_find("<brand> <type> <spec>") to get those links with prices; if a sorted page
   shows only accessories, drop the sort and pick the cheapest matching item from the normal results instead of
   re-filtering the same URL.
2. **See the results.** Shop pages are long; the text snapshot is cut off. Do NOT re-open the same URL.
   - browser_look("List the products on screen with title, price, rating and the label of each product link") — the vision model
     sees the real page, including images.
   - browser_find("<product words>") finds product links anywhere on the page with their price in `context`.
   - browser_scroll("down") then browser_look again for more results.
3. **Choose.** Match the user's wishes (e.g. "looks nice" → ask browser_look which ones look most attractive and why; check it fits
   the exact model, e.g. iPhone 17 Pro Max, not 17 Pro). Prefer good ratings with many reviews; skip "Sponsored" unless it fits best.
4. **Open the product page** (click its title link), then find the button: browser_find("Add to Cart") (Amazon also uses
   "Add to Basket"); if there are options (colour/size), select them first — browser_look("which colour/size options are shown?").
5. **Add to cart** by clicking the button's ref. Adding to the cart is not a purchase and doesn't need approval.
   Anything that pays or places an order (Buy Now, Checkout, Place order) goes through Sentinel's approval — only do it if
   the user explicitly asked to buy.
6. **Verify**: browser_look("Was the item added to the cart? What does the confirmation say and how many items are in the cart?")
   or check the cart count in the snapshot.
7. **Login / CAPTCHA / robot check** → first finish everything that needs no login (look up every item on the list,
   prices, totals, budget checks — product pages are public), then browser_request_takeover for the cart / checkout step
   and say what is already done. Do not stop the whole task at the first "Log in" button.
   Never type passwords. Shipping name / phone / address: profile_get. Card details at checkout: vault_list + browser_fill_secret (each fill approved by the user), otherwise take over; the final Pay / Place order click is approved separately.
8. **Buying (the user asked you to actually order)**: delivery to the user's home unless they say otherwise.
   - Shipping details: profile_get (name_en, phone, address_home). Before typing a phone number, check memory for a format
     this site accepted before (memory_search "<site> phone"). If the field rejects it, read the hint and retry in another
     common format (Singapore: "88478582", "8847 8582", "+65 88478582"; a separate country-code box → "+65" there and 8 digits
     in the number box). Once a format is accepted, memory_remember "<site> accepts the phone number as …".
   - Go through checkout up to the page that shows the final total (items + shipping + tax) and the delivery address.
     "Proceed to checkout", "Next step" and "Continue to payment" only move between pages and need no approval; do not
     stop to ask the user about them.
   - Then call **purchase_confirm ONCE**: site, items (name, qty, price as shown), shipping, total, currency, delivery
     ("Home delivery to <address>, <ETA>"), card_item_id (vault_list → the card the user keeps for shopping). The user
     approves this one card; after that, the card fills (browser_fill_secret, one call per field) and the Place order / Pay
     click on that site go through without more approvals for 30 minutes, as long as the page total stays within it.
   - If the total, the item, the card or the site changes, call purchase_confirm again. A card form inside a payment
     iframe: snapshot shows its fields with refs like f1e3 — fill those. Payment pages (Adyen, Stripe) put EACH card box in
     its own iframe: the number in one (e.g. f9e1), the expiry in the next (f10e1), the CVC in another (f11e1). Fill each
     detail into its own box — never two details into one box; if a box is missing, browser_snapshot again or
     browser_find("Expiry date") before asking the user to take over.
   - After the order: read the confirmation page and report the order number, items, total and delivery date.
9. **Final answer**: product title, price, seller/rating, link, and "added to cart ✓ (not checked out)"; mention anything uncertain.

## Comparing and watching
- Showing several candidates: call present_choices(kind="comparison") with labels/prices copied exactly from the page
  (e.g. from browser_find context). OMuse checks them against what you read; put your opinion in note.
- "Tell me when it's cheaper / back in stock": watch_create(url=product page, mode="price_below", threshold=…, keyword=
  a word from the product's own title, current_price=the price you saw) or mode="text", text="In stock". It notifies once
  per new drop; no need for a schedule. The watch reads the page itself and says what it read — report THAT value to the
  user; if it refuses (it read a different price), fix the keyword or say the page can't be watched reliably.

## After the order / cancelling or returning
- The order lands in the ledger (Trust page → Ledger) with the order number from the confirmation page — always report
  that number. To cancel or return later, orders_list first and act on that exact order; the approval card shows which
  ledger order the cancel is for.
