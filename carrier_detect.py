"""Classify a tracking number by its format.

The number wins over a supplier email that names a different carrier.
SMS-605 is the case this is built for: the email said USPS, and the number
1Z… is UPS.
"""

from __future__ import annotations

import re

_USPS_PREFIXES = {"92", "93", "94"}


def _clean(tracking: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", tracking or "").upper()


def carrier_from_format(tracking: str) -> str | None:
    """Return UPS, USPS, or FedEx when the number's shape is known."""
    raw = _clean(tracking)
    if not raw:
        return None
    if raw.startswith("1Z") and len(raw) >= 4:
        return "UPS"
    if raw.startswith("TBA") and len(raw) >= 6:
        return "Amazon"
    if not raw.isdigit():
        return None
    size = len(raw)
    if 20 <= size <= 22 and raw[:2] in _USPS_PREFIXES:
        return "USPS"
    # ZIP+IMpb: 420 + 5-digit ZIP + a 92/93/94 tracking number.
    if size >= 12 and raw.startswith("420") and raw[8:10] in _USPS_PREFIXES:
        return "USPS"
    if size == 22 and raw.startswith("96"):
        return "FedEx"
    if size in {12, 15}:
        return "FedEx"
    return None


def detect_carrier(tracking: str, labeled: str | None = None) -> str:
    """Format first. Otherwise the label, or `unknown` when both are empty."""
    found = carrier_from_format(tracking)
    if found:
        return found
    label = " ".join(str(labeled or "").split())
    return label or "unknown"
