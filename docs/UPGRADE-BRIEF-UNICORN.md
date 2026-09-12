# SHIP BRIEF — Unicorn Mini App payments + checkout rails

Product: peptide (unicorn)  
Repo: `C:\Users\Remy\peptide_inventory_bot`  
Prod: https://unicornfartzz-bot.onrender.com  
Priority: P0  
Ship agent: grok  
AUTOPUSH: this ship (Pages checkout JS + `STORE_URL_CACHE_BUST=20260912`)

Goal: Mini App uses structured `pay_url` / `pay_hint` / `message` (no mockup
copy, no regex-only pay buttons); never wipe inventory.

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

## This ship

**Pages checkout JS + cache bust.** Mini App (`miniapp-demos/unicorn/index.html`,
Cloudflare Pages, not this git remote) now:

- Uses `payment_methods[].pay_url` / `pay_hint` (string `payments` regex is
  fallback only)
- Surfaces storefront `checkout_ready` + `message` (submit disabled when empty)
- Order lookup shows status `message` and pay rails only while `needs_payment`
- Checkout errors use buyer `message` (`sold_out` / `min_order` / `no_payment_methods`)
- Drops mockup chrome (“sample data”, “make-believe”, fake 12s-ago livebar)
- SKU on cards already rendered when `/storefront` sends `sku`

This repo: `POST /order` adds short `checkout_message`; `STORE_URL_CACHE_BUST`
`20260912` so Telegram refetches Pages HTML.

**Buyer copy + `pay_hint` — live on `2054b95`:** `/storefront` `message` +
`invoices_enabled`; `/order-status` status `message` and null `pay_hint`
after paid/cancelled/rejected; `POST /order` rails include `pay_hint`
(PayPal email = copy Friends & Family; no handle inside the hint).

## Acceptance (this ship — `fe857ec`, Pages `9c914f77`)

- [x] `python -m pytest -q -x` green on scratch DBs (611 passed)
- [x] Docker COPY check still lists every imported module (26)
- [x] No writes to laptop `inventory.db`; no DELETE of products
- [x] `POST /order` success includes short `checkout_message` (not the full DM)
- [x] Mini App HTML: no “sample data” / “make-believe”; uses `pay_url` / `pay_hint`
- [x] Mini App disables submit when `checkout_ready` is false
- [x] Live Pages `/unicorn/` no longer shows mockup chrome
- [x] Live `/health` `git_sha` `fe857ec…`, `payments.active` = 2; store URL `?v=20260912`
- [x] No secrets / `.env` / scratch import files committed

Prior 2054b95 checks stay true: storefront names-only, paid-order null `pay_url`.

## Out of scope

- Native Telegram invoice (needs Remy provider token)
- Hard-delete / DB wipe / importer against live stock
- Telegram Stars
- PayPal.me for email handles (seeded PayPal is an email — copy/paste only)
- SPBC back-room; other vendor shops' default handles
- Force-push; committing untracked scratch

## Verify in 60s

1. GET https://unicornfartzz-bot.onrender.com/health → `ok: true`, `payments.active` ≥ 1
2. Open https://remy-miniapp-demos.pages.dev/unicorn/ — no “sample data” / “make-believe”;
   livebar says live catalog; checkout note names Venmo / PayPal.
3. Place nothing. Do not run local `start.bat`. Do not wipe `/data`.
