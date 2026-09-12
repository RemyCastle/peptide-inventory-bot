# SHIP BRIEF — Unicorn Mini App payments + checkout rails

Product: peptide (unicorn)  
Repo: `C:\Users\Remy\peptide_inventory_bot`  
Prod: https://unicornfartzz-bot.onrender.com  
Priority: P0  
Ship agent: grok  
AUTOPUSH: yes (`deploy-map.json` autopush=true, autoCommit=false)

Goal: Buyers can always see at least one way to pay after Mini App checkout;
owners have a seed/admin path when rails are empty; never wipe inventory.

Context (already verified — do not rediscover):

- Deep dive: [`GROK-DEEP-DIVE-UNICORN.md`](GROK-DEEP-DIVE-UNICORN.md)
- `POST /order` auth + HMAC: [`../UNICORN-INITDATA-RUNBOOK.md`](../UNICORN-INITDATA-RUNBOOK.md)
- Catalog vs Pages: [`../UNICORN-MINIAPP-PARITY.md`](../UNICORN-MINIAPP-PARITY.md)
- P0 seed/409/health live on `16c6547` (`payments.active` = 2)
- Checkout JSON follow-up live on `52a5e5a` (order-status `pay_url`, 401 split, `invoices.enabled`)
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

## This ship (stable sold-out/min-order codes + empty-rails UX)

1. **`POST /order` stock fail** uses short `error: sold_out` (not the long
   sentence). Below-minimum carts use `error: min_order` + buyer `message`
   instead of looking sold-out. Success JSON includes `needs_payment: true`.
2. **`GET /order-status`** cancelled / rejected keep method names, null
   `pay_url`, `needs_payment: false`. Awaiting confirmation still has `pay_url`.
3. Public `/storefront` adds `checkout_ready` (true iff ≥1 active method).
   Names + types only — no handles, no pay URLs. Unknown key 404 has `message`.
4. Admin empty-rails: Telegram warns when **all methods are paused** (not only
   when the list is empty). Unicorn seed button shows when Venmo or PayPal
   types are missing. Web panel seed uses the same rule.

## Acceptance

- [ ] `python -m pytest -q -x` green on scratch DBs
- [ ] Docker COPY check still lists every imported module
- [ ] No writes to laptop `inventory.db`; no DELETE of products
- [ ] Pending `GET /order-status` includes `needs_payment: true` and Venmo `pay_url`
- [ ] Paid / cancelled / rejected `GET /order-status` include `needs_payment: false`
      and null `pay_url`
- [ ] Empty `initData` → 401 `empty_initdata` + `message`; zero new orders
- [ ] Zero active methods → 409 `no_payment_methods` + `message`
- [ ] Sold-out uses `error: sold_out` + `message`; min-order uses `error: min_order`
- [ ] Empty cart / no vendor token / unknown storefront all have `message`
- [ ] `/storefront` `checkout_ready` is boolean; no handles / pay URLs
- [ ] Live `/health` `ok: true`, new `git_sha`, `payments.active` ≥ 1,
      `invoices.enabled` is a boolean
- [ ] No secrets / `.env` / scratch import files committed

## Out of scope

- Cloudflare Pages HTML (SKU on cards, mockup copy, using `pay_url` client-side)
- Hard-delete / DB wipe / importer against live stock
- Telegram Stars; enabling invoices (needs Remy provider token)
- PayPal.me for email handles (seeded PayPal is an email — copy/paste only)
- SPBC back-room; other vendor shops' default handles
- Force-push; committing untracked scratch

## Verify in 60s

1. GET https://unicornfartzz-bot.onrender.com/health → `ok: true`, new `git_sha`,
   `payments.active` ≥ 1, `invoices.enabled` present (false until Remy sets a
   provider token).
2. GET `/storefront?invite=` (Pages key) → `payments` non-empty names (no handles).
3. Place nothing. Do not run local `start.bat`. Do not wipe `/data`.
