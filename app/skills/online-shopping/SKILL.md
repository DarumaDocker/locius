---
name: online-shopping
description: 网购：搜索商品、用视觉挑选、加入购物车（不结账）Shop online — search products, choose (with vision), add to cart; never checks out without approval.
---
# Online shopping / 网购（搜索、挑选、加入购物车）

1. **Search with a direct URL** (fast, no typing):
   Amazon: `https://www.amazon.<sg|com|co.jp…>/s?k=<words+joined+by+plus>` · Lazada: `https://www.lazada.sg/catalog/?q=<words>` ·
   Shopee: `https://shopee.sg/search?keyword=<words>` · other shops: their search box (browser_type with submit=true).
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
7. **Login / CAPTCHA / robot check** → browser_request_takeover. Never type passwords or card numbers.
8. **Final answer**: product title, price, seller/rating, link, and "added to cart ✓ (not checked out)"; mention anything uncertain.

## Comparing and watching
- Showing several candidates: call present_choices(kind="comparison") with labels/prices copied exactly from the page
  (e.g. from browser_find context). Locius checks them against what you read; put your opinion in note.
- "Tell me when it's cheaper / back in stock": watch_create(url=product page, mode="price_below", threshold=…, keyword=
  product name) or mode="text", text="In stock". It notifies once per new drop; no need for a schedule.
