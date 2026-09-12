# Unicorn catalog ship — 2026-09-11 (extreme follow-up)

**Bot:** @UnicornMagicFactory2Bot only  
**Live:** https://unicornfartzz-bot.onrender.com  
**Mini App:** https://remy-miniapp-demos.pages.dev/unicorn/  
**Never:** wipe `/data/inventory.db` (Render disk). Laptop `inventory.db` was not touched.

## This follow-up

Purge remaining weird catalog names + UTF-8 menus + admin UX + Mini App parity notes.

- Snap 8 `$8` / `$50` (250mg) no longer auto-merges (ratio floor 7.0 + distinct descriptions). `$8` + `$75` kit still merges.
- MT1 / MT2 / Oxytocin sibling cards keep both rows and append `$price` when description is empty.
- Generic shop title `Shop` persists as `Unicorn Magic Factory` on Unicorn boot only.
- JSON `/storefront` `/health` `/order-status` and panel APIs declare `charset=utf-8` and dump Unicode (emoji) unescaped.
- Telegram shop picker + admin product buttons run through glyph repair / 64-char cap.
- Admin `/webpanel` catalog: find box, show-hidden, SKU field, duplicate-name flag, “buyers see: …”.
- Notes: `UNICORN-MINIAPP-PARITY.md` (Pages mockup copy is out of this repo).

---

# Unicorn catalog ship — 2026-09-11 (prior)

**Bot:** @UnicornMagicFactory2Bot only  
**Live:** https://unicornfartzz-bot.onrender.com  
**Mini App:** https://remy-miniapp-demos.pages.dev/unicorn/  
**Never:** wipe `/data/inventory.db` (Render disk). Laptop `inventory.db` was not touched.

## What was wrong

Public `/storefront` still served uniqueness-hack import tails after `a449e82`:

- `Anav@r 25mg`
- `$price` jammed into names (`Aod 5mg (vial) $15.00`)
- original card + `(vial)`/`(kit)` copy of the same dose

Sample **before this follow-up** (public catalog): **312** active products, **1** `Anav@r`, **32** names with `$`, plus `(vial)`/`(kit)` suffix dupes. Shop title on that feed: `Shop`.

`a449e82` was on GitHub but **did not boot**. `bot.py` imports `catalog_cleanup` at module load, and `Dockerfile` did not `COPY catalog_cleanup.py`. Render kept the old process. Display sanitization and boot cleanup never ran.

A later boot on `d30a728` (module now in the image) still **skipped DB cleanup** because Render `OWNER_IDS` is empty (`catalog_cleanup.skipped=no OWNER_IDS`). Buyer names were already stripped in `/storefront`; the MagicFactory2 rows stayed dirty.

## What this ship does

On Unicorn boot, `run_cloud._cleanup_unicorn_catalog` runs against `unicorn_shop.find_catalog_shop()` only (MagicFactory2 Pages catalog). Shop-scoped. Idempotent. **Never DELETE.** Empty `OWNER_IDS` no longer blocks boot apply.

- `Anav@r` → `Anavar 25mg`
- strip `$price` / `(vial)` / `(kit)` uniqueness tails
- merge true vial/kit pairs (kit ≈ 6–12× vial, or explicit kit unit with a **higher** price): keeper keeps the **lower** vial price, `kit_price` = higher; loser `active=0`
- hide same-price uniqueness copies of the keeper
- **do not** merge sibling SKUs (B12 $10/$15, MT1/MT2 $11/$30, Oxytocin $10/$30) — strip `$` only
- `/health` exposes `git_sha` (`RENDER_GIT_COMMIT`) and last `catalog_cleanup` counts
- Claude glyph repair (`b127c5d`): shop title/welcome + labels self-heal mojibake; Grok did not restore/overwrite those files

## Git

Pushed `89a7ab8` to `RemyCastle/peptide-inventory-bot` `master` (includes `d30a728` Docker COPY + `b127c5d` Claude glyph repair + boot apply without `OWNER_IDS`). Laptop `inventory.db` not opened for writes.

## Live resample (2026-09-11, after `89a7ab8` boot)

`GET /health`:

- `ok: true`
- `git_sha`: `89a7ab80a7b05a0bb10b1b3f736afcf849840a15`
- `catalog_cleanup`: **31** rename(s), **38** merge(s), **78** deactivated, 147 row(s) touched, **no products deleted**

`GET /storefront`:

- **234** active products (was 312)
- **0** `Anav@r`, **0** `$` in names, **0** `(vial)`/`(kit)` suffixes
- `Anavar 25mg` $35
- one `Aod 5mg` card, vial $15, `kit_price` $130
- B12 5ml still two prices ($10 and $15 hydroxycolabin)
- MT1 / MT2 / Oxytocin still two prices ($11/$30, $10/$30)
- Mini App production `https://remy-miniapp-demos.pages.dev/unicorn/` title `Unicorn Magic Factory 🦄` (Claude UI fix deployed to Pages **main**, not the `master` preview)

## Out of scope

- Hard delete / DB wipe
- Other vendor shops
- Setting `OWNER_IDS` on Render (boot no longer needs it for this cleanup)
- Telegram menu source (Claude `b127c5d`; Grok did not restore those files)

## Remy check (60s)

1. GET https://unicornfartzz-bot.onrender.com/health → `ok: true`, `git_sha` starts `89a7ab8`, `catalog_cleanup.ok: true`
2. Mini App / `/storefront`: no `Anav@r`, no `$` inside names, one Aod 5mg card with kit price
3. B12 / MT1 / MT2 / Oxytocin still two prices if they were two SKUs
4. Place nothing; do not run local `start.bat` while cloud is live
5. Do not wipe `/data`
