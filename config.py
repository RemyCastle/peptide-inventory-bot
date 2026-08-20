"""Instance configuration loaded from environment (one deploy = one bot instance).

Per-shop (multi-tenant) settings live in the `shops` table and override these
defaults when set. Spin up a new white-label customer by copying the project
and editing `.env` — no code changes required.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "inventory.db")))
LOG_PATH = Path(os.getenv("LOG_PATH", str(BASE_DIR / "bot.log")))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Standby pool: comma-separated BotFather tokens (different accounts OK).
# First usable token wins; on ban/invalid, failover advances the index.
# If unset, TELEGRAM_BOT_TOKEN alone is used.
BOT_TOKENS_RAW = os.getenv("BOT_TOKENS", "").strip()

try:
    ACTIVE_BOT_INDEX = int(os.getenv("ACTIVE_BOT_INDEX", "0"))
except ValueError:
    ACTIVE_BOT_INDEX = 0

# Persist which standby token is live across restarts
TOKEN_STATE_PATH = Path(
    os.getenv("TOKEN_STATE_PATH", str(BASE_DIR / "token_state.json"))
)

# When true, invalid/banned token advances to the next BOT_TOKENS entry and restarts
TOKEN_FAILOVER = os.getenv("TOKEN_FAILOVER", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Public pointer after cutover (website/QR). Optional.
PUBLIC_BOT_USERNAME = os.getenv("PUBLIC_BOT_USERNAME", "").strip().lstrip("@")
RECOVERY_URL = os.getenv("RECOVERY_URL", "").strip()

# Encrypted backup vault (live DB stays at DB_PATH; vault is for restore)
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(BASE_DIR / "backups")))
BACKUP_PASSPHRASE = os.getenv("BACKUP_PASSPHRASE", "").strip()
try:
    BACKUP_RETENTION_DAYS = max(1, int(os.getenv("BACKUP_RETENTION_DAYS", "30")))
except ValueError:
    BACKUP_RETENTION_DAYS = 30

# Comma-separated Telegram user IDs with global owner privileges
_owner_raw = os.getenv("OWNER_IDS", "").strip()
OWNER_IDS: set[int] = set()
for part in _owner_raw.split(","):
    part = part.strip()
    if part.isdigit():
        OWNER_IDS.add(int(part))

# Instance-level branding defaults (overridable per shop in DB)
BRAND_NAME = os.getenv("BRAND_NAME", "UnicornFartzzBot")
CURRENCY = os.getenv("CURRENCY", "USD")
CURRENCY_SYMBOL = os.getenv("CURRENCY_SYMBOL", "$")
WELCOME_TEXT = os.getenv(
    "WELCOME_TEXT",
    "Browse the catalog, add items to your cart, and checkout when ready.",
).strip()

# Soft hold: still only deduct stock on admin confirm
DEFAULT_SHIPPING_FEE = float(os.getenv("DEFAULT_SHIPPING_FEE", "8.00"))
DEFAULT_FREE_SHIPPING_ABOVE = float(os.getenv("DEFAULT_FREE_SHIPPING_ABOVE", "150.00"))

# Alert admins when stock falls to this level or below after a confirm
DEFAULT_LOW_STOCK_THRESHOLD = int(os.getenv("DEFAULT_LOW_STOCK_THRESHOLD", "2"))

# Order minimum (sum of cart quantities). 0 = disabled. Label is "vial" or "kit".
DEFAULT_MIN_ORDER_QTY = int(os.getenv("DEFAULT_MIN_ORDER_QTY", "0"))
DEFAULT_MIN_ORDER_LABEL = os.getenv("DEFAULT_MIN_ORDER_LABEL", "vial").strip() or "vial"

# Kit pricing: one kit = this many vials (stock units)
try:
    KIT_SIZE = max(2, int(os.getenv("KIT_SIZE", "10")))
except ValueError:
    KIT_SIZE = 10

# Buyer catalog open view: only top N by paid sales; rest via search
try:
    CATALOG_TOP_N = max(1, int(os.getenv("CATALOG_TOP_N", "10")))
except ValueError:
    CATALOG_TOP_N = 10

# ── SPBC website integration (supplier notify + catalog sync) ──────────────
# Shared secret with the spbc-orders Cloudflare worker (X-Notify-Secret).
NOTIFY_SECRET = os.getenv("NOTIFY_SECRET", "").strip()
# Fallback chat when a supplier has no telegram_chat_id in the payload.
SUPPLIER_TELEGRAM_CHAT_ID = os.getenv("SUPPLIER_TELEGRAM_CHAT_ID", "").strip()
# Where supplier totals / OOS answers are forwarded (defaults to supplier chat).
OWNER_TELEGRAM_CHAT_ID = (
    os.getenv("OWNER_TELEGRAM_CHAT_ID", "").strip() or SUPPLIER_TELEGRAM_CHAT_ID
)
# Storefront base URL whose /api/products feeds the Telegram catalog.
SPBC_SITE_URL = os.getenv("SPBC_SITE_URL", "").strip()
# Bot shop (chat id) that mirrors the website catalog. 0 = sync disabled.
try:
    SPBC_SHOP_CHAT_ID = int(os.getenv("SPBC_SHOP_CHAT_ID", "0"))
except ValueError:
    SPBC_SHOP_CHAT_ID = 0
# Auto-sync interval in minutes. 0 = manual /syncsite only.
try:
    SITE_SYNC_INTERVAL_MIN = int(os.getenv("SITE_SYNC_INTERVAL_MIN", "0"))
except ValueError:
    SITE_SYNC_INTERVAL_MIN = 0

# Public base URL of this service (for vendor web panel links).
# e.g. https://spbc-supplier-bot.onrender.com — empty disables /webpanel links.
PANEL_BASE_URL = os.getenv("PANEL_BASE_URL", "").strip().rstrip("/")

# spbc-orders worker admin API — lets the owner confirm a payment from
# Telegram when the Gmail receipt matcher misses one.
SPBC_ORDERS_URL = os.getenv(
    "SPBC_ORDERS_URL", "https://spbc-orders.spbc.workers.dev"
).strip().rstrip("/")
SPBC_ORDERS_ADMIN_TOKEN = os.getenv("SPBC_ORDERS_ADMIN_TOKEN", "").strip()
# Where panel-uploaded product photos / COA files are stored (persistent disk)
MEDIA_DIR = os.getenv("MEDIA_DIR", str(DB_PATH.parent / "uploads"))

# Master Venmo handle for vendor platform-fee invoices (weekly autobill + /invoices).
MASTER_VENMO = (os.getenv("MASTER_VENMO", "") or "").strip() or "@remycastle"

# Soft checkout hold (minutes). Real stock still deducts only on admin confirm.
try:
    RESERVATION_HOLD_MINUTES = max(1, int(os.getenv("RESERVATION_HOLD_MINUTES", "30")))
except ValueError:
    RESERVATION_HOLD_MINUTES = 30

# Telegram Payments provider token for physical goods (Stripe / Smart Glocal / etc.).
# Empty = invoices disabled. Never use Telegram Stars (XTR / empty provider).
# Do not log this value.
TELEGRAM_PAYMENT_PROVIDER_TOKEN = os.getenv(
    "TELEGRAM_PAYMENT_PROVIDER_TOKEN", ""
).strip()

# Schema version expected by this code release
SCHEMA_VERSION = 11


def resolve_bot_tokens() -> list[str]:
    """Ordered sales-bot tokens (standby pool + legacy single token)."""
    from token_pool import parse_tokens

    return parse_tokens(BOT_TOKENS_RAW, TELEGRAM_BOT_TOKEN)
