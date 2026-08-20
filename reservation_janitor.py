"""Background expiry for checkout stock reservations.

Soft holds only: expired rows are released; products.stock is never changed.
Isolated daemon — failures must not affect order handling.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

log = logging.getLogger("reservation_janitor")

DEFAULT_TICK_SECONDS = 60
DEFAULT_BOOT_DELAY_SECONDS = 15

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _tick_seconds() -> float:
    raw = (os.getenv("RESERVATION_JANITOR_TICK_SECONDS") or "").strip()
    if raw:
        try:
            return max(15.0, float(raw))
        except ValueError:
            pass
    return float(DEFAULT_TICK_SECONDS)


def _boot_delay_seconds() -> float:
    raw = (os.getenv("RESERVATION_JANITOR_BOOT_DELAY_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return float(DEFAULT_BOOT_DELAY_SECONDS)


def run_expire_tick() -> int:
    import db

    try:
        n = db.expire_stale_reservations()
        if n:
            log.info("released %s expired stock reservation(s)", n)
        return n
    except Exception:
        log.exception("reservation expire tick failed (swallowed)")
        return 0


def _loop() -> None:
    delay = _boot_delay_seconds()
    if delay > 0:
        _stop.wait(delay)
    tick = _tick_seconds()
    log.info(
        "reservation janitor started (boot_delay=%.0fs tick=%.0fs)",
        delay,
        tick,
    )
    while not _stop.is_set():
        run_expire_tick()
        if _stop.wait(tick):
            break
    log.info("reservation janitor stopped")


def start_reservation_janitor(*, daemon: bool = True) -> Optional[threading.Thread]:
    global _thread
    try:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        t = threading.Thread(target=_loop, name="reservation-janitor", daemon=daemon)
        t.start()
        _thread = t
        log.info("reservation janitor thread started")
        return t
    except Exception:
        log.exception("reservation janitor failed to start")
        return None


def stop_reservation_janitor() -> None:
    _stop.set()
