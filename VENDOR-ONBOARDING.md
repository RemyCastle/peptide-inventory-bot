# Vendor onboarding — mini-app store + SPBC inventory bot

Recipe to add **vendor N+1** so their branded Telegram store bot places orders
into the shared `inventory.db` on **spbc-inventory-bot** (Render).

This is the production path used by Unicorn Magic Factory and any future
vendor. Do not invent a second order pipeline.

---

## Architecture (one sentence)

Each vendor gets a **BotFather bot** + a **Cloudflare Pages mini-app**. The
Pages store reads stock/prices from `GET /storefront?invite=…` on the Render
service; checkout posts the cart back via Telegram `web_app_data`. A daemon
thread in `vendor_stores.py` (started from `run_cloud.py`) long-polls that
vendor bot, calls `db.create_order` on the bound shop, and DMs the owner.

---

## Fee policy

| Who | Per-order platform fee |
|-----|------------------------|
| Default (every new vendor) | **$2.00** |
| Unicorn Magic Factory (legacy) | **$1.00** |
| Override | `"order_fee": <number>` on the vendor’s `VENDOR_STORES_JSON` entry |

- Fee is stored on the shop as `shops.hidden_service_fee`.
- On every order, `franchise.customer_shipping_total` **folds** it into
  `shipping_fee` so the customer only sees one shipping total (never a
  “platform fee” line).
- Boot seeding (`vendor_stores._ensure_order_fee`) only writes when the shop’s
  current fee is `0` and config fee is `> 0`. A fee already set (via
  `/master` or a previous seed) is left alone.
- To permanently disable a shop’s fee: set `"order_fee": 0` in
  `VENDOR_STORES_JSON` (explicit zero skips seeding). Setting fee to `0` only
  in Telegram can be re-seeded on restart if config still says `$2`.

Weekly rollup: paid orders with `hidden_service_fee > 0` →
`franchise.generate_weekly_invoices` (UTC Mon 00:00 → next Mon).

---

## Env vars (Render: spbc-inventory-bot)

| Key | Required | Purpose |
|-----|----------|---------|
| `TELEGRAM_BOT_TOKEN` | yes | Main SPBC inventory bot |
| `OWNER_IDS` | yes | Comma-separated master Telegram user ids |
| `OWNER_TELEGRAM_CHAT_ID` | yes | Added to every vendor’s notify list |
| `DB_PATH` | yes | `/data/inventory.db` on Render disk |
| `PANEL_BASE_URL` | yes | Public service URL (e.g. `https://spbc-inventory-bot.onrender.com`) — panel links + handoff |
| `VENDOR_STORES_JSON` | for mini-app vendors | JSON array of vendor receivers (see below) |
| `UNICORN_BOT_TOKEN` | legacy | First vendor token if not in JSON |
| `UNICORN_CLAIM_TOKEN` | legacy | Invite body (or `vendor…` prefix) for Unicorn storefront bind |
| `UNICORN_SHOP_CHAT_ID` | optional | Force Unicorn shop; else title/virtual match |
| `UNICORN_STORE_URL` | optional | Default `https://remy-miniapp-demos.pages.dev/unicorn/` |
| `UNICORN_NOTIFY_IDS` | optional | Extra DM targets (comma-separated chat ids) |
| `NOTIFY_SECRET` | SPBC site | Worker → bot notify auth |
| `BACKUP_PASSPHRASE` | recommended | Encrypted vault |

**Never commit tokens.** Edit only in the Render dashboard (or local `.env` for
scratch tests). Local tests must set `DB_PATH` to a **scratch** file — never
point tests at production `/data/inventory.db`.

### `VENDOR_STORES_JSON` shape

```json
[
  {
    "name": "Unicorn Magic Factory",
    "emoji": "🦄",
    "token": "123456:ABC-DEF...",
    "invite": "3a9eee77166edc67b4cbb94d",
    "shop_chat_id": null,
    "store_url": "https://remy-miniapp-demos.pages.dev/unicorn/",
    "notify_ids": [],
    "order_fee": 1.0,
    "welcome": "optional custom /start text"
  },
  {
    "name": "Vendor Two",
    "emoji": "🧬",
    "token": "789:XYZ...",
    "invite": "aabbccddeeff001122334455",
    "store_url": "https://remy-miniapp-demos.pages.dev/vendor-two/",
    "order_fee": 2.0
  }
]
```

| Field | Notes |
|-------|--------|
| `token` | BotFather token for **their** store bot (not the SPBC bot) |
| `invite` | 24-char hex from `/handover` or `/invitevendor` (with or without `vendor` / `vendor_` prefix) |
| `shop_chat_id` | Optional override; wins over invite lookup |
| `store_url` | Mini-app HTTPS URL opened by the bot’s Web App button |
| `order_fee` | Platform fee seed; omit → $2 default ($1 if name contains “unicorn”) |
| `notify_ids` | Extra chat ids for new-order DMs; owner is always included |

JSON entries **win** over legacy `UNICORN_*` when the bot token is the same.

---

## Recipe: add vendor N+1

### 1. BotFather — vendor store bot

1. Open [@BotFather](https://t.me/BotFather) → `/newbot`.
2. Name + username (e.g. `Acme Research Shop` / `acme_research_bot`).
3. Copy the **HTTP API token** — that is `token` in JSON.
4. Optional polish (after the mini-app URL exists):
   - **Menu button**: BotFather → Bot Settings → Menu Button → set URL to
     `store_url` (same as the Web App).
   - **Main Mini App** (if offered in BotFather for this bot): same URL.
5. Have the vendor (or you) open the bot once and send `/myid` if you need
   their chat id for `notify_ids`.

### 2. Pre-build the shop inventory (SPBC main bot)

On the **main** inventory bot (owner only):

```
/newvendor Acme Research
```

- Creates a **virtual shop**, switches you onto it.
- Add products, prices, kit prices, stock, photos/COAs (Telegram admin or
  `/webpanel` if `PANEL_BASE_URL` is set).
- Seed payment methods (Venmo / PayPal F&F / Zelle / Apple Cash / crypto /
  Cash App) in the panel or Admin → Payments.

When stocked:

```
/handover <shop_chat_id>
```

You get a one-time link:

```
https://t.me/<MAIN_BOT_USERNAME>?start=vendor_<24hex>
```

The **24 hex characters** (after `vendor_`) are the `invite` value for
`VENDOR_STORES_JSON` and for the Pages storefront query string.

Send the handoff link to the vendor so they claim admin on that shop.
**Storefront + order receiver do not require claim** — they only need
`vendor_invites.shop_chat_id`, which boot binding and `/handover` set.

### 3. Mini-app store (Cloudflare Pages)

Stores live under **remy-miniapp-demos.pages.dev** (out of scope to edit here).

1. **Clone** an existing vendor store folder (e.g. `unicorn/`) to a new path
   (e.g. `acme/`).
2. Point the store’s catalog fetch at:

   ```
   https://<PANEL_BASE_URL>/storefront?invite=<24hex>
   ```

   Example:

   ```
   https://spbc-inventory-bot.onrender.com/storefront?invite=aabbccddeeff001122334455
   ```

3. Deploy Pages. Confirm CORS works (API sends `Access-Control-Allow-Origin: *`).
4. Smoke-check:

   ```bash
   curl -sS "https://spbc-inventory-bot.onrender.com/storefront?invite=YOUR_HEX" | head
   ```

   Expect `"ok": true`, products, payments names.

### 4. Render env — register the receiver

Add (or append) the vendor object to `VENDOR_STORES_JSON` on
**spbc-inventory-bot**. Include at least:

- `name`, `emoji`, `token`, `invite`, `store_url`
- `order_fee`: `2.0` unless you negotiated otherwise

Save → **Manual Deploy** (or restart) so:

1. `run_cloud._bind_vendor_miniapps` rebinds invite → stocked shop
2. `vendor_stores.start_all` starts a polling thread for the new bot
3. `_ensure_order_fee` seeds `$2` (or override) onto `hidden_service_fee`

### 5. End-to-end smoke (scratch or prod)

1. Open the vendor bot → `/start` → **Open the Store**.
2. Add vial + kit lines → place order.
3. Confirm you get the order DM; customer sees payment code + methods.
4. Confirm order total’s shipping includes base shipping + platform fee.
5. Admin marks paid → fee is eligible for the weekly invoice.

**Do not** run automated tests against the production DB. Locally:

```powershell
$env:DB_PATH = "$PWD\scratch_test.db"
.\venv\Scripts\python.exe -m pytest tests/ -q
.\venv\Scripts\python.exe -c "import run_cloud"
```

---

## Weekly invoices (master only)

| Path | What it does |
|------|----------------|
| `/invoices` | Generate this UTC week’s invoices + list all **open** invoices; DMs summary if run from a group |
| `/master` → Generate weekly invoices | Same generate via buttons |
| `/master` → Open invoices → Mark paid | Clear an invoice after vendor remits |

Implementation: `franchise.generate_weekly_invoices` /
`franchise.list_invoices` / `franchise.mark_invoice_paid`.

Rollup rule: orders with `status = 'paid'`, `paid_at` in the UTC week, and
`hidden_service_fee > 0`, summed per shop.

---

## Boot order (why resume-on-Render just works)

`run_cloud.main()`:

1. Start `spbc_notify` HTTP (`/`, `/health`, `/storefront`, `/panel`, `/notify`, …)
2. `_bind_vendor_miniapps()` — invite → shop (+ Unicorn payment seed)
3. `vendor_stores.start_all()` — one thread per configured vendor bot
4. `bot.main()` — main SPBC Telegram bot polling

If invite rebinding finds a prior shop that is empty or not a title/virtual
match, it **rebinds** to the stocked vendor shop (avoids leaking the main
SPBC catalog into a vendor mini-app).

---

## Checklist (copy/paste)

- [ ] BotFather bot created; token saved
- [ ] `/newvendor …` + stock + payments + shipping
- [ ] `/handover <id>` → invite hex recorded
- [ ] Pages store cloned; `storefront?invite=` wired; deployed
- [ ] BotFather menu button / Main Mini App → `store_url` (optional polish)
- [ ] `VENDOR_STORES_JSON` entry with `order_fee` (default 2)
- [ ] Render redeploy / restart
- [ ] `curl` `/storefront?invite=…` OK
- [ ] Test order vial + kit; owner DM received
- [ ] `/invoices` after a paid test order shows the fee

---

## Code map

| Piece | File |
|-------|------|
| Multi-vendor receivers + fee seed | `vendor_stores.py` |
| Cloud boot + mini-app bind | `run_cloud.py` |
| Public catalog API + CORS | `spbc_notify.py` → `webpanel.api_storefront` |
| Invite bind / rebind | `webpanel.ensure_miniapp_storefront` |
| Fee fold into shipping | `franchise.customer_shipping_total` + `db.create_order` |
| Weekly invoices | `franchise.generate_weekly_invoices` |
| Owner commands | `bot.py` `/master`, `/invoices`, `/newvendor`, `/handover`, `/invitevendor` |
| Typed payment rails | `payment_templates.py` + panel in `webpanel.py` |
