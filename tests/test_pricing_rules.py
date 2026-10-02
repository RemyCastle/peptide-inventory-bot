"""Public price formulas. Patriotic and franchisee stay on stored prices."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db
import franchise
import pricing_rules


class PublicPriceTests(unittest.TestCase):
    def test_spbc_vial_and_kit(self) -> None:
        self.assertEqual(pricing_rules.public_price("spbc", 17.50), 52.50)
        self.assertEqual(
            pricing_rules.public_price("SPBC", 80, kind="kit"), 330.00
        )
        self.assertEqual(
            pricing_rules.public_price("springfield", 10, kind="single"), 45.00
        )

    def test_jekyll_doubles_single_and_kit(self) -> None:
        self.assertEqual(
            pricing_rules.public_price("Jekyll A. Hyde", 17.5), 35.00
        )
        self.assertEqual(
            pricing_rules.public_price("jekyll", 80, kind="kit"), 160.00
        )

    def test_patriotic_and_franchisee_keep_listed_price(self) -> None:
        self.assertEqual(
            pricing_rules.public_price(
                "Patriotic Peptides", 20, listed_price=44
            ),
            44.00,
        )
        self.assertEqual(
            pricing_rules.public_price("franchisee", 20, kind="kit", listed_price=90),
            90.00,
        )
        # No SMS addon and no doubling when the catalog price is the cost itself.
        self.assertEqual(pricing_rules.public_price("patriotic", 20), 20.00)
        self.assertEqual(pricing_rules.public_price("franchise", 15, kind="kit"), 15.00)

    def test_unknown_channel_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pricing_rules.public_price("unicorn", 10)

    def test_franchisee_shipping_matches_current_code(self) -> None:
        shop = {
            "shipping_enabled": 1,
            "shipping_fee": 8,
            "free_shipping_above": 9999,
            "hidden_service_fee": 2.5,
        }
        base = db.calc_shipping(shop, 40)
        charged, hidden = franchise.customer_shipping_total(shop, 40)
        self.assertEqual(hidden, 2.5)
        self.assertEqual(
            pricing_rules.franchisee_customer_shipping(base, hidden), charged
        )
        self.assertEqual(pricing_rules.franchisee_customer_shipping(0, 2.5), 2.5)


if __name__ == "__main__":
    unittest.main()
