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
- Laptop `inventory.db` is not live; Render `/data/inventory.db` is never opened from tests

## Prior ship (P0 + top P1) — live on `16c6547`

1. Boot seed if empty — `run_cloud._seed_unicorn_payments` even without claim env.
2. Admin path if empty — Telegram **Seed Venmo + PayPal**; panel `{seed_defaults: true}`.
3. Mini App `POST /order` 409 `no_payment_methods` before `create_order`.
4. Buyer JSON `payment_methods` on `POST /order`.
5. Telegram quick-add PayPal + Apple Cash.
6. `/health` `payments.active` / `payments.total`.
7. 401 `detail`.
8. Honest empty copy.

## This ship (checkout JSON follow-up)

1. **`GET /order-status`** returns `payments` string lines + `payment_methods`
   `{name, method_type, target, pay_url, line}` (same shape as `POST /order`)
   so “check my order” can show pay links. Public `/storefront` stays names only.
2. **401/409 `message`** — buyer-facing alert text (never a secret).
3. **401 `error` split** — `empty_initdata` / `bad_payload` / `bad_hash` /
   `expired` so the Mini App can say “open from the bot” vs “wrong bot”.
4. **`/health` `invoices.enabled`** — boolean only; no provider token.

## Acceptance

- [ ] `python -m pytest -q -x` green on scratch DBs
- [ ] Docker COPY check still lists every imported module
- [ ] No writes to laptop `inventory.db`; no DELETE of products
- [ ] `GET /order-status` includes `payment_methods[].pay_url` for Venmo
- [ ] Empty `initData` → 401 `empty_initdata` + `message`; zero new orders
- [ ] Zero active methods → 409 `no_payment_methods` + `message`
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
