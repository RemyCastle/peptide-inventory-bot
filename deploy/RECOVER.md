# Recover after a bot ban (multi-token + encrypted backup)

Same **code** + same **inventory DB** + new/standby **BotFather token**.

## What you already have

| Piece | Role |
|-------|------|
| Live `inventory.db` (`DB_PATH`) | Shops, stock, prices, payment methods, collab, franchise, orders |
| Encrypted vault (`BACKUP_DIR`) | `latest.enc` + dated/daily snapshots (~30 day retention) |
| `BOT_TOKENS` | Comma-separated standby tokens from different accounts |
| `token_state.json` | Which pool index is active after failover |

## Env vars (Render / `.env`)

```text
# Live token (or first in pool)
TELEGRAM_BOT_TOKEN=...

# Optional standby pool (preferred for autodeploy)
BOT_TOKENS=token_from_account_A,token_from_account_B,token_from_account_C
ACTIVE_BOT_INDEX=0
TOKEN_FAILOVER=1
TOKEN_STATE_PATH=/data/token_state.json

# Encrypted vault (set a strong passphrase — password manager only)
BACKUP_PASSPHRASE=long-random-secret
BACKUP_DIR=/data/backups
BACKUP_RETENTION_DAYS=30
DB_PATH=/data/inventory.db

# Optional public pointer after cutover
PUBLIC_BOT_USERNAME=YourNewBot
RECOVERY_URL=https://yoursite.example/qr.html
```

**Never commit tokens or `BACKUP_PASSPHRASE` to git.**

## Automatic failover (warm standby)

If `BOT_TOKENS` has 2+ tokens and `TOKEN_FAILOVER=1`:

1. Bot runs on active index (from `token_state.json` or `ACTIVE_BOT_INDEX`).
2. On invalid/banned token (401 / InvalidToken), process marks it dead and **starts the next token** against the **same DB**.
3. Update website/QR to the new `@username` (`PUBLIC_BOT_USERNAME`).
4. Re-add the **new** bot to shop groups if needed; admins `/start` the new bot once.

Cold DMs to people who never opened the new bot will not work until they Start it.

## Manual cutover (you tell Grok / yourself)

1. **Get latest vault file**  
   - Host: `/data/backups/latest.enc`  
   - Or owner DM: `/backup` then copy file off the server  
   - Laptop copy of `latest.enc` (best)

2. **Create or pick next BotFather token** (spare account preferred).

3. **Restore if live DB was lost** (skip if disk still has good `inventory.db`):

```powershell
cd C:\Users\Remy\peptide_inventory_bot
.\venv\Scripts\Activate.ps1
$env:BACKUP_PASSPHRASE="your-secret"
python scripts/restore_backup.py path\to\latest.enc
```

4. **Point env at new token** (keep same `DB_PATH` / restored DB):

```text
TELEGRAM_BOT_TOKEN=<new>
# or
BOT_TOKENS=<new>,<other spares>
ACTIVE_BOT_INDEX=0
```

5. **Restart** the service (Render deploy / `python bot.py`).

6. **Human reattach**  
   - Add new bot to each shop group  
   - Pin recovery link  
   - Update site QR / `PUBLIC_BOT_USERNAME`

7. Confirm: `/backup_status`, open catalog, check payment methods + stock.

## Owner commands

| Command | Who | What |
|---------|-----|------|
| `/backup` | Owner | Write encrypted snapshot now |
| `/backup_status` | Owner | List vault files + pool size |

Paid **Confirm** also writes a snapshot when `BACKUP_PASSPHRASE` is set.

## What restores with the DB

- All shops, products, **stock**, **prices**  
- **Payment methods** (cashtag, addresses, instructions)  
- Admins, orders, collab shares, franchise links, settlements  

## What you must re-do on Telegram

- New bot username / deep links  
- Group membership for the new bot  
- Telegram `file_id` media (COA photos) may need re-upload; `coa_url` still works  

## Laptop vault habit

1. Keep `BACKUP_PASSPHRASE` only in a password manager.  
2. Copy `latest.enc` (or whole `backups/`) to the laptop weekly (or after big stock changes).  
3. Retention on host is ~30 days; laptop can keep its own copies longer if you want.

## Indefinite redeploys

You can repeat this forever: new/standby token + restore/same DB + same code.  
Pre-stage 2–3 tokens so you are not creating bots mid-outage.
