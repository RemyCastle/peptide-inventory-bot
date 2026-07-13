"""Tests for product COA file (PDF/photo) and legacy URL field."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


class CoaFileDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmpdir.name) / "coa.db")
        db.init_db()
        self.shop = 10
        self.other = 20
        db.ensure_shop(self.shop, title="A")
        db.ensure_shop(self.other, title="B")
        self.pid = db.add_product(self.shop, name="Vial A", price=40.0, stock=5)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_set_coa_file_document(self) -> None:
        ok, ftype = db.set_product_coa_file(
            self.pid, self.shop, "AgADFILE1", "document", "coa.pdf"
        )
        self.assertTrue(ok)
        self.assertEqual(ftype, "document")
        p = db.get_product(self.pid)
        self.assertEqual(p["coa_file_id"], "AgADFILE1")
        self.assertEqual(p["coa_file_type"], "document")
        self.assertEqual(p["coa_filename"], "coa.pdf")
        self.assertTrue(db.product_has_coa_file(p))

    def test_set_coa_file_photo(self) -> None:
        ok, ftype = db.set_product_coa_file(
            self.pid, self.shop, "AgADPHOTO1", "photo"
        )
        self.assertTrue(ok)
        self.assertEqual(ftype, "photo")
        p = db.get_product(self.pid)
        self.assertEqual(p["coa_file_id"], "AgADPHOTO1")
        self.assertTrue(db.product_has_coa_file(p))

    def test_invalid_file_type(self) -> None:
        ok, msg = db.set_product_coa_file(self.pid, self.shop, "x", "video")
        self.assertFalse(ok)
        self.assertIn("document", msg.lower() + "photo")

    def test_cross_shop_rejected(self) -> None:
        ok, _ = db.set_product_coa_file(
            self.pid, self.other, "AgAD", "document", "a.pdf"
        )
        self.assertFalse(ok)
        self.assertFalse(db.product_has_coa_file(db.get_product(self.pid)))

    def test_clear_coa(self) -> None:
        db.set_product_coa_file(self.pid, self.shop, "AgAD", "document", "a.pdf")
        db.set_product_coa_url(self.pid, self.shop, "https://example.com/coa.pdf")
        self.assertTrue(db.clear_product_coa(self.pid, self.shop))
        p = db.get_product(self.pid)
        self.assertIsNone(p.get("coa_file_id"))
        self.assertIsNone(p.get("coa_url"))
        self.assertFalse(db.product_has_coa_file(p))

    def test_url_counts_as_coa(self) -> None:
        ok, url = db.set_product_coa_url(
            self.pid, self.shop, "https://example.com/coa.pdf"
        )
        self.assertTrue(ok)
        self.assertEqual(url, "https://example.com/coa.pdf")
        p = db.get_product(self.pid)
        self.assertFalse(db.product_has_coa_file(p))
        self.assertTrue(db.product_has_coa_url(p))
        self.assertTrue(db.product_has_coa(p))

    def test_file_or_url_or_both(self) -> None:
        p = db.get_product(self.pid)
        self.assertFalse(db.product_has_coa(p))
        db.set_product_coa_url(self.pid, self.shop, "https://example.com/c.pdf")
        self.assertTrue(db.product_has_coa(db.get_product(self.pid)))
        db.set_product_coa_file(self.pid, self.shop, "AgAD", "document", "a.pdf")
        p = db.get_product(self.pid)
        self.assertTrue(db.product_has_coa_file(p))
        self.assertTrue(db.product_has_coa_url(p))
        self.assertTrue(db.product_has_coa(p))


class CoaKeyboardTests(unittest.TestCase):
    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT))
        import bot as botmod

        self.bot = botmod

    def test_detail_keyboard_with_coa_file(self) -> None:
        p = {
            "id": 1,
            "name": "X",
            "coa_file_id": "AgAD123",
            "coa_file_type": "document",
            "stock": 3,
        }
        kb = self.bot.product_detail_keyboard(p, stock=3)
        callbacks = []
        for row in kb.inline_keyboard:
            for btn in row:
                if getattr(btn, "callback_data", None):
                    callbacks.append(btn.callback_data)
                self.assertFalse(getattr(btn, "url", None), "must not use URL buttons")
        self.assertIn("viewcoa:1", callbacks)

    def test_detail_keyboard_without_coa(self) -> None:
        p = {"id": 1, "name": "X", "coa_file_id": None, "coa_url": None, "stock": 3}
        kb = self.bot.product_detail_keyboard(p, stock=3)
        for row in kb.inline_keyboard:
            for btn in row:
                cd = getattr(btn, "callback_data", None) or ""
                self.assertFalse(cd.startswith("viewcoa:"))

    def test_detail_keyboard_with_coa_url_only(self) -> None:
        p = {
            "id": 9,
            "name": "Y",
            "coa_file_id": None,
            "coa_url": "https://example.com/coa.pdf",
            "stock": 2,
        }
        kb = self.bot.product_detail_keyboard(p, stock=2)
        callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if getattr(btn, "callback_data", None)
        ]
        self.assertIn("viewcoa:9", callbacks)

    def test_list_keyboard_coa_callback(self) -> None:
        products = [
            {
                "id": 1,
                "name": "With",
                "price": 10,
                "stock": 1,
                "coa_file_id": "AgAD",
                "coa_file_type": "photo",
            },
            {
                "id": 2,
                "name": "LinkOnly",
                "price": 10,
                "stock": 1,
                "coa_file_id": None,
                "coa_url": "https://example.com/c.pdf",
            },
            {
                "id": 3,
                "name": "Without",
                "price": 10,
                "stock": 1,
                "coa_file_id": None,
                "coa_url": None,
            },
        ]
        kb = self.bot.product_list_keyboard(products)
        first = kb.inline_keyboard[0]
        self.assertEqual(len(first), 2)
        self.assertEqual(first[1].callback_data, "viewcoa:1")
        second = kb.inline_keyboard[1]
        self.assertEqual(len(second), 2)
        self.assertEqual(second[1].callback_data, "viewcoa:2")
        third = kb.inline_keyboard[2]
        self.assertEqual(len(third), 1)


if __name__ == "__main__":
    unittest.main()
