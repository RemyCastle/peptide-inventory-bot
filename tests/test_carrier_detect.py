"""Tracking numbers are classified by format, not by the email label."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from carrier_detect import carrier_from_format, detect_carrier


class CarrierDetectTests(unittest.TestCase):
    def test_sms_605_labeled_usps_is_ups(self) -> None:
        number = "1Z1J329C0217555166"
        self.assertEqual(carrier_from_format(number), "UPS")
        self.assertEqual(detect_carrier(number, labeled="USPS"), "UPS")
        self.assertEqual(detect_carrier("1Z 1J32 9C02 1755 5166", labeled="USPS"), "UPS")

    def test_usps_22_digit_prefixes(self) -> None:
        samples = {
            "92": "9205511043900003415317",
            "93": "9305511043900003415317",
            "94": "9405511043900003415317",
        }
        for prefix, number in samples.items():
            self.assertEqual(len(number), 22, prefix)
            self.assertEqual(detect_carrier(number, labeled="UPS"), "USPS")

    def test_unknown_keeps_label(self) -> None:
        self.assertEqual(detect_carrier("ABC123", labeled="Local courier"), "Local courier")
        self.assertEqual(detect_carrier("", labeled=""), "unknown")

    def test_fedex_lengths(self) -> None:
        self.assertEqual(carrier_from_format("123456789012"), "FedEx")
        self.assertEqual(carrier_from_format("9612345678901234567890"), "FedEx")


if __name__ == "__main__":
    unittest.main()
