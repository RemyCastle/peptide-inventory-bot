"""Paid SPBC /notify → Unicorn shop order (scratch DB only)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import spbc_notify  # noqa: E402
import spbc_unicorn  # noqa: E402

UNICORN_SHOP = 91001
OTHER_SHOP = 91002


class PaidSpbcUnicornImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._td.name) / "unicorn_import.db")
        db.init_db()
        db.ensure_shop(UNICORN_SHOP, title="Unicorn Magic Factory")
        db.ensure_shop(OTHER_SHOP, title="Other Shop")
        self.reta = db.add_product(UNICORN_SHOP, "RETA 35 MG", 50.0, stock=10)
        db.update_product(self.reta, sku="RETA-35")
        self.sema = db.add_product(UNICORN_SHOP, "SEMA 10MG", 40.0, stock=8)
        # Same name in another shop must not be used
        db.add_product(OTHER_SHOP, "RETA 35 MG", 9.0, stock=99)
        self._shop = mock.patch.object(spbc_unicorn, "UNICORN_SHOP_CHAT_ID", UNICORN_SHOP)
        self._shop.start()
        spbc_notify._sessions.clear()

    def tearDown(self) -> None:
        spbc_notify._sessions.clear()
        self._shop.stop()
        self._td.cleanup()

    def _payload(self, **extra) -> dict:
        body = {
            "order_number": "PEP-20260822-0001",
            "status": "paid",
            "customer_name": "Jane Doe",
            "total_cents": 15000,
            "items": [
                {"name": "RETA 35 MG (Vial)", "qty": 2, "sku": "RETA-35"},
                {"name": "SEMA 10MG", "qty": 1},
            ],
            "shipping": {
                "name": "Jane Doe",
                "line1": "1 Main St",
                "city": "Springfield",
                "state": "MO",
                "postal": "65801",
            },
        }
        body.update(extra)
        return body

    def test_happy_path_creates_paid_order_and_deducts_stock(self) -> None:
        result = spbc_unicorn.import_paid_spbc_order(self._payload())
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["status"], "paid")
        self.assertEqual(result["shop_chat_id"], UNICORN_SHOP)
        self.assertEqual(result.get("payment_requested"), False)
        self.assertEqual(result.get("unmatched"), [])

        order = db.get_order(result["order_id"])
        self.assertEqual(order["status"], "paid")
        self.assertEqual(order["chat_id"], UNICORN_SHOP)
        self.assertEqual(order["external_ref"], "spbc:PEP-20260822-0001")
        self.assertEqual(order["payment_method_name"], spbc_unicorn.PAYMENT_LABEL)
        self.assertIn("Do not invoice", order["ship_notes"])
        self.assertTrue(order.get("paid_at"))

        items = db.get_order_items(order["id"])
        names = {it["product_name"]: it for it in items}
        self.assertIn("RETA 35 MG", names)
        self.assertEqual(int(names["RETA 35 MG"]["quantity"]), 2)
        self.assertEqual(int(names["RETA 35 MG"]["product_id"]), self.reta)

        self.assertEqual(int(db.get_product(self.reta)["stock"]), 8)
        self.assertEqual(int(db.get_product(self.sema)["stock"]), 7)

        with db.get_db() as conn:
            audits = conn.execute(
                "SELECT reason, delta FROM stock_audit WHERE order_id = ?",
                (order["id"],),
            ).fetchall()
        self.assertTrue(audits)
        self.assertTrue(all(a["reason"] == "order_paid_confirm" for a in audits))

    def test_duplicate_order_number_does_not_create_or_deduct_again(self) -> None:
        first = spbc_unicorn.import_paid_spbc_order(self._payload())
        second = spbc_unicorn.import_paid_spbc_order(self._payload())
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["order_id"], first["order_id"])

        with db.get_db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM orders WHERE external_ref = ?",
                ("spbc:PEP-20260822-0001",),
            ).fetchone()
        self.assertEqual(int(n["c"]), 1)
        self.assertEqual(int(db.get_product(self.reta)["stock"]), 8)
        self.assertEqual(int(db.get_product(self.sema)["stock"]), 7)

    def test_unmatched_sku_stays_on_order_no_fake_product(self) -> None:
        payload = self._payload(
            items=[
                {"name": "RETA 35 MG (Vial)", "qty": 1, "sku": "RETA-35"},
                {"name": "MYSTERY 1MG", "qty": 3, "sku": "NO-SUCH-SKU"},
            ]
        )
        before_names = {p["name"] for p in db.list_products(UNICORN_SHOP, active_only=False)}
        result = spbc_unicorn.import_paid_spbc_order(payload)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "paid")
        self.assertTrue(any("MYSTERY 1MG" in u for u in result["unmatched"]))
        self.assertTrue(any("NO-SUCH-SKU" in u for u in result["unmatched"]))

        order = db.get_order(result["order_id"])
        items = db.get_order_items(order["id"])
        unmatched_lines = [it for it in items if it["product_id"] is None]
        self.assertEqual(len(unmatched_lines), 1)
        self.assertIn("MYSTERY 1MG", unmatched_lines[0]["product_name"])
        self.assertIn("unmatched", unmatched_lines[0]["product_name"].lower())
        self.assertIn("Unmatched SPBC items", order["ship_notes"])
        self.assertIn("MYSTERY 1MG", order["ship_notes"])
        self.assertIn("MYSTERY 1MG", order.get("admin_note") or "")

        after = db.list_products(UNICORN_SHOP, active_only=False)
        self.assertEqual({p["name"] for p in after}, before_names)
        self.assertFalse(any("MYSTERY" in (p["name"] or "") for p in after))
        self.assertEqual(int(db.get_product(self.reta)["stock"]), 9)

    def test_sku_match_without_identical_name(self) -> None:
        payload = self._payload(
            items=[{"name": "Website Reta Label", "qty": 1, "sku": "RETA-35"}]
        )
        result = spbc_unicorn.import_paid_spbc_order(payload)
        self.assertTrue(result["ok"], result)
        items = db.get_order_items(result["order_id"])
        self.assertEqual(int(items[0]["product_id"]), self.reta)
        self.assertEqual(int(db.get_product(self.reta)["stock"]), 9)

    def test_handle_notify_paid_creates_unicorn_order(self) -> None:
        sent: list = []

        def fake_send(chat_id, text):
            sent.append((str(chat_id), text))
            return {"message_id": len(sent)}

        payload = self._payload(
            items=[
                {
                    "name": "RETA 35 MG (Vial)",
                    "qty": 1,
                    "sku": "RETA-35",
                    "supplier": "Acme",
                    "telegram_chat_id": "111",
                }
            ]
        )
        with mock.patch.object(spbc_notify, "send_telegram", fake_send), mock.patch.object(
            spbc_notify, "OWNER_TELEGRAM_CHAT_ID", "999"
        ), mock.patch.object(spbc_notify, "SUPPLIER_TELEGRAM_CHAT_ID", ""):
            code, body = spbc_notify.handle_notify(payload)
        self.assertEqual(code, 200)
        self.assertIn("unicorn_order", body)
        self.assertTrue(body["unicorn_order"]["ok"], body["unicorn_order"])
        self.assertEqual(body["unicorn_order"]["status"], "paid")
        order = db.get_order(body["unicorn_order"]["order_id"])
        self.assertEqual(order["status"], "paid")
        self.assertEqual(int(db.get_product(self.reta)["stock"]), 9)

        code2, body2 = None, None
        with mock.patch.object(spbc_notify, "send_telegram", fake_send), mock.patch.object(
            spbc_notify, "OWNER_TELEGRAM_CHAT_ID", "999"
        ), mock.patch.object(spbc_notify, "SUPPLIER_TELEGRAM_CHAT_ID", ""):
            code2, body2 = spbc_notify.handle_notify(payload)
        self.assertEqual(code2, 200)
        self.assertTrue(body2["unicorn_order"]["duplicate"])
        self.assertEqual(
            body2["unicorn_order"]["order_id"], body["unicorn_order"]["order_id"]
        )
        self.assertEqual(int(db.get_product(self.reta)["stock"]), 9)

    def test_skips_when_shop_unconfigured(self) -> None:
        with mock.patch.object(spbc_unicorn, "UNICORN_SHOP_CHAT_ID", 0), mock.patch.object(
            spbc_unicorn, "resolve_unicorn_shop_chat_id", return_value=0
        ):
            result = spbc_unicorn.import_paid_spbc_order(self._payload())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unicorn_shop_not_configured")
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()
        self.assertEqual(int(n["c"]), 0)


if __name__ == "__main__":
    unittest.main()
