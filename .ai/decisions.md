# Decisions - peptide_inventory_bot

Append-only log. Newest at bottom.

### 2026-07-11
- Decision: Initialized project memory for local AI system
- Why: Enable persistent context across Grok + Ollama tandem sessions

### 2026-07-11
- Decision: Implemented multi-tenant shop fields, /myorders + admin /orders, low-stock alerts, stock audit log; kept admin-confirm-before-deduct
- Why: Claude product plan items 1-4
- Tests: tests/test_stock_audit.py (8 passing)

### 2026-07-11
- Decision: Guided group setup (any TG group admin), payment templates CashApp/Venmo/Crypto/Zelle/Custom, my_chat_member onboarding, polished pay-confirm copy
- Why: Claude handoff for seller UX; confirm_order_payment untouched
- Tests: 18 passing (stock + templates + permissions + wizard DB)

### 2026-07-16
- Decision: Shop rename (admin) + full shop transfer to another group via one-time token (`/claim_transfer`); remap chat_id across all tables; keep old deep links via `shop_aliases`
- Why: Sellers change groups / rebrand without losing inventory and order history
- Tests: tests/test_shop_transfer_rename.py (11) — full suite 71 OK

### 2026-07-16
- Decision: Inventory bulk import via pipe layout text file (`name | price | stock | desc`); Telegram .txt upload; add-only skip-by-name
- Why: Faster catalog setup than one-by-one Add product
- Tests: tests/test_inventory_import.py

### 2026-08-09
- Decision: Overnight vendor mini-app cleanup — /invoices master command, VENDOR-ONBOARDING.md, fee seed default Unicorn=$1 when JSON omits order_fee
- Why: Ship brief so Render resume + vendor N+1 playbook are complete; integrate Claude fee seeding with prior invite rebinding
- Tests: full suite 237 pass on scratch DB_PATH; import run_cloud clean

### 2026-08-09
- Decision: FIX BRIEF #3 vendor order-path majors — (1) wrap web_app_data parse+normalize in try/except + coerce helper for non-finite; (2) Markdown-escape product/pay strings + try/except so owner notify always runs after create_order; (3) confirm_order_payment / confirm_payment_multi aggregate need_by_stock_id before deduct; (4) invoices count paid|shipped|complete, never downgrade open totals, week_offset + current+previous path; (5) log.warning on hidden_fee fallback; (6) hmac.compare_digest for NOTIFY_SECRET. Did not touch storefront_keys, fail-closed bind, or run_cloud payment-seeding.
- Why: Adversarial review: silent lost orders, post-commit Telegram 400, oversell via vials+kit lines, mid-week fee underbill, silent fee loss, timing-unsafe secret compare
- Tests: 269 pass on scratch DB_PATH (temp file); new tests for cart/md, oversell-by-duplicate-line, invoice status/no-downgrade/prev-week, compare_digest

### 2026-08-09
- Decision: Vendor web panel Orders section — confirm payment, set tracking + ship, date-range .txt export. Customer DMs via vendor_stores.get_bot_token_for_shop + telegram_send_with_token (never main SPBC bot). Cross-shop rejected by tok["chat_id"]. Reused db.confirm_order_payment / set_order_tracking / mark_order_shipped / list_orders. Did not touch storefront_keys, fail-closed bind, run_cloud payment seeding, store HTML, or vendor order-receiver logic beyond the new token helper.

### 2026-08-09
- Decision: Per-order "Add tracking" link in NEW ORDER vendor DMs. New `order_action_tokens` namespace (separate from web_tokens / vendor_invites / storefront_keys); mint_order_tracking_token hex(12) idempotent ~60d; standalone GET/POST `/track?ot=` (not admin panel). Token binds one order_id + shop_chat_id + action=track only. POST sets tracking, marks shipped if paid, customer DM via existing notify_order_customer. PANEL_BASE_URL required for link line; skip+log if unset. Did not touch storefront_keys, fail-closed bind, run_cloud payment seeding, or store HTML.
- Why: Vendor can tap tracking from the order DM without opening the full panel
- Tests: tests/test_order_tracking_link.py; full suite 312 pass on scratch DB_PATH

### 2026-08-09
- Decision: Mini-app ship address — parse optional `ship` from web_app_data into ship_name/address/notes (phone in notes as `Phone: … · via mini app`); customer confirm + NEW ORDER text show address (Markdown-escaped); missing address warns on NEW ORDER notify. Notify recipients = configured notify_ids ∪ db.list_admins(shop). Panel `_order_public` + history .txt expose ship fields. Did not touch storefront_keys, fail-closed bind, run_cloud payment seeding, or store HTML.
- Why: Store now sends ship object; vendor who claimed shop must get order DMs without manual notify_ids
- Tests: 298 pass on scratch DB_PATH; tests/test_vendor_stores_ship.py

### 2026-08-09
- Decision: Automatic weekly vendor billing — `service_fee_invoices.vendor_notified_at` (NULL = not DM'd); `franchise.bill_previous_complete_week` (week_offset=-1 only); `autobiller` daemon thread (~hourly, boot delay 45s) started from `run_cloud.main` with full try/except isolation; vendor DMs via main-bot `spbc_notify.send_telegram` to `db.list_admins`; stamp only after successful send; owner summary on notify wave; `MASTER_VENMO` config default `@remycastle` on vendor DM + `/invoices` + open-invoices view. Does not touch storefront_keys, fail-closed bind, payment-seeding, store HTML, or order-receiver order logic.
- Why: Platform fees for previous complete UTC week without manual /invoices every Monday; restart/catch-up safe via unique(shop,week) + vendor_notified_at
- Tests: 325 pass on scratch DB_PATH; tests/test_autobiller.py
- Why: Feature brief for magic-link panel order ops
- Tests: 282 pass on scratch DB; tests/test_panel_orders.py

### 2026-08-20
- Decision: Unicorn shop slices S4–S8 are additive only. `products.sku` / `variant_group` / `variant_label` via `_ensure_column` (NULL = standalone; `linked_product_id` stays shared stock). `GET /order-status?invite=&code=` is read-only and storefront_keys-scoped. `stock_reservations` is a new IF NOT EXISTS table: available-to-sell = stock − active holds; real stock still deducts only on admin confirm; janitor + lazy expire release rows. `orders.tg_payment_charge_id` + sendInvoice/pre_checkout/successful_payment handlers exist but stay off unless `TELEGRAM_PAYMENT_PROVIDER_TOKEN` is set (physical goods, never Stars). `shops.shipping_zones` JSON is optional; NULL keeps flat `shipping_fee` / `free_shipping_above`.
- Why: Feature plan S4–S8 without wiping or rewriting live inventory.
- Tests: tests/test_stock_reservations.py, tests/test_order_status.py, tests/test_tg_payments.py plus sku/variant storefront + search coverage

### 2026-08-20
- Decision: Keep Dockerfile explicit `COPY` of app `.py` modules (no `COPY .`). Added `reservation_janitor.py` and `tg_payments.py` after Unicorn S4–S8 so Render image boot can import `run_cloud` / `bot`. Still omit `*.db` / `.env` from the copy list.
- Why: 9a5eaa4 auto-deploys `update_failed`; live stayed on cb4a718. Image built, then container died on missing modules.
- Tests: Dockerfile COPY list vs run_cloud first-party import graph; no DB/migration work

### 2026-08-28
- Decision: Vendor stock how-to lives in `webpanel.PANEL_HTML` Catalog (same screen as Stock + Save), not README. Stock is stored as vials; kits of 10 are display/pricing only (`KIT_SIZE`). Live Unicorn Magic Factory catalog is served by `spbc-supplier-bot` `/storefront` (shop title `@unicornmagicfactory`). BAC 3ml is product id 69, stock in vials. Did not write live `/data/inventory.db` from this agent (no Render SSH).
- Why: Ghostie already opens Telegram `/webpanel` → `{PANEL_BASE_URL}/panel?t=…` Catalog. Remy asked for on-page shelf-count instructions and 90 vials of 3ml BAC.
- Tests: `tests/test_webpanel.py::HttpLayerTests.test_panel_html_has_stock_howto_on_catalog`

### 2026-08-28
- Decision: Cache-bust vendor Mini App `WebAppInfo` URLs with `?v=20260828` (`vendor_stores.cache_bust_store_url`) at both the Unicorn default and `_build_app` keyboard. Add `/webpanel` on the vendor storefront bot only (shop admins ∪ notify_ids ∪ owners) using `webpanel.issue_panel_link` / `revoke_tokens`. Did not rewrite POST `/order`, did not port Telegram catalog-admin buttons, kept `voffer_*` / `shand_*`.
- Why: Telegram WebView was serving cached Pages HTML; Ghostie needs a weblink on HER vendor bot, not SPBC admin buttons. Mini App no-initData fake-success is fixed on Pages, not here.
- Tests: `tests/test_vendor_webpanel.py`; `tests/test_webpanel.py` issue_panel_link cases

### 2026-08-30
- Decision: Stop quoting Unicorn / Ghostie for SPBC `/notify` fulfillment. `order_router.compute_quotes` / `quote_shop` skip shops whose title contains unicorn / ghostie / unicornmagicfactory (case-insensitive), plus `SKIP_VENDOR_SHOP_CHAT_IDS` and `UNICORN_SHOP_CHAT_ID`. `SKIP_UNICORN_ROUTING` defaults on. If she was the only complete-fill quote, return no vendor quotes so Remy fulfills. Did not delete Unicorn, change her catalog, wipe inventory, touch Patriotic Peptides, or add BAC auto-routing through her (no such auto-add exists in this repo).
- Why: Remy now fills springfieldpbc.com SMS-sourced peptides from Show Me Source stock. Unicorn stays a live shop for Ghostie's own customers.
- Tests: `tests/test_order_router.py` Unicorn/Ghostie skip + Patriotic still quoted; vendor-link/payables routing shops renamed off "Unicorn" so they still quote.
