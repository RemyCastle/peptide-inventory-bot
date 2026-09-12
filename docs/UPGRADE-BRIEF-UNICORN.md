# SHIP BRIEF — Unicorn Mini App claim cancel + confirm-success tracking

Product: peptide (unicorn)  
Repo: `C:\Users\Remy\peptide_inventory_bot`  
Prod: https://unicornfartzz-bot.onrender.com  
Priority: P1  
Ship agent: grok  
AUTOPUSH: this ship (Mini App PAYMENT CLAIM ping gets Cancel URL
button; /confirm success gets Add tracking CTA; no stock change on claim)

Goal: After Mini App I've paid, the vendor bot can cancel or (after
confirm) add tracking via URL buttons/pages; never wipe inventory.

Context (already verified — do not rediscover):

- Deep dive: [`GROK-DEEP-DIVE-UNICORN.md`](GROK-DEEP-DIVE-UNICORN.md)
- `POST /order` auth + HMAC: [`../UNICORN-INITDATA-RUNBOOK.md`](../UNICORN-INITDATA-RUNBOOK.md)
- Catalog vs Pages: [`../UNICORN-MINIAPP-PARITY.md`](../UNICORN-MINIAPP-PARITY.md)
- P0 seed/409/health live on `16c6547` (`payments.active` = 2)
- Checkout JSON follow-up live on `52a5e5a` (order-status `pay_url`, 401 split, `invoices.enabled`)
- Buyer `message` + paid-order rails live on `fef9326`
- Sold-out/min-order codes + empty-rails UX live on `c5bbf40`
- Laptop `inventory.db` is not live; Render `/data/inventory.db` is never opened from tests
- Confirm URL + buyer DM live on `6c3d68b` (`payments.active` = 2, cache bust `20260913`)

## Prior ships

**P0 + top P1 — live on `16c6547`:** boot seed, admin seed, `POST /order` 409
`no_payment_methods`, buyer `payment_methods`, PayPal/Apple Cash quick-add,
`/health` payment counts, 401 `detail`, honest empty copy.

**Checkout JSON follow-up — live on `52a5e5a`:** `GET /order-status` pay rails,
401/409 `message` on initData + empty methods, 401 `error` split
(`empty_initdata` / `bad_payload` / `bad_hash` / `expired`),
`/health` `invoices.enabled`.

**Buyer `message` + paid-order rails — live on `fef9326`:** every `POST /order`
error has `message`; `GET /order-status` 404 has `message`; `needs_payment`
plus null `pay_url` after paid.

**Buyer copy + `pay_hint` — live on `2054b95`:** `/storefront` `message` +
`invoices_enabled`; `/order-status` status `message`; rails `pay_hint`
(no handles on catalog; pay_hint/pay_url hidden after paid).

**Stable sold-out/min-order codes + empty-rails UX — live on `c5bbf40`:**
`POST /order` `error: sold_out` / `min_order` + buyer `message`; success
`needs_payment: true`. Cancelled/rejected `GET /order-status` keep method
names, null `pay_url`, `needs_payment: false`; awaiting confirmation still
has `pay_url`. `/storefront` `checkout_ready` (names+types only). Telegram
warns when all methods are paused; Unicorn seed CTA shows when Venmo or
PayPal types are missing (web panel same rule).

**Pages checkout JS + cache bust — live on `fe857ec`:** Mini App uses
`pay_url` / `pay_hint` / `checkout_message`; submit disabled when
`checkout_ready` is false; mockup chrome gone. `STORE_URL_CACHE_BUST`
was `20260912`.

**Mini App I've paid + admin rails preview — live on `bef4d5f`:**
`POST /order-paid` pending → awaiting (no stock change); `can_mark_paid`;
health `checkout_ready` + cache bust `20260913`; Pages I've paid + Copy
target. Live `/health` sha later `5951281` / `592eb3b`.

**Mini App payment-claim confirm URL — live on `6c3d68b`:** vendor ping
includes `/confirm?ct=` URL button + cancel text line; buyer DM on first
claim; second tap 200 idempotent; no stock change.

## This ship

**Mini App claim cancel URL + confirm-success tracking.** Vendor bot still
has no cancel/track callbacks. After I've paid:

- PAYMENT CLAIM markup is two URL buttons: **Confirm payment** and
  **Cancel order** (same `/confirm?ct=` / `/cancel?xt=` links as the text
  lines). Unset `PANEL_BASE_URL` still pings with fallback copy and no
  markup.
- `/confirm` success (and already-confirmed GET/POST) includes an
  **Add tracking** CTA (`/track?ot=`) when `PANEL_BASE_URL` is set.
  Pending confirm form does not. JSON `track_url` matches.
- Claim still never confirms payment and never changes stock. Confirm
  still deducts once. `STORE_URL_CACHE_BUST` stays `20260913`.

## Acceptance (this ship)

- [x] `python -m pytest -q -x` green on scratch DBs (654 passed)
- [x] Docker COPY check still lists every imported module (26)
- [x] No writes to laptop `inventory.db`; no DELETE of products
- [x] `POST /order-paid` vendor ping includes Confirm + Cancel URL buttons
      when `PANEL_BASE_URL` is set
- [x] Unset `PANEL_BASE_URL` still pings; no confirm/cancel URL; no markup
- [x] `/confirm` success HTML + JSON include `/track?ot=` when panel URL set
- [x] Pending `/confirm` GET has no tracking CTA
- [x] Stock unchanged after I've paid; confirm still decrements once
- [ ] Live `/health` new `git_sha`, `payments.active` ≥ 1,
      `checkout_ready: true`, `store_url_cache_bust` `20260913`
      (after AUTOPUSH)
- [x] No secrets / `.env` / scratch import files committed

Prior `6c3d68b` / `bef4d5f` checks stay true: `can_mark_paid`, 403
`not_your_order`, Pages I've paid, buyer claim DM.

## Out of scope

- Native Telegram invoice (needs Remy provider token)
- Hard-delete / DB wipe / importer against live stock
- Telegram Stars
- PayPal.me for email handles (seeded PayPal is an email — copy/paste only)
- SPBC back-room; other vendor shops' default handles
- Force-push; committing untracked scratch
- Pages JS (no cache-bust bump)

## Verify in 60s

1. GET https://unicornfartzz-bot.onrender.com/health → `ok: true`,
   `payments.active` ≥ 1, `payments.checkout_ready: true`,
   `store_url_cache_bust` `20260913`
2. Open https://remy-miniapp-demos.pages.dev/unicorn/ — receipt/lookup have
   I've paid; PayPal email is Copy, not a fake paypal.me link.
3. Place nothing. Do not run local `start.bat`. Do not wipe `/data`.
