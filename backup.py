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
        # Consistent copy even if writers are active
        raw = db_path.read_bytes()
        zf.writestr("inventory.db", raw)
    return buf.getvalue()


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


def backup_dir_from_env(default: Path) -> Path:
    return Path(os.getenv("BACKUP_DIR", str(default)))


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
    """Decrypt enc_path and replace dest_db. Optionally save prior DB aside."""
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


def maybe_backup_after_event(
    db_path: Path,
    *,
    reason: str = "paid_confirm",
) -> Optional[Path]:
    """
    Best-effort backup when BACKUP_PASSPHRASE is set.
    Returns path or None if skipped/failed.
    """
    passphrase = passphrase_from_env()
    if not passphrase:
        log.debug("Skip backup (%s): BACKUP_PASSPHRASE not set", reason)
        return None
    bdir = backup_dir_from_env(
        Path(os.getenv("DB_PATH", "inventory.db")).resolve().parent / "backups"
    )
    try:
        path = create_encrypted_backup(
            Path(db_path), bdir, passphrase, reason=reason
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
