# SHIP BRIEF — Unicorn Mini App payments + checkout rails

Product: peptide (unicorn)  
Repo: `C:\Users\Remy\peptide_inventory_bot`  
Prod: https://unicornfartzz-bot.onrender.com  
Priority: P0  
Ship agent: grok  
AUTOPUSH: this ship (Mini App I've paid claim ping gets `/confirm` URL
button + buyer DM; no stock change)

Goal: After Mini App I've paid, vendor tap-to-confirm works on the vendor
bot; buyer gets a short claim DM; never wipe inventory.

Context (already verified — do not rediscover):

- Deep dive: [`GROK-DEEP-DIVE-UNICORN.md`](GROK-DEEP-DIVE-UNICORN.md)
- `POST /order` auth + HMAC: [`../UNICORN-INITDATA-RUNBOOK.md`](../UNICORN-INITDATA-RUNBOOK.md)
- Catalog vs Pages: [`../UNICORN-MINIAPP-PARITY.md`](../UNICORN-MINIAPP-PARITY.md)
- P0 seed/409/health live on `16c6547` (`payments.active` = 2)
- Checkout JSON follow-up live on `52a5e5a` (order-status `pay_url`, 401 split, `invoices.enabled`)
- Buyer `message` + paid-order rails live on `fef9326`
- Sold-out/min-order codes + empty-rails UX live on `c5bbf40`
- Laptop `inventory.db` is not live; Render `/data/inventory.db` is never opened from tests

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

## This ship

**Mini App payment-claim confirm URL.** I've paid vendor ping was plain
text (“Confirm in Admin → Orders”). Telegram-native claims already have
Confirm + tracking buttons on the *main* bot. The vendor poller does not
handle `admconfirm` callbacks, so Mini App claims now get:

- The same `/confirm?ct=` capability link as NEW ORDER (mint/reuse).
- An inline **Confirm payment** URL button (opens `/confirm`, no callback).
- Cancel-order text line when `PANEL_BASE_URL` is set.
- Fallback copy when `PANEL_BASE_URL` is unset.
- A short vendor-bot DM to the buyer. Second tap stays 200 idempotent
  and does not re-notify. Never confirms payment, never changes stock.
- `STORE_URL_CACHE_BUST` stays `20260913` (no Pages JS change).

## Acceptance (this ship)

- [x] `python -m pytest -q -x` green on scratch DBs (652 passed)
- [x] Docker COPY check still lists every imported module (26)
- [x] No writes to laptop `inventory.db`; no DELETE of products
- [x] `POST /order-paid` vendor ping includes `/confirm?ct=` + URL button
      when `PANEL_BASE_URL` is set
- [x] Unset `PANEL_BASE_URL` still pings; no confirm URL; no markup
- [x] Buyer DM on first claim; second tap does not re-notify
- [x] Stock unchanged after I've paid
- [ ] Live `/health` new `git_sha`, `payments.active` ≥ 1,
      `checkout_ready: true`, `store_url_cache_bust` `20260913`
      (after AUTOPUSH)
- [x] No secrets / `.env` / scratch import files committed

Prior `bef4d5f` checks stay true: `can_mark_paid`, 403 `not_your_order`,
Pages I've paid.

## Out of scope

- Native Telegram invoice (needs Remy provider token)
- Hard-delete / DB wipe / importer against live stock
- Telegram Stars
- PayPal.me for email handles (seeded PayPal is an email — copy/paste only)
- SPBC back-room; other vendor shops' default handles
- Force-push; committing untracked scratch

## Verify in 60s

1. GET https://unicornfartzz-bot.onrender.com/health → `ok: true`,
   `payments.active` ≥ 1, `payments.checkout_ready: true`,
   `store_url_cache_bust` `20260913`
2. Open https://remy-miniapp-demos.pages.dev/unicorn/ — receipt/lookup have
   I've paid; PayPal email is Copy, not a fake paypal.me link.
3. Place nothing. Do not run local `start.bat`. Do not wipe `/data`.
