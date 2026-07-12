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
