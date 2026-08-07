# Cutover: spbc-supplier-bot (Node) → combined inventory bot (Python)

The Python bot now does everything the Node notifier did (`/notify`,
`/resolve-chat`, `/recent-chats`, supplier total/OOS Q&A) **plus** Telegram
sales and website catalog sync. One Render service, same URL, so the
spbc-orders worker needs **zero changes**.

## Before you start

- [ ] Copy every env var off the old `spbc-supplier-bot` Render service
      (Dashboard → Environment) somewhere safe, and note the old repo name
      (for rollback).
- [ ] **Stop any locally running bot with the same token** (`peptide_inventory_bot`
      watchdog or start.bat). Two pollers on one token = Telegram 409 Conflict
      and missed orders. The unicorn token local runner can keep running —
      different token.
- [ ] Push this repo to GitHub: `RemyCastle/peptide-inventory-bot`.

## Swap the service

1. Render Dashboard → `spbc-supplier-bot` → **Settings → Build & Deploy**:
   change the repository to `RemyCastle/peptide-inventory-bot`, branch `master`
   (this repo uses master, not main), runtime **Docker** (Dockerfile at repo root).
2. **Disks**: add a disk, mount path `/data`, 1 GB.
3. **Environment** — keep the existing four, add the rest:

   | Key | Value |
   |-----|-------|
   | `TELEGRAM_BOT_TOKEN` | (keep — SPBC bot token) |
   | `NOTIFY_SECRET` | (keep) |
   | `OWNER_TELEGRAM_CHAT_ID` | (keep) |
   | `SUPPLIER_TELEGRAM_CHAT_ID` | (keep) |
   | `OWNER_IDS` | your numeric Telegram user id |
   | `BRAND_NAME` | `SPBC Shop` |
   | `DB_PATH` | `/data/inventory.db` |
   | `LOG_PATH` | `/data/bot.log` |
   | `BACKUP_DIR` | `/data/backups` |
   | `TOKEN_STATE_PATH` | `/data/token_state.json` |
   | `BACKUP_PASSPHRASE` | long random secret (password manager only) |
   | `SPBC_SITE_URL` | `https://springfieldpbc.com` |
   | `SPBC_SHOP_CHAT_ID` | see "Create the SPBC shop" below |
   | `SITE_SYNC_INTERVAL_MIN` | `360` |

4. Manual Deploy → **Deploy latest commit**. Health check path stays `/`.

## Create the SPBC shop

The sales catalog lives in a bot "shop" keyed by a chat id.

- DM the bot `/start` → it creates your personal shop (chat id = your user id), or
- add the bot to a Telegram group and run `/setup` (chat id = the group id,
  negative number).

Put that chat id in `SPBC_SHOP_CHAT_ID` and redeploy. Then as owner run
`/syncsite` — the website's 26 products appear with **stock 0**. Set real
stock in Admin → Products (customers can't buy at stock 0).

## Verify

- [ ] `https://<service>.onrender.com/health` → `{"ok":true,...,"notify_secret_configured":true}`
- [ ] Test owner alert (same as before):

```bash
curl -X POST https://YOUR-SERVICE.onrender.com/notify -H "Content-Type: application/json" -H "X-Notify-Secret: YOUR_SECRET" -d "{\"order_number\":\"PEP-TEST\",\"status\":\"pending\",\"customer_name\":\"Test\",\"total_cents\":15000,\"items\":[{\"name\":\"RETA 35 MG (Vial)\",\"qty\":3}]}"
```

- [ ] Phone buzzes with the PEP-TEST order.
- [ ] `/start` the bot in Telegram → shop menu appears.
- [ ] `/syncsite` → catalog matches the website.
- [ ] Place a real website test order → owner alert; mark paid in SPBC admin →
      supplier messages + total/OOS questions still work.

## Rollback

Settings → Build & Deploy → point the repository back at the old Node repo and
redeploy. Env vars are unchanged, so the old notifier resumes as-is.

## Notes

- Auto-sync runs at startup (+20 s) and every 6 h; it DMs the first `OWNER_IDS`
  only when something changed or failed. Manual: `/syncsite` (owner-only).
- Site is source of truth for names/prices/active; **stock stays bot-managed**.
  Products removed from the site are deactivated, never deleted.
- Telegram sales use the bot's own flow (payment ref → proof → admin confirm →
  stock deduct). Website orders keep flowing through spbc-orders → `/notify`.
