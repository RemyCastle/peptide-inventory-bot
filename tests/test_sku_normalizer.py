"""Canonical SKUs from supplier spellings on the Sep 2026 order lines."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sku_normalizer import canonical_sku


class SkuNormalizerTests(unittest.TestCase):
    def test_reta_spellings_are_one_sku(self) -> None:
        names = ["Reta 30 MG", "R30", "R 30 MG", "Reta 30", "Retatrutide 30mg", "R30 x10"]
        skus = {canonical_sku(name) for name in names}
        self.assertEqual(skus, {"RETA-30"})

    def test_tirz_spellings_are_one_sku(self) -> None:
        names = ["T30", "T 30 MG", "Tirzepatide 30", "Tirzepitide", "Tirzepatide 30 MG"]
        skus = {canonical_sku(name) for name in names}
        self.assertEqual(skus, {"TIRZ-30"})

    def test_written_strength_wins_over_the_default(self) -> None:
        self.assertEqual(canonical_sku("Tirzepatide 20"), "TIRZ-20")
        self.assertEqual(canonical_sku("Reta 10 MG"), "RETA-10")

    def test_september_order_lines(self) -> None:
        self.assertEqual(canonical_sku("5-Amino-1MQ"), "5-AMINO-1MQ-50")
        self.assertEqual(canonical_sku("5-AMINO-1MQ 50 MG"), "5-AMINO-1MQ-50")
        self.assertEqual(canonical_sku("MOTS-C 40"), "MOTS-C-40")
        self.assertEqual(canonical_sku("GHK-CU 100"), "GHK-CU-100")
        self.assertEqual(canonical_sku("NAD"), "NAD")

    def test_unknown_name(self) -> None:
        self.assertIsNone(canonical_sku("BPC-157 5MG"))
        self.assertIsNone(canonical_sku(""))


if __name__ == "__main__":
    unittest.main()
