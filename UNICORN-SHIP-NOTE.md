# Unicorn catalog ship — 2026-09-11

**Bot:** @UnicornMagicFactory2Bot only  
**Live:** https://unicornfartzz-bot.onrender.com  
**Mini App:** https://remy-miniapp-demos.pages.dev/unicorn/  
**Never:** wipe `/data/inventory.db`

## What was wrong

Buyer catalog still showed uniqueness-hack tails on the **live DB**:

- `Anav@r 25mg`
- names with `$price` jammed in (`Aod 5mg (vial) $15.00`)
- duplicate vial/kit cards as two rows

Commit `ee22a98` (display sanitize + owner cleanup) was **local ahead 1** and **not on Render**, so the storefront JSON stayed dirty.

Sampled public `/storefront` 2026-09-11 before this ship: dirty names present (Anav@r, `$` tails, (vial)/(kit) pairs).

## What this ship does

1. **Pushes** `ee22a98` plus boot-time apply.
2. On Unicorn bind at boot, `catalog_cleanup.apply_bound_shop_cleanup` runs for the MagicFactory2 bound shop only:
   - Anav@r → Anavar 25mg
   - strip `$price` / `(vial)`/`(kit)` uniqueness tails
   - merge true vial/kit pairs (kit ≈ 6–12× vial): keeper keeps **lower** price, `kit_price` = higher; loser `active=0`
   - **do not** merge sibling SKUs (B12 $10/$15, MT1/MT2 $11/$30, Oxytocin $10/$30) — strip `$` only
   - never DELETE rows; open order lines keep `product_id` + copied `product_name`
3. Storefront API already uses `display_product_name` after `ee22a98`, so labels stay clean even before the next cleanup.

## Out of scope

- Other shops on the same disk
- Hard delete / DB wipe
- Autopush of peptide_inventory_bot in general (this push was an explicit Remy ship; it **does** auto-deploy both Render services on this repo)

## Git / Render

Pushed `1895573` to `RemyCastle/peptide-inventory-bot` `master` (rebased onto `2f5cc65` first — remote had Cursor PRs). This repo auto-deploys **two** Render services.

Live sample **after push** (same public storefront, ~4 min later): still **312** products, **83** dirty names (`Anav@r 25mg` still present). One **502** during the window, then the old catalog again — new code is **not serving yet**. Confirm `unicornfartzz-bot` (dashboard `srv-d9a6h057vvec738lov80`) actually built this SHA. If auto-deploy is stuck, Manual Deploy → latest master. After a healthy boot, cleanup runs once for the bound Unicorn shop.

## Remy check (60s)

1. GET https://unicornfartzz-bot.onrender.com → `ok: true`
2. Mini App shelf: no `Anav@r`, no `$` inside names, one Aod 5mg card with kit price
3. B12 / MT1 / MT2 / Oxytocin still two prices if they were two SKUs
4. Place nothing; do not run local `start.bat` while cloud is live
5. If names still dirty: Render dashboard → unicornfartzz-bot logs for `unicorn catalog cleanup`
