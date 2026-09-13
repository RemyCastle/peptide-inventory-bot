"""Encrypted live-DB snapshots for ban recovery (laptop vault + host path)."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import shutil
import struct
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("inventory_bot.backup")

MAGIC = b"UFIB1\0"  # UnicornFartzz Inventory Backup v1
# file = MAGIC | salt(16) | nonce(12) | ciphertext
# ciphertext = AESGCM(zip of inventory.db + meta.json)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def derive_key(passphrase: str, salt: bytes, iterations: int = 200_000) -> bytes:
    if not passphrase:
        raise ValueError("Backup passphrase is empty")
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        iterations,
        dklen=32,
    )


def _aes_gcm_encrypt(key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise RuntimeError(
            "cryptography package required for encrypted backups. "
            "pip install cryptography"
        ) from exc
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, MAGIC)
    return nonce, ct


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise RuntimeError(
            "cryptography package required for encrypted backups. "
            "pip install cryptography"
        ) from exc
    return AESGCM(key).decrypt(nonce, ciphertext, MAGIC)


def build_zip_bytes(db_path: Path, extra_meta: Optional[dict] = None) -> bytes:
    import json
    import io

    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"DB not found: {db_path}")

    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "db_name": db_path.name,
        "db_size": db_path.stat().st_size,
        "schema": "peptide_inventory_bot",
    }
    if extra_meta:
        meta.update(extra_meta)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", json.dumps(meta, indent=2))
        # Consistent copy even if writers are active (includes WAL).
        raw = _consistent_db_bytes(db_path)
        zf.writestr("inventory.db", raw)
    return buf.getvalue()


def _consistent_db_bytes(db_path: Path) -> bytes:
    """Snapshot sqlite including WAL via the backup API. Never touches dest live path.

    Do not use `file:C:/...` URIs — on Windows sqlite treats `C` as the host and
    can open a different database. Path.as_uri() is `file:///C:/...` and is safe.
    """
    import sqlite3
    import tempfile

    db_path = Path(db_path).resolve()
    tmp: Optional[Path] = None
    src: Optional[sqlite3.Connection] = None
    dest: Optional[sqlite3.Connection] = None
    try:
        src = sqlite3.connect(str(db_path))
        fd, name = tempfile.mkstemp(suffix=".snap.db")
        os.close(fd)
        tmp = Path(name)
        dest = sqlite3.connect(str(tmp))
        src.backup(dest)
        dest.close()
        dest = None
        return tmp.read_bytes()
    except Exception:
        return db_path.read_bytes()
    finally:
        if dest is not None:
            try:
                dest.close()
            except Exception:
                pass
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def encrypt_blob(plaintext: bytes, passphrase: str) -> bytes:
    salt = secrets.token_bytes(16)
    key = derive_key(passphrase, salt)
    nonce, ct = _aes_gcm_encrypt(key, plaintext)
    return MAGIC + salt + nonce + ct


def decrypt_blob(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("Not a valid encrypted inventory backup (bad magic)")
    salt = blob[len(MAGIC) : len(MAGIC) + 16]
    nonce = blob[len(MAGIC) + 16 : len(MAGIC) + 16 + 12]
    ct = blob[len(MAGIC) + 16 + 12 :]
    key = derive_key(passphrase, salt)
    return _aes_gcm_decrypt(key, nonce, ct)


def extract_db_from_zip(zip_bytes: bytes, dest_db: Path) -> dict:
    import json
    import io

    dest_db = Path(dest_db)
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    meta: dict = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        if "meta.json" in zf.namelist():
            meta = json.loads(zf.read("meta.json").decode("utf-8"))
        if "inventory.db" not in zf.namelist():
            raise ValueError("Backup zip missing inventory.db")
        data = zf.read("inventory.db")
    tmp = dest_db.with_suffix(dest_db.suffix + ".restore_tmp")
    tmp.write_bytes(data)
    tmp.replace(dest_db)
    return meta


def default_backup_dir(db_path: Optional[Path] = None) -> Path:
    """Vault next to the live DB: /data/inventory.db → /data/backups."""
    if db_path is not None:
        return Path(db_path).resolve().parent / "backups"
    return Path(os.getenv("DB_PATH", "inventory.db")).resolve().parent / "backups"


def backup_dir_from_env(default: Optional[Path] = None) -> Path:
    return Path(os.getenv("BACKUP_DIR", str(default or default_backup_dir())))


def passphrase_from_env() -> str:
    return os.getenv("BACKUP_PASSPHRASE", "").strip()


def retention_days() -> int:
    try:
        return max(1, int(os.getenv("BACKUP_RETENTION_DAYS", "30")))
    except ValueError:
        return 30


def create_encrypted_backup(
    db_path: Path,
    backup_dir: Path,
    passphrase: str,
    *,
    reason: str = "manual",
    keep_daily: bool = True,
) -> Path:
    """
    Write latest.enc always; also a dated file for history.
    Snapshots sqlite via backup API into a temp file — never unlinks or
    truncates the live inventory.db.
    Returns path to the dated (or latest) file written.
    """
    if not passphrase:
        raise ValueError(
            "BACKUP_PASSPHRASE is not set — refuse to write unencrypted vault"
        )
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    zip_bytes = build_zip_bytes(db_path, extra_meta={"reason": reason})
    blob = encrypt_blob(zip_bytes, passphrase)

    latest = backup_dir / "latest.enc"
    _atomic_write(latest, blob)

    stamp = _utc_stamp()
    dated = backup_dir / f"inventory-{stamp}-{reason}.enc"
    _atomic_write(dated, blob)

    if keep_daily:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        daily = backup_dir / f"daily-{day}.enc"
        if not daily.exists():
            _atomic_write(daily, blob)

    log.info(
        "Encrypted backup written reason=%s latest=%s dated=%s size=%s",
        reason,
        latest,
        dated.name,
        len(blob),
    )
    return dated


def _atomic_write(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def restore_encrypted_backup(
    enc_path: Path,
    dest_db: Path,
    passphrase: str,
    *,
    backup_existing: bool = True,
) -> dict:
    """Decrypt enc_path and replace dest_db. Optionally save prior DB aside.

    Never deletes dest_db without writing a restored copy. When
    backup_existing is True (default), the previous file is copied aside
    first; replace is atomic via a sibling .restore_tmp.
    """
    enc_path = Path(enc_path)
    dest_db = Path(dest_db)
    blob = enc_path.read_bytes()
    zip_bytes = decrypt_blob(blob, passphrase)

    if backup_existing and dest_db.is_file():
        aside = dest_db.with_name(
            dest_db.stem + f".pre_restore_{_utc_stamp()}" + dest_db.suffix
        )
        shutil.copy2(dest_db, aside)
        log.info("Previous DB copied to %s", aside)

    meta = extract_db_from_zip(zip_bytes, dest_db)
    log.info("Restored DB to %s from %s meta=%s", dest_db, enc_path, meta)
    return meta


def prune_old_backups(backup_dir: Path, retention_days: int = 30) -> int:
    """
    Delete dated *.enc older than retention_days.
    Always keep latest.enc and daily-* within the window.
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return 0
    cutoff = time.time() - (retention_days * 86400)
    removed = 0
    for p in backup_dir.glob("*.enc"):
        if p.name == "latest.enc":
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                p.unlink()
                removed += 1
                log.info("Pruned old backup %s", p.name)
            except OSError as exc:
                log.warning("Could not prune %s: %s", p, exc)
    return removed


def _pytest_blocks_host_vault() -> bool:
    """Refuse host-vault writes during pytest unless tests opt in.

    load_dotenv() can inject BACKUP_PASSPHRASE / BACKUP_DIR from the laptop
    .env; a paid-confirm test must not overwrite the real latest.enc.
    """
    if not os.getenv("PYTEST_CURRENT_TEST"):
        return False
    flag = os.getenv("BACKUP_ALLOW_IN_PYTEST", "").strip().lower()
    return flag not in ("1", "true", "yes", "on")


def maybe_backup_after_event(
    db_path: Path,
    *,
    reason: str = "paid_confirm",
) -> Optional[Path]:
    """
    Best-effort backup when BACKUP_PASSPHRASE is set.
    Snapshots via sqlite backup API — never unlinks or truncates the live DB.
    Returns path or None if skipped/failed.
    """
    if _pytest_blocks_host_vault():
        log.debug("Skip backup (%s): pytest host-vault guard", reason)
        return None
    passphrase = passphrase_from_env()
    if not passphrase:
        log.debug("Skip backup (%s): BACKUP_PASSPHRASE not set", reason)
        return None
    db_path = Path(db_path)
    bdir = backup_dir_from_env(default_backup_dir(db_path))
    try:
        path = create_encrypted_backup(
            db_path, bdir, passphrase, reason=reason
        )
        prune_old_backups(bdir, retention_days())
        return path
    except Exception as exc:
        log.exception("Backup failed (%s): %s", reason, exc)
        return None


def list_backups(backup_dir: Path) -> list[Path]:
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []
    files = sorted(backup_dir.glob("*.enc"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def read_products_from_encrypted_backup(
    enc_path: Path,
    passphrase: str,
) -> list[dict]:
    """Decrypt a vault snapshot and return product rows. Does not touch dest DB.

    Used to recall stock onto the live catalog without replacing inventory.db.
    """
    import sqlite3
    import tempfile

    enc_path = Path(enc_path)
    blob = enc_path.read_bytes()
    zip_bytes = decrypt_blob(blob, passphrase)

    tmp_path: Optional[Path] = None
    conn: Optional[sqlite3.Connection] = None
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes), "r") as zf:
            if "inventory.db" not in zf.namelist():
                raise ValueError("Backup zip missing inventory.db")
            data = zf.read("inventory.db")
        fd, name = tempfile.mkstemp(suffix=".recall.db")
        os.close(fd)
        tmp_path = Path(name)
        tmp_path.write_bytes(data)
        conn = sqlite3.connect(str(tmp_path))
        conn.row_factory = sqlite3.Row
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(products)").fetchall()
        }
        select_cols = ["id", "chat_id", "name", "stock", "active"]
        if "sku" in cols:
            select_cols.append("sku")
        if "unit" in cols:
            select_cols.append("unit")
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM products"
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            item = dict(r)
            if "sku" not in item:
                item["sku"] = ""
            out.append(item)
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def pick_vault_backup(backup_dir: Path) -> Optional[Path]:
    """Prefer latest.enc, else newest dated *.enc. None if the vault is empty."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return None
    latest = backup_dir / "latest.enc"
    if latest.is_file():
        return latest
    dated = [
        p
        for p in backup_dir.glob("*.enc")
        if p.is_file() and p.name != "latest.enc"
    ]
    if not dated:
        return None
    dated.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return dated[0]
