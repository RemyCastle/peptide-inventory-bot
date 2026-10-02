"""Nate's reorder gaps: 8, 5, 9, 6, 8 days, last order 2026-09-25."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reorder_predictor import predict_reorder, predict_reorders


def _dates_ending(last: date, gaps: list[int]) -> list[date]:
    cursor = last
    found = [last]
    for gap in reversed(gaps):
        cursor = cursor - timedelta(days=gap)
        found.append(cursor)
    found.reverse()
    return found


class ReorderPredictorTests(unittest.TestCase):
    def test_nate_average_and_next_date(self) -> None:
        gaps = [8, 5, 9, 6, 8]
        last = date(2026, 9, 25)
        history = _dates_ending(last, gaps)
        result = predict_reorder(history, as_of=date(2026, 10, 2))
        self.assertEqual(result["gaps_days"], gaps)
        self.assertEqual(result["order_count"], 6)
        self.assertEqual(f"{result['average_gap_days']:.1f}", "7.2")
        self.assertEqual(result["last_order"], last)
        self.assertEqual(result["next_expected"], date(2026, 10, 2))
        self.assertTrue(result["due"])

    def test_not_due_a_week_early(self) -> None:
        history = _dates_ending(date(2026, 9, 25), [8, 5, 9, 6, 8])
        result = predict_reorder(history, as_of=date(2026, 9, 25))
        self.assertEqual(result["next_expected"], date(2026, 10, 2))
        self.assertFalse(result["due"])

    def test_single_order_has_no_interval(self) -> None:
        result = predict_reorder([date(2026, 9, 9)], as_of=date(2026, 10, 2))
        self.assertEqual(result["order_count"], 1)
        self.assertIsNone(result["average_gap_days"])
        self.assertIsNone(result["next_expected"])
        self.assertFalse(result["due"])

    def test_groups_customers(self) -> None:
        rows = predict_reorders(
            [
                {"customer": "Nate Milkowski", "ordered_on": "2026-09-17"},
                {"customer": "Janel T", "ordered_on": "2026-09-28"},
                {"customer": "Nate Milkowski", "ordered_on": "2026-09-25"},
            ],
            as_of=date(2026, 10, 2),
        )
        by_name = {row["customer"]: row for row in rows}
        self.assertEqual(by_name["Nate Milkowski"]["average_gap_days"], 8.0)
        self.assertEqual(by_name["Nate Milkowski"]["next_expected"], date(2026, 10, 3))
        self.assertIsNone(by_name["Janel T"]["next_expected"])


if __name__ == "__main__":
    unittest.main()
