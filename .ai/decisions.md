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
