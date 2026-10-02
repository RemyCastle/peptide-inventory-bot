# Peptide Inventory Telegram Bot

> **Superseded.** The live Unicorn bot runs on Cloudflare from a codebase that is not in this GitHub repo. This Python project is a reference library. Do not deploy it. Feature inventory and what to port: [docs/LEGACY.md](docs/LEGACY.md). Secret-scan report: [docs/SECRET-SCAN.md](docs/SECRET-SCAN.md).

Multi-shop inventory bot: catalog, cart, checkout, payment options, per-shop admins, and **inventory deduction only after admin confirms payment**. Shipping fee is auto-added at checkout.

---

## Features

| Who | What |
|-----|------|
| **Customers** | Browse catalog, cart, checkout, pick payment method, enter shipping, mark “I've paid” |
| **Admins (per shop)** | Products (price/stock), payment methods, shipping rules, confirm/reject orders, add admins |
| **Owners** | `OWNER_IDS` in `.env` — full access to every shop; can `/setup` group shops |

### Order lifecycle

1. Customer checks out → order status `pending_payment` (**stock not reduced**)
2. Customer pays using instructions → taps **I've paid** → `awaiting_confirmation`
3. Admin **Confirm paid** → status `paid` → **stock reduced**
4. Or admin **Reject** / customer cancel → no stock change

### Shipping auto add-on

- Flat fee per shop (default `$8`)
- Optional free-shipping threshold (default `$150`)
- Toggle shipping on/off in Admin → Shipping

---

## Always on (free for a bit) + still editable

Code lives on GitHub (edit anytime):  
**https://github.com/RemyCastle/peptide-inventory-bot**

| Goal | How |
|------|-----|
| Run 24/7 free-ish | Deploy to [Railway](https://railway.app/new/github) or Render from that repo |
| Keep editing | Change files here → `git push` → host redeploys |
| Secrets | Host env vars only (`.env` is gitignored) |
| Local click-to-run | Desktop **Run Inventory Bot** or `start.bat` |

Double-click **`OPEN_FREE_HOSTING.bat`** for the setup links.  
Details: [`deploy/ALWAYS_ON.md`](deploy/ALWAYS_ON.md)

**Only one runner at a time** (cloud *or* local), or Telegram will conflict.

### Ban recovery / multi-token standby

- Put spare BotFather tokens in `BOT_TOKENS` (different accounts OK). On invalid/banned token the process can fail over to the next while keeping the **same** `inventory.db`.
- Set `BACKUP_PASSPHRASE` + `BACKUP_DIR` (`/data/backups` on Render) for encrypted snapshots (`latest.enc`) after each paid confirm, plus owner `/backup`.
- Owner webpanel **Settings → Download latest.enc**, then `.\scripts\pull-vault.ps1` into `C:\Users\Remy\peptide_inventory_bot\backups`. Never overwrites `inventory.db`.
- Restore: `python scripts/restore_backup.py backups/latest.enc` — see [`deploy/RECOVER.md`](deploy/RECOVER.md).

---

## Quick start (Windows)

### 1. Create a bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the token

### 2. Get your Telegram user ID

Message [@userinfobot](https://t.me/userinfobot) and copy your ID.

### 3. Configure

```powershell
cd C:\Users\Remy\peptide_inventory_bot
copy .env.example .env
```

Edit `.env`:

```
TELEGRAM_BOT_TOKEN=123456:ABC...
OWNER_IDS=YOUR_TELEGRAM_USER_ID
BRAND_NAME=UnicornFartzzBot
```

### 4. Run

Double-click `start.bat`, or:

```powershell
cd C:\Users\Remy\peptide_inventory_bot
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

### 5. First-time setup

**Option A — personal shop (DM)**  
Open the bot → `/start` as owner → personal shop is created → **Admin Panel**.

**Option B — group shop**  
1. Add the bot to your Telegram group  
2. In the group: `/setup` (owner only)  
3. DM the bot → `/start` → Admin Panel  
4. Share the **Shop link** from Admin with customers

### 6. Stock the shop

In Admin Panel:

1. **Add product** (name, price, stock, description)
2. **Payments** → add Cash App / Zelle / crypto + instructions
3. **Shipping** → set fee and free threshold
4. **Admins** → add other admins by Telegram user ID

---

## Customer commands

| Command | Description |
|---------|-------------|
| `/start` | Menu (or `?start=shop_<id>` deep link) |
| `/catalog` | Product list |
| `/cart` | View cart / checkout |
| `/orders` | Your order history |
| `/help` | Help |

---

## Admin commands

| Command | Description |
|---------|-------------|
| `/admin` | Admin panel |
| `/setup` | Initialize shop in a group (owner) |
| `/cancel` | Abort multi-step input |

---

## Project layout

```
peptide_inventory_bot/
├── bot.py           # Telegram handlers
├── db.py            # SQLite models & order logic
├── config.py        # Env config
├── inventory.db     # Created on first run
├── requirements.txt
├── .env.example
├── start.bat
└── README.md
```

---

## Environment

Names only. Put values in the host secret store or a local `.env` that is gitignored. `.env.example` lists the same names.

Admin commands (`/admin`, `/setup`, inventory edits, order confirm, `/master`, `/backup`, `/track`, and the rest in [docs/LEGACY.md](docs/LEGACY.md)) require `ADMIN_TELEGRAM_IDS`. If that variable is empty, every admin command is denied.

| Name | Role |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token for this process |
| `BOT_TOKENS` | Comma-separated standby tokens |
| `ACTIVE_BOT_INDEX` | Which standby token is first |
| `TOKEN_FAILOVER` | Advance to the next token when the current one is rejected |
| `TOKEN_STATE_PATH` | File that remembers the active index |
| `OWNER_IDS` | Global owner user IDs |
| `ADMIN_TELEGRAM_IDS` | Allowlist for admin commands. Empty denies everyone |
| `OWNER_TELEGRAM_CHAT_ID` | Owner chat for alerts |
| `SUPPLIER_TELEGRAM_CHAT_ID` | Fallback supplier chat |
| `UNICORN_BOT_TOKEN` | Unicorn vendor bot token |
| `UNICORN_EXTRA_BOT_TOKENS` | Extra Unicorn tokens |
| `UNICORN_NOTIFY_IDS` | Extra Unicorn notify user IDs |
| `UNICORN_SHOP_CHAT_ID` | Unicorn shop id |
| `UNICORN_STORE_URL` | Mini App URL |
| `UNICORN_STOREFRONT_KEY` | 24-hex Pages catalog invite |
| `UNICORN_CLAIM_TOKEN` | One-time shop claim |
| `UNICORN_VENMO_HANDLE` | Seeded Venmo handle |
| `UNICORN_PAYPAL_EMAIL` | Seeded PayPal address |
| `UNICORN_PAYPAL_NETWORK` | PayPal note, default `friends_family` |
| `MASTER_VENMO` | Platform-fee Venmo |
| `NOTIFY_SECRET` | Shared secret for `POST /notify` |
| `SPBC_ORDERS_URL` | spbc-orders worker base URL |
| `SPBC_ORDERS_ADMIN_TOKEN` | Admin token for that worker |
| `SPBC_SITE_URL` | Storefront whose `/api/products` is synced |
| `SPBC_SHOP_CHAT_ID` | Shop that receives the sync |
| `SITE_SYNC_INTERVAL_MIN` | Auto-sync interval. `0` is manual only |
| `PANEL_BASE_URL` | Public URL for magic links |
| `VENDOR_STORES_JSON` | Vendor bot definitions |
| `TELEGRAM_PAYMENT_PROVIDER_TOKEN` | Physical-goods invoice provider. Empty disables invoices |
| `BACKUP_PASSPHRASE` | Encrypts vault snapshots |
| `BACKUP_DIR` | Vault directory |
| `BACKUP_RETENTION_DAYS` | How long snapshots are kept |
| `DB_PATH` | SQLite file |
| `LOG_PATH` | Log file |
| `MEDIA_DIR` | Panel uploads |
| `BRAND_NAME` | Default shop brand |
| `CURRENCY` | Currency code |
| `CURRENCY_SYMBOL` | Symbol |
| `WELCOME_TEXT` | Default welcome |
| `DEFAULT_SHIPPING_FEE` | New-shop shipping fee |
| `DEFAULT_FREE_SHIPPING_ABOVE` | New-shop free-shipping threshold |
| `DEFAULT_LOW_STOCK_THRESHOLD` | Alert at or below this stock |
| `DEFAULT_MIN_ORDER_QTY` | New-shop minimum. `0` disables it |
| `DEFAULT_MIN_ORDER_LABEL` | `vial` or `kit` |
| `KIT_SIZE` | Vials per kit |
| `CATALOG_TOP_N` | How many products the open catalog shows |
| `RESERVATION_HOLD_MINUTES` | Soft checkout hold |
| `RESERVATION_JANITOR_TICK_SECONDS` | How often holds are expired |
| `RESERVATION_JANITOR_BOOT_DELAY_SECONDS` | Delay before the first expiry pass |
| `VENDOR_OFFER_TTL_MIN` | How long a vendor has to accept an offer |
| `AUTOBILL_TICK_SECONDS` | Weekly invoice tick |
| `AUTOBILL_BOOT_DELAY_SECONDS` | Delay before the first invoice tick |
| `PUBLIC_BOT_USERNAME` | Username shown after a cutover |
| `RECOVERY_URL` | Public recovery link |
| `ONESHOT_CATALOG_STOCK` | One-shot stock rewrite flag |
| `TG_API_ID` | supplier_watch Telegram API id |
| `TG_API_HASH` | supplier_watch Telegram API hash |
| `SW_SESSION_PATH` | supplier_watch session file |
| `SW_DB_PATH` | supplier_watch database |
| `SW_SUPPLIERS_PATH` | supplier_watch supplier list |
| `OLLAMA_URL` | supplier_watch local model URL |
| `SW_OLLAMA_MODEL` | supplier_watch model name |
| `SW_OLLAMA_TIMEOUT` | supplier_watch model timeout |
| `SW_PARSE_MODE` | `llm_first` or `regex_only` |
| `SW_ALERT_MIN_CHANGE_PCT` | supplier_watch price-change threshold |

Pricing, SKU names, reorder dates, and carrier detection live in `pricing_rules.py`, `sku_normalizer.py`, `reorder_predictor.py`, and `carrier_detect.py`.

## Notes

- **Multi-shop**: each Telegram group (or owner DM) is a separate shop with its own products, prices, inventory, admins, and payment methods.
- **Soft stock check**: stock is checked at order creation and again at payment confirm. Only confirm deducts.
- Run only **one** instance of the bot (Telegram allows a single long-poll client per token).

---

## Disclaimer

For research-product inventory tooling only. You are responsible for compliance with Telegram terms and all applicable laws in your jurisdiction.
