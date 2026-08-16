"""Supplier watch configuration (separate process from the shop bot).

Reads the same .env as the inventory bot if present, plus its own vars.
The Telethon session lives OUTSIDE the project tree (sync/backup safety):
%LOCALAPPDATA%\\supplier_watch\\ by default.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")
load_dotenv(BASE_DIR / ".env", override=True)

# --- Telegram user session (my.telegram.org) ---
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()

_local = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "supplier_watch"
_local.mkdir(parents=True, exist_ok=True)
SESSION_PATH = Path(os.getenv("SW_SESSION_PATH", str(_local / "remy_watch")))

# --- Storage (own DB file; the shop bot's inventory.db is never touched) ---
DB_PATH = Path(os.getenv("SW_DB_PATH", str(BASE_DIR / "supplier_watch.db")))

# --- Supplier chat whitelist ---
SUPPLIERS_PATH = Path(os.getenv("SW_SUPPLIERS_PATH", str(BASE_DIR / "suppliers.json")))

# --- Parsing ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("SW_OLLAMA_MODEL", "qwen-coder-opt")
OLLAMA_TIMEOUT = int(os.getenv("SW_OLLAMA_TIMEOUT", "180"))  # CPU inference is slow
# llm_first (default) | regex_only (no Ollama dependency)
PARSE_MODE = os.getenv("SW_PARSE_MODE", "llm_first").strip().lower()

# --- Alerts ---
ALERT_MIN_CHANGE_PCT = float(os.getenv("SW_ALERT_MIN_CHANGE_PCT", "0"))  # 0 = every change
