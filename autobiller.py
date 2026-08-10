"""Automatic weekly vendor service-fee billing (background daemon).

Once a week (previous complete UTC Mon–Mon only), generates invoices, DMs each
vendor shop's admins exactly once, and DMs the owner a summary.

Safe isolation:
- Runs on a daemon thread; every tick is try/except so errors never affect
  order handling or crash the process.
- Only reads orders / writes service_fee_invoices rows (vendor_notified_at).
- Does not touch storefront keys, order-receiver logic, or payment seeding.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("autobiller")

# ~hourly wake; week boundary is enforced by bill_previous_complete_week.
DEFAULT_TICK_SECONDS = 3600
# Short delay so spbc_notify.set_bot_token can land after bot.main builds the app.
DEFAULT_BOOT_DELAY_SECONDS = 45

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _tick_seconds() -> float:
    raw = (os.getenv("AUTOBILL_TICK_SECONDS") or "").strip()
    if raw:
        try:
            return max(60.0, float(raw))
        except ValueError:
            pass
    return float(DEFAULT_TICK_SECONDS)


def _boot_delay_seconds() -> float:
    raw = (os.getenv("AUTOBILL_BOOT_DELAY_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return float(DEFAULT_BOOT_DELAY_SECONDS)


def bot_token_ready() -> bool:
    """True when spbc_notify has an active main-bot token."""
    try:
        import spbc_notify

        with spbc_notify._state_lock:
            return bool(spbc_notify._bot_token)
    except Exception:
        return False


def _send_dm(chat_id: int | str, text: str) -> bool:
    """Send via main bot. Returns True on success; does not raise."""
    try:
        import spbc_notify

        spbc_notify.send_telegram(chat_id, text)
        return True
    except Exception as exc:
        log.warning("autobiller DM failed chat_id=%s: %s", chat_id, exc)
        return False


def run_billing_tick(
    *,
    ref: datetime | None = None,
    send_fn: Callable[[int | str, str], bool] | None = None,
    require_token: bool = True,
) -> dict[str, Any]:
    """One billing cycle. Safe to call from tests (inject send_fn).

    Returns a result dict for tests/logging; never raises.
    """
    result: dict[str, Any] = {
        "ok": True,
        "skipped": None,
        "invoices": [],
        "notified": [],
        "failed_notify": [],
        "total_billed": 0.0,
        "owner_summary": None,
        "error": None,
    }
    send = send_fn or _send_dm
    try:
        if require_token and send_fn is None and not bot_token_ready():
            result["skipped"] = "bot_token_not_ready"
            log.info("autobiller tick skipped: bot token not ready yet")
            return result

        import franchise
        from config import MASTER_VENMO, OWNER_TELEGRAM_CHAT_ID

        franchise.ensure_franchise_tables()
        now = ref or datetime.now(timezone.utc)

        ok, msg, invs = franchise.bill_previous_complete_week(ref=now)
        if not ok:
            result["ok"] = False
            result["error"] = msg
            log.warning("autobiller generate failed: %s", msg)
            return result

        result["invoices"] = list(invs)
        total_billed = sum(float(i.get("total_fees") or 0) for i in invs)
        result["total_billed"] = total_billed
        log.info(
            "autobiller previous-week invoices: n=%s total=%.2f (%s)",
            len(invs),
            total_billed,
            msg,
        )

        # Catch-up: any open unnotified invoice (not only this tick's week)
        pending = franchise.list_unnotified_open_invoices(limit=200)
        notified: list[dict] = []
        failed: list[dict] = []

        import db

        for inv in pending:
            inv_id = int(inv["id"])
            chat_id = int(inv["chat_id"])
            total = float(inv.get("total_fees") or 0)
            if total <= 0:
                continue
            text = franchise.format_vendor_invoice_dm(
                inv, master_venmo=MASTER_VENMO
            )
            admins = db.list_admins(chat_id)
            if not admins:
                log.warning(
                    "autobiller invoice #%s shop %s has no admins; will retry",
                    inv_id,
                    chat_id,
                )
                failed.append({**inv, "reason": "no_admins"})
                continue

            any_ok = False
            for a in admins:
                uid = a.get("user_id")
                if uid is None:
                    continue
                if send(int(uid), text):
                    any_ok = True
                # keep trying other admins even if one fails

            if any_ok:
                stamped = franchise.mark_vendor_notified(inv_id)
                if stamped:
                    notified.append(inv)
                else:
                    # Already stamped by a concurrent tick — treat as done
                    notified.append(inv)
            else:
                log.warning(
                    "autobiller invoice #%s: all admin DMs failed; not stamping",
                    inv_id,
                )
                failed.append({**inv, "reason": "send_failed"})

        result["notified"] = notified
        result["failed_notify"] = failed

        # Owner summary once per successful vendor notify wave (not every hourly tick)
        if notified:
            # Prefer the invoices we just DM'd for per-shop lines; fall back to gen list
            summary_invs = notified if notified else invs
            billed = sum(float(i.get("total_fees") or 0) for i in summary_invs)
            summary = franchise.format_owner_autobill_summary(
                invoices=summary_invs,
                notified=notified,
                total_billed=billed if billed else total_billed,
            )
            result["owner_summary"] = summary
            owner = (OWNER_TELEGRAM_CHAT_ID or "").strip()
            if owner:
                if not send(owner, summary):
                    log.warning("autobiller owner summary DM failed")
            else:
                log.info("autobiller: OWNER_TELEGRAM_CHAT_ID unset; summary not sent")

        return result
    except Exception as exc:
        log.exception("autobiller tick error (swallowed): %s", exc)
        result["ok"] = False
        result["error"] = str(exc)
        return result


def _loop() -> None:
    delay = _boot_delay_seconds()
    if delay > 0 and not _stop.wait(delay):
        pass
    tick = _tick_seconds()
    log.info(
        "autobiller daemon started (boot_delay=%.0fs tick=%.0fs)",
        delay,
        tick,
    )
    while not _stop.is_set():
        try:
            run_billing_tick()
        except Exception:
            # Belt-and-suspenders: run_billing_tick already swallows
            log.exception("autobiller unexpected loop error")
        if _stop.wait(tick):
            break
    log.info("autobiller daemon stopped")


def start_autobiller(*, daemon: bool = True) -> Optional[threading.Thread]:
    """Start the background thread once. Failure must never block boot."""
    global _thread
    try:
        if _thread is not None and _thread.is_alive():
            log.info("autobiller already running")
            return _thread
        _stop.clear()
        t = threading.Thread(target=_loop, name="autobiller", daemon=daemon)
        t.start()
        _thread = t
        log.info("autobiller thread started")
        return t
    except Exception:
        log.exception("autobiller failed to start (continuing without it)")
        return None


def stop_autobiller() -> None:
    """Test helper: signal the loop to exit."""
    _stop.set()
