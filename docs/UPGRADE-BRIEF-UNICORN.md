# SHIP BRIEF — Unicorn empty-handle rails are not checkout-ready

Product: peptide (unicorn)  
Repo: `C:\Users\Remy\peptide_inventory_bot`  
Prod: https://unicornfartzz-bot.onrender.com  
Priority: P1  
Ship agent: grok  
AUTOPUSH: this ship (typed payment rows with no handle no longer
count as checkout_ready; POST /order 409; admin/panel warn; no stock change)

Goal: An enabled Venmo/PayPal/Cash App row with no handle is empty UX —
buyers cannot checkout on it; never wipe inventory.

Context (already verified — do not rediscover):

- Deep dive: [`GROK-DEEP-DIVE-UNICORN.md`](GROK-DEEP-DIVE-UNICORN.md)
- `POST /order` auth + HMAC: [`../UNICORN-INITDATA-RUNBOOK.md`](../UNICORN-INITDATA-RUNBOOK.md)
- Catalog vs Pages: [`../UNICORN-MINIAPP-PARITY.md`](../UNICORN-MINIAPP-PARITY.md)
- P0 seed/409/health live on `16c6547` (`payments.active` = 2)
- Checkout JSON follow-up live on `52a5e5a`
- Buyer `message` + paid-order rails live on `fef9326`
- Sold-out/min-order codes + empty-rails UX live on `c5bbf40`
- Laptop `inventory.db` is not live; Render `/data/inventory.db` is never opened from tests
- Confirm URL + buyer DM live on `6c3d68b` (`payments.active` = 2, cache bust `20260913`)
- Claim cancel URL + confirm-success tracking live on `991eb64`
- Sanitizer harden live on `2340351` (live `/health` sha `2340351`, `payments.active` = 2)

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
target.

**Mini App payment-claim confirm URL — live on `6c3d68b`:** vendor ping
includes `/confirm?ct=` URL button + cancel text line; buyer DM on first
claim; second tap 200 idempotent; no stock change.

**Mini App claim cancel URL + confirm-success tracking — live on `991eb64`:**
PAYMENT CLAIM is Confirm + Cancel URL buttons; `/confirm` success offers
Add tracking. Claim still no stock change.

## This ship

**Empty-handle rails are not checkout-ready.** An active typed method
(Venmo / PayPal / Cash App / Zelle / Apple Cash / crypto) with no
handle/cashtag/email/phone/wallet no longer counts as a pay rail:

- `checkout_ready` is true only when ≥1 **usable** active method
  (copyable target, or custom instructions ≥8 chars).
- `/storefront` `payments` / `payment_methods` list usable rails only.
- `POST /order` 409 `no_payment_methods` when none are usable (same
  buyer `message` as a fully empty shop). Telegram cart checkout
  blocks the same way.
- `/health` adds `payments.usable` (count, never handles).
  `checkout_ready` follows usable, not merely `active > 0`.
- Telegram 💳 Payments and `/webpanel` warn when enabled rows have
  no handle. Panel state includes `rail_ready`.
- Seeded Unicorn Venmo + PayPal still pass. Custom “pay in person”
  instructions still count. `STORE_URL_CACHE_BUST` stays `20260913`.

## Acceptance (this ship)

- [ ] `python -m pytest -q -x` green on scratch DBs
- [ ] Docker COPY check still lists every imported module (26)
- [ ] No writes to laptop `inventory.db`; no DELETE of products
- [ ] Empty-handle Venmo: `checkout_ready` false, storefront names
      empty, `POST /order` 409 `no_payment_methods`, no order row
- [ ] Empty Venmo + usable PayPal: `checkout_ready` true, catalog
      names PayPal only
- [ ] Seeded Unicorn shop still `checkout_ready`; health `usable` ≥ 1
- [ ] Telegram + panel copy warn on empty handles
- [ ] Live `/health` new `git_sha`, `payments.active` ≥ 1,
      `payments.usable` ≥ 1, `checkout_ready: true`,
      `store_url_cache_bust` `20260913` (after AUTOPUSH)
- [ ] No secrets / `.env` / scratch import files committed

Prior `991eb64` / `6c3d68b` / `bef4d5f` checks stay true: Confirm +
Cancel URL buttons, `/confirm` Add tracking, `can_mark_paid`, Pages
I've paid, buyer claim DM.

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
   `payments.active` ≥ 1, `payments.usable` ≥ 1,
   `payments.checkout_ready: true`, `store_url_cache_bust` `20260913`
2. Open https://remy-miniapp-demos.pages.dev/unicorn/ — checkout still
   enabled (Venmo + PayPal have handles); PayPal email is Copy, not a
   fake paypal.me link.
3. Place nothing. Do not run local `start.bat`. Do not wipe `/data`.
