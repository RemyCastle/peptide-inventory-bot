# Legacy Python bot — feature inventory

The live Unicorn bot runs on Cloudflare. Its source is not in GitHub and is not this repository. This Python tree is a reference library. Do not deploy it.

Nothing below is a confirmed "already ported" check. The Cloudflare code was not available here, so the last section is the list to verify and port if the live bot does not already do it.

## Customer

| Command or flow | What it does |
|---|---|
| `/start` | Shop menu, deep links (`shop_`, `vendor_`, `clone_`, `transfer_`, `collab_`), Unicorn "open store" keyboard when this process is the Unicorn bot |
| `/help` | Short help |
| `/catalog`, `/search` | Browse and search. Open catalog shows the top sellers; the rest is search |
| `/cart` | Cart, vial and kit lines, checkout |
| `/myorders` | That customer's orders |
| `/orders` | Shop order list for an allowlisted admin; otherwise the customer's own orders |
| `/cancel` | Leave a multi-step prompt |
| Checkout | Payment method, shipping address, "I've paid", optional proof photo |
| Mini App `web_app_data` | Vendor bot receives the Pages checkout and creates a local order |

Stock is checked when the order is created and again when payment is confirmed. Only the confirm reduces stock.

## Shop admin

These commands and the matching buttons now require the sender's Telegram user ID to be in `ADMIN_TELEGRAM_IDS`. A row in the shop `admins` table is not enough. An empty allowlist denies everyone.

| Command or screen | What it does |
|---|---|
| `/admin` | Products, orders, payments, shipping, shop link, more tools |
| `/setup` | Create a group shop. Allowlisted owner, or allowlisted Telegram group admin |
| `/restock` | Add received quantity by product name |
| `/webpanel` | Magic link to the browser panel (main bot and vendor bots) |
| `/linksite` | Attach a website catalog to the shop |
| Products | Price, kit price (default 10 vials), stock, photo, COA, active flag, rename, unit, import `.txt`, mass edit |
| Orders | Confirm paid, reject, tracking, mark shipped, view proof |
| Payments | Templates (Venmo, PayPal, Cash App, Zelle, crypto, and others), pause, delete |
| Shipping | Fee, free-over threshold, on/off, optional zones |
| Minimum order | Quantity and vial/kit label |
| Admins | Add or remove shop admins by Telegram user ID |
| Shop | Rename, clone into another group, transfer to another group |
| Collab | Share another shop's products with a markup |
| Site links | Sync or remove a mirrored site |

## Owner

Same allowlist, and the existing owner check (`OWNER_IDS` / `db.is_owner`).

| Command | What it does |
|---|---|
| `/master`, `/invoices` | Hidden per-order service fee, weekly fee invoices, mark an invoice paid |
| `/backup`, `/backup_status` | Encrypted snapshot of `inventory.db`, vault listing, token-pool size |
| `/syncsite` | Pull the SPBC website catalog into the shop |
| `/shops` | Switch the active shop |
| `/newvendor`, `/handover`, `/invitevendor` | Create a vendor shop and a one-time invite |
| `/sitepaid` | Tell the spbc-orders worker a website payment landed |
| `/track` | Tracking on a website order. The number's format overrides the carrier words |
| `/owed` | What SPBC owes a vendor, and a settle button |
| `/rescue` | Recovery kit after a bot ban |
| `/resend` | Re-DM the NEW ORDER notice |
| Catalog cleanup / clear inventory | Owner-only buttons. Cleanup renames. Clear wipes a shop catalog after confirm |

## Other pieces in this repo

- **SQLite multi-shop.** Each Telegram group or owner DM is a shop. `db.py`.
- **Franchise clones.** Shared stock, separate prices, hidden service fee folded into shipping, weekly invoices, `autobiller.py`.
- **Order router.** Quotes vendor shops for a paid SPBC website order. Unicorn shops are skipped. Offer, accept, decline. `order_router.py`.
- **Payables.** Vendor amount owed after a routed order. `payables.py`.
- **Site sync.** `springfieldpbc.com` `/api/products` into one shop. `site_sync.py`.
- **Notify HTTP.** `/notify`, `/health`, storefront JSON, order status. `spbc_notify.py`, `run_cloud.py`.
- **Web panel.** Magic-link catalog, orders, restock, backup download. `webpanel.py`.
- **Reservations.** Soft hold until confirm. `reservation_janitor.py`.
- **Telegram invoice payments.** Off unless `TELEGRAM_PAYMENT_PROVIDER_TOKEN` is set. Not Stars. `tg_payments.py`.
- **Token failover.** `BOT_TOKENS` standby pool. `token_pool.py`.
- **Encrypted backups.** `backup.py`. Live `inventory.db` is never overwritten by a restore script unless you run it on purpose.
- **Unicorn catalog helpers.** Title bind, storefront key from env, payment seed from env, name cleanup. `unicorn_shop.py`, `catalog_cleanup.py`, `unicorn_catalog.py`.
- **Supplier watch.** Separate Telethon process under `supplier_watch/`. It does not use the bot token.
- **Reference helpers added for the live bot to copy.** `pricing_rules.py`, `sku_normalizer.py`, `reorder_predictor.py`, `carrier_detect.py`. They are not wired into checkout.

## What to port to the Cloudflare bot

Check the live worker for each of these. Port the ones it does not already have.

1. **Admin allowlist, deny by default.** `ADMIN_TELEGRAM_IDS`. No admin, inventory, or order command without an explicit Telegram user ID.
2. **Pricing rules.** SPBC public price is Show Me Source cost plus $35 per vial and plus $250 per kit. Jekyll A. Hyde public price is twice the Show Me Source cost for a single and for a kit. Patriotic and franchisee prices stay the stored catalog price. Franchisee hidden fee stays on shipping, not on the vial price. `pricing_rules.py`.
3. **SKU normalizer.** `Reta 30 MG`, `R30`, and `R 30 MG` are `RETA-30`. `T30`, `Tirzepatide 30`, and `Tirzepitide` are `TIRZ-30`. `sku_normalizer.py`.
4. **Reorder predictor.** Mean gap from order dates, at least two orders. Nate's gaps 8, 5, 9, 6, 8 average 7.2 days; last order 2026-09-25 points at 2026-10-02. Flag anyone due within 3 days. `reorder_predictor.py`.
5. **Carrier detection.** `1Z…` is UPS even when the email says USPS (SMS-605, `1Z1J329C0217555166`). 20–22 digit numbers starting with 92, 93, or 94 are USPS. `carrier_detect.py`.
6. **Confirm-then-deduct stock**, including kit lines that deduct `KIT_SIZE` vials and charge `kit_price`.
7. **Low-stock alert** after a paid confirm.
8. **Tracking link** that does not require the full admin panel, and a website `/track` that classifies the number before it emails the customer.
9. **Encrypted inventory backup** if the Cloudflare bot has no equivalent vault.
10. **Franchise remittance and weekly service-fee invoices** only if those shops still bill through this process. Unicorn was cut off the SPBC quote path on purpose (`unicorn_shop.py`). Do not port "quote Unicorn for springfieldpbc.com orders."

Secrets for any port stay in the Cloudflare worker's secret store. Names are listed in `.env.example`. Values stay out of git.
