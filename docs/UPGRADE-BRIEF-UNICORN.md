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
- Boot seed used to run only inside `if UNICORN_CLAIM_TOKEN`
- Telegram checkout already blocks on zero methods; Mini App did not
- Laptop `inventory.db` is not live; Render `/data/inventory.db` is never opened from tests

## This ship (P0 + top P1)

1. **Boot seed if empty** — `run_cloud._seed_unicorn_payments` on the Pages
   catalog shop even when claim env is unset. Idempotent by `method_type`.
   Pause ≠ delete.
2. **Admin path if empty** — Telegram **Seed Venmo + PayPal** (Unicorn shop,
   zero rows); panel warning + same seed; `POST /panel/api/payment`
   `{seed_defaults: true}`; admin home banner when no active rails.
3. **Mini App refuses unpaid void** — `POST /order` returns 409
   `no_payment_methods` **before** `create_order` when no active methods.
4. **Buyer JSON** — keep `payments` string lines; add `payment_methods`
   `{name, method_type, target, pay_url, line}` so Pages can stop regex-parsing.
5. **Telegram quick-add** — PayPal + Apple Cash (templates already existed).
6. **`/health`** — `payments.active` / `payments.total` for the catalog shop
   (counts only, no handles).
7. **401 `detail`** — short `InitDataError.reason` on `/order` (never a secret).
8. **Honest empty copy** — stop promising a DM that will not contain rails.

## Acceptance

- [ ] `python -m pytest -q -x` green on scratch DBs
- [ ] Docker COPY check still lists every imported module
- [ ] No writes to laptop `inventory.db`; no DELETE of products
- [ ] Boot without `UNICORN_CLAIM_TOKEN` still inserts Venmo + PayPal when
      those types are absent
- [ ] `POST /order` with zero active methods → 409, zero new orders
- [ ] Unicorn panel `{seed_defaults: true}` seeds; other shops 400
- [ ] Live `/health` `ok: true` and `git_sha` matches the pushed commit
- [ ] No secrets / `.env` / scratch import files committed

## Out of scope

- Cloudflare Pages HTML (SKU on cards, mockup copy, using `pay_url` client-side)
- Hard-delete / DB wipe / importer against live stock
- Telegram Stars; enabling invoices (needs Remy provider token)
- SPBC back-room; other vendor shops' default handles
- Force-push; committing untracked scratch

## Verify in 60s

1. GET https://unicornfartzz-bot.onrender.com/health → `ok: true`, new `git_sha`,
   `payments.active` ≥ 1 (or `total` ≥ 1 if owner paused — then unpause, don't wipe).
2. GET `/storefront?invite=` (Pages key) → `payments` non-empty names.
3. Place nothing. Do not run local `start.bat`. Do not wipe `/data`.
