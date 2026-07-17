# Keep the bot online (free-ish) + still editable

## What “always on + editable” means

| Piece | Where |
|--------|--------|
| **Running 24/7** | Free cloud host (Railway trial / Render free / similar) |
| **Editable code** | This folder + GitHub — change files, push, redeploy |
| **Secrets** | Host **Environment Variables** only (never commit `.env`) |
| **Shop data** | SQLite on a **persistent volume** (`/data`) when the host supports it |

Local laptop is great for development. For buyers messaging at 3am, use cloud.

---

## Recommended free path: Railway (trial / credits)

1. Create a free account: https://railway.app  
2. **New Project → Deploy from GitHub** → pick `peptide-inventory-bot`  
3. Add variables:

```text
TELEGRAM_BOT_TOKEN=...
# Optional spares for ban failover (same DB):
# BOT_TOKENS=token_a,token_b,token_c
# TOKEN_FAILOVER=1
OWNER_IDS=your_telegram_id
BRAND_NAME=YourShopName
DB_PATH=/data/inventory.db
LOG_PATH=/data/bot.log
# Encrypted vault (paid confirm + /backup):
# BACKUP_PASSPHRASE=long-secret
# BACKUP_DIR=/data/backups
# BACKUP_RETENTION_DAYS=30
```

Ban recovery runbook: [`RECOVER.md`](RECOVER.md)

4. Attach a **volume** mounted at `/data` (so inventory.db survives restarts)  
5. Deploy  

**Important:** Stop the local bot (close the black window) so only **one** instance polls Telegram.

### Edit later
```powershell
cd C:\Users\Remy\peptide_inventory_bot
# edit bot.py / etc.
git add -A
git commit -m "Update bot"
git push
```
Railway auto-redeploys if connected to the repo.

---

## Alternative: Render free worker

1. https://render.com → New → Blueprint → this repo (`render.yaml`)  
2. Set env vars in dashboard  
3. Free workers can sleep / limited — fine for testing, flaky for real shops  

---

## Local “always on while PC is awake”

Double-click **Run Inventory Bot** on the Desktop, or:

```powershell
cd C:\Users\Remy\peptide_inventory_bot
.\start.bat
```

Not true 24/7 unless the PC stays on.

---

## Don’t run two copies

Telegram only allows **one** `getUpdates` poller.  
Cloud **or** local — not both.
