# SHIP BRIEF — MEGA-UNICORN v2 empty-rail copy + URL leftover

Product: peptide (unicorn)  
Repo: `C:\Users\Remy\peptide_inventory_bot`  
Prod: https://unicornfartzz-bot.onrender.com  
Priority: P1  
Ship agent: grok  
AUTOPUSH: this ship (empty-target payment copy + leftover URL/report
fields; no stock change; never wipe inventory.db)

Goal: Empty payment rails name the missing target (cashtag / wallet /
phone, not a generic "handle"); http URLs fail closed on ZWJ/NBSP;
pending-order reports come out clean; never wipe inventory.

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
- Empty-handle rails live on `8829dfb` (`payments.usable` = 2)
- PayPal/Apple Cash quick-add + refuse empty typed saves live on `3663272`
- Sanitizer audit live on `4acf273` (invisible fillers, percent-decoded
  URL C0/Cf, leftover branding/order/report fields)
- This ship: type-specific empty-rail copy; URL ZWJ/NBSP fail closed;
  pending-orders report leftover

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

**Empty-handle rails are not checkout-ready — live on `8829dfb`:**
typed methods with no handle do not count as a pay rail; `POST /order`
409; health `payments.usable`; admin/panel warn.

**Sanitizer audit — live on `4acf273`:** invisible fillers, percent-decoded
C0/Cf/line-sep, leftover `shop_display` / order summary / payment HTML /
inventory report / shipping label / tracking URL / site-sync.

## This ship

**Empty-rail copy + leftover URL/report fields.** Payments P1 is live
(Venmo+PayPal usable). Leftover: admin/panel still said "handle" for
crypto wallets / Cash App cashtags / Apple Cash numbers; `public_http_url`
kept ZWJ (catalog KEEP_CF) and NBSP so `exam%E2%80%8Dple.com` /
`%C2%A0` could spoof; pending-orders report still echoed dirty names.

- Type-specific empty-target copy: `payment_target_kind_label` /
  `payment_empty_rail_hint` / `payment_empty_rails_admin_line`. Panel
  per-row + save toast; Telegram payments list + checkout-blocked.
- `public_http_url` fails closed on all Cf (incl. ZWJ/tags), Zs other
  than SPACE, and combining marks. Pirate flag / profession ZWJ stay
  on catalog labels. `%20` and emoji percents stay.
- Pending-orders report sanitizes buyer / method / item names. Telegram
  shipping label uses `shop_display`.
- `STORE_URL_CACHE_BUST` stays `20260913`. No Pages JS. No DELETE.

## Acceptance (this ship)

- [x] `python -m pytest -q -x` green on scratch DBs (691 passed)
- [x] Docker COPY check still lists every imported module (26)
- [x] No writes to laptop `inventory.db`; no DELETE of products
- [x] Crypto empty rail says wallet address, not handle
- [x] `public_http_url` rejects `%E2%80%8D` / `%C2%A0` / `%CC%81`;
      keeps `%20` and emoji percents; pirate ZWJ kept in names
- [x] Dirty pending-orders report comes out clean
- [x] No secrets / `.env` / scratch import files committed

Prior `4acf273` / `3663272` / `8829dfb` / `991eb64` checks stay true:
PayPal/Apple Cash quick-add, usable rails only, Confirm + Cancel URL
buttons, invisible fillers, `can_mark_paid`.

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
