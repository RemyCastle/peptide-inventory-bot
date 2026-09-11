# Unicorn catalog ship — 2026-09-11

**Bot:** @UnicornMagicFactory2Bot only  
**Live:** https://unicornfartzz-bot.onrender.com  
**Mini App:** https://remy-miniapp-demos.pages.dev/unicorn/  
**Never:** wipe `/data/inventory.db` (Render disk). Laptop `inventory.db` was not touched.

## What was wrong

Public `/storefront` still served uniqueness-hack import tails:

- `Anav@r 25mg`
- `$price` jammed into names (`Aod 5mg (vial) $15.00`)
- original card + `(vial)`/`(kit)` copy of the same dose

Sample **before this follow-up** (public catalog, 2026-09-11): **312** active products, **1** `Anav@r`, **32** names with `$`, plus `(vial)`/`(kit)` suffix dupes. Shop title on that feed: `Shop`.

Earlier local commit `ee22a98` only sanitized **display** labels and was not what live was running. Boot cleanup on `d0fcf37` ran only inside the claim-token bind, so the Pages catalog shop could stay dirty, and same-price uniqueness copies stayed as a second card after kit merge.

## What this ship does

On Unicorn boot, `run_cloud._cleanup_unicorn_catalog` runs against `unicorn_shop.find_catalog_shop()` only (MagicFactory2 Pages catalog). Owner-gated. Idempotent.

- `Anav@r` → `Anavar 25mg`
- strip `$price` / `(vial)` / `(kit)` uniqueness tails
- merge true vial/kit pairs (kit ≈ 6–12× vial, or explicit kit unit with a **higher** price): keeper keeps the **lower** vial price, `kit_price` = higher; loser `active=0`
- hide same-price uniqueness copies of the keeper (original `Aod 5mg` $15 + `Aod 5mg (vial) $15.00`)
- **do not** merge sibling SKUs (B12 $10/$15, MT1/MT2 $11/$30, Oxytocin $10/$30) — strip `$` only
- never DELETE rows; open order lines keep `product_id` + copied `product_name`
- other shops on the same disk are not written

Local simulation of that plan against the live JSON: remaining weird names **0**, remaining same-name/same-price dupes **0**. Tests: **523** passed.

## Out of scope

- Hard delete / DB wipe
- Other vendor shops
- Autopush as a general peptide_inventory_bot habit (this repo auto-deploys **two** Render services)

## Remy check (60s)

1. GET https://unicornfartzz-bot.onrender.com → `ok: true`
2. Mini App / `/storefront`: no `Anav@r`, no `$` inside names, one Aod 5mg card with kit price
3. B12 / MT1 / MT2 / Oxytocin still two prices if they were two SKUs
4. Place nothing; do not run local `start.bat` while cloud is live
5. If names still dirty: Render dashboard → unicornfartzz-bot (`srv-d9a6h057vvec738lov80`) logs for `unicorn catalog cleanup`
