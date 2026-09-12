# Unicorn Magic Factory — Grok deep dive (2026-09-11)

**Bot / API:** https://unicornfartzz-bot.onrender.com  
**Poller:** `@UnicornMagicFactory2Bot`  
**Mini App (Cloudflare Pages, not this repo):** https://remy-miniapp-demos.pages.dev/unicorn/  
**Never:** wipe `/data/inventory.db` on Render. Laptop `inventory.db` is not the live catalog.

This is the architecture + gap map for Mini App checkout, payment-methods UX,
`initData`, catalog, and Telegram menus. Implementation of the top slices lives
in [`UPGRADE-BRIEF-UNICORN.md`](UPGRADE-BRIEF-UNICORN.md). HMAC / trailing-slash
ops stay in [`../UNICORN-INITDATA-RUNBOOK.md`](../UNICORN-INITDATA-RUNBOOK.md).
Catalog vs Pages chrome stays in [`../UNICORN-MINIAPP-PARITY.md`](../UNICORN-MINIAPP-PARITY.md).

---

## 1. Surfaces (who owns what)

| Surface | Where | Writes stock/orders? |
|---|---|---|
| Cloudflare Pages Mini App | `C:\Users\Remy\projects\miniapp-demos\unicorn\index.html` (not this git remote) | No. View + POST cart. |
| Public catalog | `GET /storefront?invite=` → `webpanel.api_storefront` | No. `storefront_keys` only. |
| Checkout | `POST /order` → `spbc_notify.handle_http_order` | Yes. One order per success. `create_order` is authoritative. |
| Order lookup | `GET /order-status?invite=&code=` | No. Shop-scoped by catalog key. |
| Vendor bot (Unicorn) | `vendor_stores._build_app` poller + keyboard WebApp button | Yes, via `on_web_app_data` fallback. |
| Main inventory bot | `bot.py` catalog / cart / admin | Yes, Telegram-native checkout. |
| Web panel | `/webpanel` magic link → `webpanel.api_*` | Yes, shop-scoped. |
| Boot | `run_cloud._bind_vendor_miniapps` | Bind keys, catalog cleanup, **payment seed**. Never DELETE products. |

Two secrets stay separate: `storefront_keys` (public catalog) never gain
claim/admin power; `vendor_invites` never resolve on `/storefront` or `/order`.

---

## 2. Mini App checkout

Buyer path:

1. Telegram opens the Web App URL (must be trailing-slashed `/unicorn/` — see
   runbook). `Telegram.WebApp.initData` is injected.
2. Pages `loadLiveCatalog()` GETs `/storefront?invite=<24hex>` from
   `STOREFRONT_HOSTS` (unicornfartzz-bot first). Sample catalog is **not** shown
   when the live fetch fails (`renderCatalogError`).
3. Cart + shipping sheet → `submitOrder()` POSTs
   `{invite, initData, items, ship}` to `/order`.
4. Success → `showOrderReceipt(d)` uses `d.code`, `d.total`, `d.payments`
   (string lines). `payHref()` regex-parses Venmo / Cash App / PayPal.me from
   those strings.
5. No `initData` → `TG.sendData` keyboard fallback (`on_web_app_data`).
6. Opened in a plain browser → alert to open from the bot.

Server path (`handle_http_order`):

1. Resolve shop via `resolve_storefront_key` (claim tokens rejected).
2. Validate `initData` against **every** token bound to that shop
   (`get_bot_tokens_for_shop` + HMAC aliases).
3. Parse cart (`parse_miniapp_cart_items`) + ship fields.
4. `db.create_order` **once**. Stock is **not** deducted here — deduction is
   admin confirm. 409 `ORDER_STOCK_ERROR` if available-to-sell fails.
5. Vendor/owner DM, buyer HTML receipt (tap-to-copy code + pay links + QR),
   JSON `{ok, code, total, payments, message, invoice_offered}`.

**Gap (this ship):** Telegram-native checkout already **blocks** when the shop
has zero active payment methods (`bot.py` `cb_checkout_start`). Mini App
`POST /order` still created the order and returned `payments: []`, so the
buyer saw a code with no rail. Boot seed only ran inside `if UNICORN_CLAIM_TOKEN`,
so a Pages-bound catalog shop with no claim env could boot with an empty pay
screen.

---

## 3. `initData`

Telegram signs `user=…&auth_date=…&hash=…` with the bot the buyer **opened**,
not necessarily the polling bot. Unicorn must accept `@UnicornMagicFactory2Bot`
plus HMAC aliases (`extra_tokens` / `UNICORN_EXTRA_BOT_TOKENS` /
`TELEGRAM_BOT_TOKEN` + `BOT_TOKENS` for the catalog shop). Aliases do **not**
start a second poller and are never used to send DMs.

HMAC (Telegram spec) is in `vendor_stores.validate_webapp_init_data`. Replay
guard: `auth_date` within 24 h, not >60 s in the future. `hash_ok=True` after
a match so `validate_webapp_init_data_any` does not overwrite `expired` with a
later token's `bad_hash`.

Buyer-facing JSON still collapses almost everything to `bad_hash` vs `expired`
(`initdata_error_code`). The **log `reason`** is the real diagnosis
(`initData_len=0` → trailing-slash redirect; `bad hash` → wrong/missing token;
`missing user` → signed but incomplete payload). This ship adds a short
`detail` field on 401 so the Mini App alert can show the reason without
opening Render logs. It is never a secret.

Cloudflare Pages 308 `/unicorn` → `/unicorn/` strips `#tgWebAppData=…` on iOS
WKWebView. `_normalize_store_url` + BotFather Menu Button must keep the
trailing slash. `STORE_URL_CACHE_BUST` busts Telegram's cached HTML after a
Pages deploy.

---

## 4. Payment methods UX

Rows live in `payment_methods` (per shop). Types:
`cashapp | venmo | paypal | zelle | apple_cash | crypto | custom`.
Buyers only see `active=1`, `ORDER BY sort_order, name`.

### Buyer

| Channel | What they see |
|---|---|
| Telegram cart checkout | Inline method picker. **Blocked** if none active. |
| Vendor-bot DM after Mini App order | HTML receipt: tap-to-copy code, handle, Friends & Family warning, pay URL, QR (`segno`). |
| Mini App success sheet | String lines from `POST /order` `payments` + regex pay links. Empty list = no buttons. |
| `GET /storefront` | **Names only** (plus `payment_methods` name+type after this ship). Handles stay off the public catalog. |

Empty-copy used to say “Payment details will be DM'd to you.” That is a lie
when no methods exist. This ship tells the buyer to message the seller and
not send money.

### Admin

| Path | How |
|---|---|
| Boot seed | `run_cloud._seed_unicorn_payments` → `webpanel.ensure_unicorn_shop_payments`. Idempotent by `method_type` (paused rows count as present). **Pause, don't delete,** a seeded type you don't want. |
| Telegram **💳 Payments** | `cb_adm_pays`. Quick-add templates + freeform. Pause / delete. This ship adds PayPal + Apple Cash + a Unicorn-only **Seed Venmo + PayPal** when the list is empty. |
| Web panel Payments card | Typed fields, quick-add, Save/Delete. This ship warns when nothing is enabled and offers the same seed on the Unicorn shop. |
| `POST /panel/api/payment` | Add / update / delete. This ship: `{seed_defaults: true}` (Unicorn shop only). |

Seeded Unicorn defaults (buyer-facing handles, not secrets): Venmo `@wineboos`,
PayPal `unicornfartzz@proton.me` as friends & family. Edit in the panel; boot
will not overwrite an existing type.

---

## 5. Catalog

Public JSON is the Mini App shelf. `display_product_name` strips `$price` /
`(vial)` / `(kit)` / `Anav@r`. Boot `catalog_cleanup.apply_bound_shop_cleanup`
is shop-scoped, idempotent, **never DELETE**. Empty `OWNER_IDS` no longer
blocks it. Sibling SKUs (B12 / MT1 / MT2 / Oxytocin, Snap 8 `$8` vs `$50`)
must stay distinct.

`/health` exposes `git_sha` (`RENDER_GIT_COMMIT`) + last cleanup counts (no
chat ids). This ship also exposes `payments.active` / `payments.total` for the
catalog shop so an empty pay screen is visible without dumping the DB.

Mini App chrome copy (“sample data”) is Pages, not this repo. Search / tile /
favorites are client-only. Telegram `file_id` photos do not appear on the
storefront (HTTP URLs only).

---

## 6. Telegram menus

**Vendor / Unicorn customer bot** (`vendor_stores`):

- Persistent keyboard: `🦄 Open the Store` → `WebAppInfo(store_url)` with
  trailing slash + `?v=` cache bust.
- `/start` welcome. `/webpanel` mints a 3-day admin link (DM in groups).
- `/myid`. No Telegram catalog buttons on this poller — catalog is the Mini App.

**Main inventory bot** (`bot.py` `post_init`):

- Buyer commands: `/start` `/catalog` `/search` `/cart` `/myorders` `/shops` `/help` `/cancel`.
- Admin commands scoped to `BotCommandScopeChat` for shop admins; owners get
  the full owner set. Customers never see `/admin` in the slash menu.
- Admin home: Products, Orders, **💳 Payments**, Shipping, Site links, More tools.
- Catalog buttons are glyph-repaired and capped at 64 characters.
- Group checkout hands off to DM so the shipping address never hits the group.

Menu Button URL in BotFather must match `UNICORN_STORE_URL` / vendor
`store_url` (`…/unicorn/`). Wrong URL = empty `initData`.

---

## 7. Ranked gaps (before this ship)

| Pri | Gap | Risk if ignored |
|---|---|---|
| P0 | Payment seed only ran when `UNICORN_CLAIM_TOKEN` was set | Empty Mini App pay screen; orders with no rail |
| P0 | Mini App `POST /order` did not refuse empty methods | Buyer gets a code they cannot pay |
| P0 | Admin empty-state was a quiet “_None configured._” | Owner doesn't notice checkout is dead |
| P1 | Telegram quick-add missing PayPal / Apple Cash | Types exist in templates; UI hid them |
| P1 | `payments` JSON is regex-parsed strings | Fragile Venmo/Cash App links in Pages |
| P1 | `/health` hid whether rails exist | Had to guess or dump DB |
| P1 | 401 `bad_hash` overloaded | Buyer alert can't say “re-open” vs “wrong bot” — **live `52a5e5a`** |
| P1 | Remaining `POST /order` errors had no `message` | Mini App `alert(d.error)` showed codes for empty cart / no vendor token |
| P1 | `GET /order-status` still offered `pay_url` after paid | “Check my order” could re-prompt a paid buyer |
| P2 | Pages mockup copy / SKU on cards | Out of this repo |
| P2 | Native Telegram invoice (provider token unset) | Optional; Stars forbidden for physical goods |

---

## 8. Do / Don't

**Do:** migrate, never reset; seed or pause payment rows; bump
`STORE_URL_CACHE_BUST` only after Pages checkout JS changes; confirm live
`/health` `git_sha` after deploy; tests against a scratch `DB_PATH` only.

**Don't:** wipe `/data/inventory.db` or laptop `inventory.db`; run `start.bat`
while cloud is live; hard-delete products; put claim tokens in Pages HTML;
use Telegram Stars for physical goods; force-push; commit `.env` / scratch
import dumps.
