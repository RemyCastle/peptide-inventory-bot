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

# Schema version expected by this code release
SCHEMA_VERSION = 5
