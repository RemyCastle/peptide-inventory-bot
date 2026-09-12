# SHIP BRIEF — MEGA-UNICORN v2 panel pay-target copy + leftover notify

Product: peptide (unicorn)  
Repo: `C:\Users\Remy\peptide_inventory_bot`  
Prod: https://unicornfartzz-bot.onrender.com  
Priority: P1  
Ship agent: grok  
AUTOPUSH: this ship (type-specific panel/API/Telegram pay-target
copy; leftover vendor-notify + low-stock names; no stock change;
never wipe inventory.db)

Goal: Panel and API name the missing pay target the same way Telegram
does (cashtag / wallet / Zelle email or phone, not a generic handle);
crypto without a network note warns the admin; leftover vendor-notify
and low-stock names come out clean; never wipe inventory.

Context (already verified — do not rediscover):

- Deep dive: [`GROK-DEEP-DIVE-UNICORN.md`](GROK-DEEP-DIVE-UNICORN.md)
- `POST /order` auth + HMAC: [`../UNICORN-INITDATA-RUNBOOK.md`](../UNICORN-INITDATA-RUNBOOK.md)
- Catalog vs Pages: [`../UNICORN-MINIAPP-PARITY.md`](../UNICORN-MINIAPP-PARITY.md)
- P0 seed/409/health live on `16c6547` (`payments.active` = 2)
- Empty-handle rails live on `8829dfb` (`payments.usable` = 2)
- PayPal/Apple Cash quick-add + refuse empty typed saves live on `3663272`
- Empty-rail copy + URL ZWJ/NBSP live on `1555d79`
- Laptop `inventory.db` is not live; Render `/data/inventory.db` is never opened from tests
- This ship: panel field labels/placeholders; type-specific 400;
  Zelle "email or phone"; crypto network warning (still usable);
  leftover NEW ORDER notify + low-stock names

## Prior ships

**P0 + top P1 — live on `16c6547`:** boot seed, admin seed, `POST /order` 409
`no_payment_methods`, buyer `payment_methods`, PayPal/Apple Cash quick-add,
`/health` payment counts, 401 `detail`, honest empty copy.

**Empty-rail copy + leftover URL/report — live on `1555d79`:** crypto /
Cash App / Apple Cash empty rails name wallet/cashtag/phone;
`public_http_url` rejects `%E2%80%8D` / `%C2%A0`; pending-orders report
sanitized.

## This ship

**Panel pay-target copy + leftover notify/low-stock names.** Payments P1
is live (Venmo+PayPal usable). Leftover: panel still labeled every typed
field "Handle / email / phone"; Zelle copy said "contact"; API 400 said
"Payment handle / number required"; Telegram empty-typed re-prompt was a
slash list; crypto with an address and no network note was silent;
NEW ORDER vendor DM and low-stock alerts still echoed dirty names.

- `PAYMENT_TARGET_COPY` is the source of truth (panel JS `PAY_TARGET_COPY`
  stays in sync). Field labels, placeholders, 400, Telegram re-prompt,
  and Zelle `pay_hint` all use it.
- Crypto missing `network_note` stays **usable** and shows admin
  `rail_warning` (wrong-chain is on the seller to prevent).
- `build_new_order_notify_text` + `format_new_order_ship_section`
  sanitize buyer / shop / item / ship. Low-stock alert names use
  `display_product_name`.
- `STORE_URL_CACHE_BUST` stays `20260913`. No Pages JS. No DELETE.

## Acceptance (this ship)

- [x] `python -m pytest -q -x` green on scratch DBs (695 passed)
- [x] Docker COPY check still lists every imported module (26)
- [x] No writes to laptop `inventory.db`; no DELETE of products
- [x] Panel label for Cash App is cashtag, not handle
- [x] Zelle empty copy / 400 / `pay_hint` say email or phone, not contact
- [x] Enabled empty crypto 400 says wallet; missing network warns, still usable
- [x] Dirty NEW ORDER notify and low-stock names come out clean
- [x] No secrets / `.env` / scratch import files committed

Prior `1555d79` / `3663272` / `8829dfb` checks stay true:
PayPal/Apple Cash quick-add, usable rails only, Confirm + Cancel URL
buttons, empty-rail type-specific Telegram list, URL ZWJ/NBSP fail closed.

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
