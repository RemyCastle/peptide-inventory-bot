# SHIP BRIEF — Unicorn Mini App payments + checkout rails

Product: peptide (unicorn)  
Repo: `C:\Users\Remy\peptide_inventory_bot`  
Prod: https://unicornfartzz-bot.onrender.com  
Priority: P0  
Ship agent: grok  
AUTOPUSH: live on `2054b95` — do not re-push / do not restart Render

Goal: Buyers always get a `message` on catalog and order lookup, and each
checkout rail has a `pay_hint` (copy-paste vs tap-to-pay); never wipe inventory.

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

**None in this repo.** `2054b95` is on `origin/master` and live
(`/health` `git_sha` `2054b95…`, `payments.active` = 2, storefront
`checkout_ready: true`, `message` present, `invoices_enabled: false`,
PayPal+Venmo names only). Do not re-dispatch this brief. Next work is Pages
(other git remote: use `pay_url` / `pay_hint` / `message`, drop mockup copy)
or Remy invoice token.

**Buyer copy + `pay_hint` — live on `2054b95`:** `/storefront` `message` +
`invoices_enabled`; `/order-status` status `message` and null `pay_hint`
after paid/cancelled/rejected; `POST /order` rails include `pay_hint`
(PayPal email = copy Friends & Family; no handle inside the hint).

## Acceptance (2054b95 — verified 2026-09-12)

- [x] `python -m pytest -q -x` green on scratch DBs (591 passed)
- [x] Docker COPY check still lists every imported module (26)
- [x] No writes to laptop `inventory.db`; no DELETE of products
- [x] `/storefront` `checkout_ready: true` → `message` mentions pay after checkout;
      paused methods → `message` tells the buyer to message the seller
- [x] `/storefront` has `invoices_enabled: false`; no handles / pay URLs / pay_hint
- [x] Pending `GET /order-status` has `message` + Venmo `pay_url` + `pay_hint`
- [x] Paid / cancelled / rejected `GET /order-status` have `needs_payment: false`
      and null `pay_url` / `pay_hint`
- [x] PayPal email `pay_hint` says copy + Friends & Family (no proton address)
- [x] Live `/health` `ok: true`, `git_sha` `2054b95…`, `payments.active` = 2
- [x] No secrets / `.env` / scratch import files committed

## Out of scope

- Cloudflare Pages HTML (SKU on cards, mockup copy, using `pay_url` client-side)
- Hard-delete / DB wipe / importer against live stock
- Telegram Stars; enabling invoices (needs Remy provider token)
- PayPal.me for email handles (seeded PayPal is an email — copy/paste only)
- SPBC back-room; other vendor shops' default handles
- Force-push; committing untracked scratch

## Verify in 60s

1. GET https://unicornfartzz-bot.onrender.com/health → `ok: true`, `git_sha`
   starts with `2054b95`, `payments.active` ≥ 1, `invoices.enabled` present
   (false until Remy sets a provider token).
2. GET `/storefront?invite=` (Pages key) → `checkout_ready: true`, `message`
   present, `invoices_enabled: false`, `payments` names only (no handles /
   pay URLs / pay_hint).
3. Place nothing. Do not run local `start.bat`. Do not wipe `/data`.
