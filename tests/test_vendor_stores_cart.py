"""Vendor mini-app cart parse hardening + Markdown escape helpers."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import vendor_stores  # noqa: E402


class CoerceCartIntTests(unittest.TestCase):
    def test_int_and_numeric_string(self) -> None:
        self.assertEqual(vendor_stores._coerce_cart_int(3), 3)
        self.assertEqual(vendor_stores._coerce_cart_int("7"), 7)
        self.assertEqual(vendor_stores._coerce_cart_int(0), 0)

    def test_rejects_non_finite_float(self) -> None:
        with self.assertRaises(ValueError):
            vendor_stores._coerce_cart_int(float("nan"))
        with self.assertRaises(ValueError):
            vendor_stores._coerce_cart_int(float("inf"))
        with self.assertRaises(ValueError):
            vendor_stores._coerce_cart_int(float("-inf"))

    def test_rejects_non_finite_string(self) -> None:
        for s in ("nan", "NaN", "inf", "Infinity", "-inf"):
            with self.assertRaises(ValueError):
                vendor_stores._coerce_cart_int(s)

    def test_rejects_bool_and_bad_types(self) -> None:
        with self.assertRaises(TypeError):
            vendor_stores._coerce_cart_int(True)
        with self.assertRaises((TypeError, ValueError)):
            vendor_stores._coerce_cart_int(None)
        with self.assertRaises((TypeError, ValueError)):
            vendor_stores._coerce_cart_int({})
        with self.assertRaises(ValueError):
            vendor_stores._coerce_cart_int("not-a-number")


class MdEscapeTests(unittest.TestCase):
    def test_escapes_telegram_legacy_metachars(self) -> None:
        raw = "BPC_157 *kit* with `code` and [link"
        esc = vendor_stores._md_escape(raw)
        self.assertIn("\\_", esc)
        self.assertIn("\\*", esc)
        self.assertIn("\\`", esc)
        self.assertIn("\\[", esc)
        # Original metachars must not appear unescaped as single chars that break parse
        # (escape inserts backslash before each)
        self.assertEqual(esc.count("_"), esc.count("\\_"))
        self.assertEqual(esc.count("*"), esc.count("\\*"))

    def test_none_and_empty(self) -> None:
        self.assertEqual(vendor_stores._md_escape(""), "")
        self.assertEqual(vendor_stores._md_escape(None), "")  # type: ignore[arg-type]


class MalformedCartNormalizeTests(unittest.TestCase):
    """Mirror the on_web_app_data parse block so hostile carts fail closed."""

    def _normalize(self, raw: str) -> list[dict]:
        import json
        from config import KIT_SIZE

        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("cart root is not an object")
        cart_items = parsed.get("items")
        if not isinstance(cart_items, list):
            raise ValueError("items is not a list")
        items: list[dict] = []
        for it in cart_items:
            if not isinstance(it, dict):
                raise ValueError("cart item is not an object")
            pid = vendor_stores._coerce_cart_int(it.get("id") or 0)
            vials = max(0, vendor_stores._coerce_cart_int(it.get("vials") or 0))
            kits = max(0, vendor_stores._coerce_cart_int(it.get("kits") or 0))
            if pid <= 0:
                continue
            if vials:
                items.append({"product_id": pid, "quantity": vials})
            if kits:
                items.append(
                    {"product_id": pid, "quantity": kits * KIT_SIZE, "is_kit": True}
                )
        return items

    def test_valid_cart(self) -> None:
        items = self._normalize('{"items":[{"id":1,"vials":2,"kits":0}]}')
        self.assertEqual(items, [{"product_id": 1, "quantity": 2}])

    def test_items_not_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._normalize('{"items":{"id":1}}')

    def test_item_not_dict_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._normalize('{"items":[1,2,3]}')

    def test_nan_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._normalize('{"items":[{"id":"NaN","vials":1}]}')

    def test_infinity_qty_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._normalize('{"items":[{"id":1,"vials":"Infinity"}]}')

    def test_string_garbage_qty_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._normalize('{"items":[{"id":1,"vials":"abc"}]}')

    def test_markdown_breaking_product_name_escaped(self) -> None:
        """Names with _ * ` [ must be escaped so Telegram Markdown won't 400."""
        name = "RET_A *pro* `special` [vial]"
        line = f"  • {vendor_stores._md_escape(name)} × 1 — $40.00"
        self.assertNotIn("RET_A", line)  # underscore escaped
        self.assertIn("RET\\_A", line)
        self.assertIn("\\*pro\\*", line)


if __name__ == "__main__":
    unittest.main()
