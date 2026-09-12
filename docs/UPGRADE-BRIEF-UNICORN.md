# SHIP BRIEF — MEGA-UNICORN v2 leftover receipt/report/claim names

Product: peptide (unicorn)  
Repo: `C:\Users\Remy\peptide_inventory_bot`  
Prod: https://unicornfartzz-bot.onrender.com  
Priority: P1  
Ship agent: grok  
AUTOPUSH: this ship (leftover customer TEXT receipt, sale admin
report, payment-claim notify, payment-confirmed DM, saved-address
preview; no stock change; never wipe inventory.db)

Goal: Leftover customer TEXT receipts, admin sale reports, Mini App
I've-paid pings, payment-confirmed DMs, and saved-address previews
strip dirty names the same way HTML receipts and NEW ORDER notify
already do; empty pay copy still does not promise a DM; never wipe
inventory.

Context (already verified — do not rediscover):

- Deep dive: [`GROK-DEEP-DIVE-UNICORN.md`](GROK-DEEP-DIVE-UNICORN.md)
- `POST /order` auth + HMAC: [`../UNICORN-INITDATA-RUNBOOK.md`](../UNICORN-INITDATA-RUNBOOK.md)
- Catalog vs Pages: [`../UNICORN-MINIAPP-PARITY.md`](../UNICORN-MINIAPP-PARITY.md)
- P0 seed/409/health live on `16c6547` (`payments.active` = 2)
- Empty-handle rails live on `8829dfb` (`payments.usable` = 2)
- PayPal/Apple Cash quick-add + refuse empty typed saves live on `3663272`
- Empty-rail copy + URL ZWJ/NBSP live on `1555d79`
- Panel pay-target copy + leftover NEW ORDER/low-stock names live on `b6eac25`
- Mini App DM fallback live on `984ea01`
- Laptop `inventory.db` is not live; Render `/data/inventory.db` is never opened from tests
- This ship: TEXT receipt item/ship/pay names; sale admin report;
  payment-claim buyer/code; payment-confirmed DM; last-ship preview

## Prior ships

**P0 + top P1 — live on `16c6547`:** boot seed, admin seed, `POST /order` 409
`no_payment_methods`, buyer `payment_methods`, PayPal/Apple Cash quick-add,
`/health` payment counts, 401 `detail`, honest empty copy.

**Empty-rail copy + leftover URL/report — live on `1555d79`:** crypto /
Cash App / Apple Cash empty rails name wallet/cashtag/phone;
`public_http_url` rejects `%E2%80%8D` / `%C2%A0`; pending-orders report
sanitized.

**Panel pay-target copy + leftover notify — live on `b6eac25`:**
`PAYMENT_TARGET_COPY` is the source of truth; Zelle email or phone;
crypto missing network warns but stays usable; NEW ORDER notify +
low-stock names sanitized.

**Mini App DM fallback — live on `984ea01`:** vendor token, then
`TELEGRAM_BOT_TOKEN`, then poller (Unicorn included).

## This ship

**Leftover receipt/report/claim/confirm names.** Payments P1 is live
(Venmo+PayPal usable). HTML customer receipts and NEW ORDER notify
already clean names. Leftover: Telegram TEXT `Order received` still
echoed dirty item/ship/pay strings; sale admin report and payment-
confirmed DM echoed dirty ship/tracking; payment-claim ping echoed
dirty buyer/code; saved-address preview could offer junk-only rows.

- `build_customer_order_received_text` sanitizes item names, ship,
  payment names/instructions (same helpers as HTML).
- `format_customer_ship_block` sanitizes before showing.
- `build_payment_claim_notify_text` sanitizes buyer / username / code.
- `format_sale_admin_report` sanitizes shop/buyer/item/ship/pay/track.
- `format_payment_confirmed_customer` is the confirm DM (bot uses it).
- `last_ship_details` sanitizes on read and returns None for junk-only.
- Empty TEXT receipt still uses `NO_PAYMENTS_BUYER_LINE` (no "DM'd").
- `STORE_URL_CACHE_BUST` stays `20260913`. No Pages JS. No DELETE.

## Acceptance (this ship)

- [x] `python -m pytest -q -x` green on scratch DBs
- [x] Docker COPY check still lists every imported module
- [x] No writes to laptop `inventory.db`; no DELETE of products
- [x] Dirty TEXT receipt item/ship/pay names come out clean
- [x] Dirty sale report + payment-confirmed DM come out clean
- [x] Dirty payment-claim buyer/code come out clean
- [x] Junk-only saved address is not offered
- [x] Empty TEXT receipt still does not promise a DM
- [x] No secrets / `.env` / scratch import files committed

Prior `b6eac25` / `1555d79` / `3663272` / `8829dfb` / `984ea01` checks stay true:
PayPal/Apple Cash quick-add, usable rails only, Confirm + Cancel URL
buttons, type-specific empty-rail copy, URL ZWJ/NBSP fail closed,
panel cashtag/wallet/Zelle email-or-phone labels, Unicorn DM fallback.

## Out of scope

- Native Telegram invoice (needs Remy provider token)
- Hard-delete / DB wipe / importer against live stock
- Telegram Stars
- PayPal.me for email handles (seeded PayPal is an email — copy/paste only)
- Requiring network_note for crypto checkout_ready
- SPBC back-room; other vendor shops' default handles
- Force-push; committing untracked scratch
- Pages JS (no cache-bust bump)

## Verify in 60s

1. GET https://unicornfartzz-bot.onrender.com/health → `ok: true`,
   `payments.active` ≥ 1, `payments.usable` ≥ 1,
   `payments.checkout_ready: true`, `store_url_cache_bust` `20260913`
2. Open https://remy-miniapp-demos.pages.dev/unicorn/ — checkout still
   enabled (Venmo + PayPal have handles); PayPal email is Copy, not a
   fake paypal.me link.
3. Place nothing. Do not run local `start.bat`. Do not wipe `/data`.
