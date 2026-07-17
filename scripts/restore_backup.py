#!/usr/bin/env python3
"""Restore an encrypted inventory backup into the live DB path.

Usage (from project root, venv active):

  set BACKUP_PASSPHRASE=your-secret
  python scripts/restore_backup.py backups/latest.enc

  python scripts/restore_backup.py backups/latest.enc --db path/to/inventory.db
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import backup as backup_mod  # noqa: E402
from config import BACKUP_PASSPHRASE, DB_PATH  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Restore encrypted inventory backup")
    p.add_argument("enc_file", type=Path, help="Path to .enc snapshot")
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help=f"Destination DB (default: {DB_PATH})",
    )
    p.add_argument(
        "--passphrase",
        default=None,
        help="Override BACKUP_PASSPHRASE env",
    )
    p.add_argument(
        "--no-aside",
        action="store_true",
        help="Do not keep a copy of the previous DB",
    )
    args = p.parse_args()
    passphrase = (args.passphrase or BACKUP_PASSPHRASE or os.getenv("BACKUP_PASSPHRASE", "")).strip()
    if not passphrase:
        print("ERROR: set BACKUP_PASSPHRASE or pass --passphrase", file=sys.stderr)
        return 1
    enc = args.enc_file
    if not enc.is_file():
        print(f"ERROR: not found: {enc}", file=sys.stderr)
        return 1
    dest = args.db or DB_PATH
    meta = backup_mod.restore_encrypted_backup(
        enc,
        dest,
        passphrase,
        backup_existing=not args.no_aside,
    )
    print(f"Restored -> {dest}")
    print(f"Meta: {meta}")
    print("Next: set TELEGRAM_BOT_TOKEN or BOT_TOKENS, then start the bot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
