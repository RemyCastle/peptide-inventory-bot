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
- Decision: Mini-app ship address — parse optional `ship` from web_app_data into ship_name/address/notes (phone in notes as `Phone: … · via mini app`); customer confirm + NEW ORDER text show address (Markdown-escaped); missing address warns on NEW ORDER notify. Notify recipients = configured notify_ids ∪ db.list_admins(shop). Panel `_order_public` + history .txt expose ship fields. Did not touch storefront_keys, fail-closed bind, run_cloud payment seeding, or store HTML.
- Why: Store now sends ship object; vendor who claimed shop must get order DMs without manual notify_ids
- Tests: 298 pass on scratch DB_PATH; tests/test_vendor_stores_ship.py
- Why: Feature brief for magic-link panel order ops
- Tests: 282 pass on scratch DB; tests/test_panel_orders.py
