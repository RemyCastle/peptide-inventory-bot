"""Predict the next order from the gaps between a customer's order dates.

Needs at least two orders. One order has no interval, so there is no next
date. Callers should pass dates already in Pacific Time; this module does
not convert time zones. When `as_of` is omitted, "today" is the UTC date.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("empty order date")
    return date.fromisoformat(text[:10])


def order_gaps(order_dates: Sequence[date | datetime | str]) -> list[int] | None:
    """Day gaps between consecutive orders. None when fewer than two dates."""
    days = sorted({_as_date(item) for item in order_dates})
    if len(days) < 2:
        return None
    return [(later - earlier).days for earlier, later in pairwise(days)]


def predict_reorder(
    order_dates: Sequence[date | datetime | str],
    *,
    as_of: date | datetime | str | None = None,
    due_within_days: int = 3,
) -> dict:
    """Average gap and next expected date for one customer.

    `due` is true when the next date is within `due_within_days` of `as_of`,
    including dates that are already past.
    """
    days = sorted({_as_date(item) for item in order_dates})
    today = _as_date(as_of) if as_of is not None else datetime.now(timezone.utc).date()
    gaps = order_gaps(days)
    base = {
        "order_count": len(days),
        "gaps_days": gaps,
        "average_gap_days": None,
        "last_order": days[-1] if days else None,
        "next_expected": None,
        "due": False,
        "due_within_days": int(due_within_days),
    }
    if not gaps:
        return base
    average = sum((Decimal(gap) for gap in gaps), Decimal(0)) / Decimal(len(gaps))
    average_days = float(average.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    seconds = int((average * Decimal(86400)).to_integral_value(rounding=ROUND_HALF_UP))
    next_at = datetime.combine(days[-1], datetime.min.time()) + timedelta(seconds=seconds)
    next_day = next_at.date()
    base["average_gap_days"] = average_days
    base["next_expected"] = next_day
    base["due"] = (next_day - today).days <= int(due_within_days)
    return base


def predict_reorders(
    history: Iterable[dict],
    *,
    as_of: date | datetime | str | None = None,
    due_within_days: int = 3,
) -> list[dict]:
    """One row per customer. Each history item needs `customer` and `ordered_on`."""
    grouped: dict[str, list] = {}
    order: list[str] = []
    for row in history:
        name = " ".join(str(row.get("customer") or "").split())
        if not name or row.get("ordered_on") in (None, ""):
            continue
        if name not in grouped:
            order.append(name)
            grouped[name] = []
        grouped[name].append(row["ordered_on"])
    rows = []
    for name in order:
        predicted = predict_reorder(
            grouped[name], as_of=as_of, due_within_days=due_within_days
        )
        rows.append({"customer": name, **predicted})
    return rows
