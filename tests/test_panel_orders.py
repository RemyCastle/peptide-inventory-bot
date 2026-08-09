"""Panel Orders section: confirm payment, tracking, history export, customer DM."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

SHOP = 9100
OTHER_SHOP = 9200
USER = 55
CUSTOMER = 777001
VENDOR_TOKEN = "999000111:AAVendorStorefrontBotTokenTest"


class PanelOrdersBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "panel_orders.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Panel Orders Shop")
        db.ensure_shop(OTHER_SHOP, title="Other Shop")
        webpanel.ensure_webpanel_tables()
        self.tok = {"chat_id": SHOP, "user_id": USER}
        self.pid = db.add_product(SHOP, "BPC-157 10MG", 41.0, stock=10)
        self.other_pid = db.add_product(OTHER_SHOP, "Foreign", 9.0, stock=5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _order(
        self,
        shop: int = SHOP,
        pid: int | None = None,
        qty: int = 1,
        user_id: int = CUSTOMER,
        username: str = "buyer1",
        full_name: str = "Buyer One",
    ) -> dict:
        o = db.create_order(
            shop,
            user_id,
            username,
            full_name,
            [
                {
                    "product_id": pid if pid is not None else (
                        self.pid if shop == SHOP else self.other_pid
                    ),
                    "product_name": "x",
                    "unit_price": 1.0,
                    "quantity": qty,
                }
            ],
            {"id": None, "name": "Cash App"},
            full_name,
            "1 Test St",
            "",
        )
        self.assertIsNotNone(o)
        return o


class ConfirmPaymentTests(PanelOrdersBase):
    def test_confirm_payment_changes_status(self):
        o = self._order()
        db.mark_order_awaiting_confirmation(int(o["id"]))
        with mock.patch.object(webpanel, "notify_order_customer", return_value=True) as dm:
            code, data = webpanel.api_confirm_payment(
                self.tok, {"order_id": o["id"]}
            )
        self.assertEqual(code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "paid")
        self.assertTrue(data["customer_notified"])
        got = db.get_order(int(o["id"]))
        self.assertEqual(got["status"], "paid")
        self.assertEqual(db.get_product(self.pid)["stock"], 9)
        dm.assert_called_once()
        args = dm.call_args[0]
        self.assertEqual(args[0], SHOP)
        self.assertEqual(args[1], CUSTOMER)
        self.assertIn(o["payment_code"], args[2])
        self.assertIn("Payment received", args[2])

    def test_confirm_rejects_cross_shop_order(self):
        foreign = self._order(shop=OTHER_SHOP, pid=self.other_pid)
        code, data = webpanel.api_confirm_payment(
            self.tok, {"order_id": foreign["id"]}
        )
        self.assertEqual(code, 404, data)
        self.assertFalse(data["ok"])
        self.assertEqual(db.get_order(int(foreign["id"]))["status"], "pending_payment")
        self.assertEqual(db.get_product(self.other_pid)["stock"], 5)

    def test_confirm_surfaces_insufficient_stock(self):
        o = self._order(qty=1)
        # wipe stock after order created
        db.update_product(self.pid, stock=0)
        code, data = webpanel.api_confirm_payment(self.tok, {"order_id": o["id"]})
        self.assertEqual(code, 400, data)
        self.assertIn("stock", (data.get("error") or "").lower())


class SetTrackingTests(PanelOrdersBase):
    def test_set_tracking_stores_and_marks_shipped(self):
        o = self._order()
        ok, msg, _ = db.confirm_order_payment(int(o["id"]), USER)
        self.assertTrue(ok, msg)
        with mock.patch.object(webpanel, "notify_order_customer", return_value=True) as dm:
            code, data = webpanel.api_set_tracking(
                self.tok,
                {
                    "order_id": o["id"],
                    "carrier": "USPS",
                    "tracking_number": "9400111899223344556677",
                },
            )
        self.assertEqual(code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "shipped")
        self.assertTrue(data["customer_notified"])
        self.assertIn("usps.com", (data.get("tracking_url") or "").lower())
        got = db.get_order(int(o["id"]))
        self.assertEqual(got["status"], "shipped")
        self.assertEqual(got["tracking_number"], "9400111899223344556677")
        self.assertEqual(got["tracking_carrier"], "USPS")
        body = dm.call_args[0][2]
        self.assertIn("has shipped", body)
        self.assertIn("9400111899223344556677", body)

    def test_set_tracking_rejects_cross_shop(self):
        foreign = self._order(shop=OTHER_SHOP, pid=self.other_pid)
        db.confirm_order_payment(int(foreign["id"]), USER)
        code, data = webpanel.api_set_tracking(
            self.tok,
            {
                "order_id": foreign["id"],
                "carrier": "UPS",
                "tracking_number": "1Z999",
            },
        )
        self.assertEqual(code, 404, data)

    def test_vendor_token_resolved_for_shop(self):
        configs = [
            {
                "name": "Unicorn",
                "token": VENDOR_TOKEN,
                "shop_chat_id": SHOP,
            },
            {
                "name": "Other",
                "token": "other-bot-token",
                "shop_chat_id": OTHER_SHOP,
            },
        ]
        with mock.patch.object(vendor_stores, "load_vendor_configs", return_value=configs):
            tok = vendor_stores.get_bot_token_for_shop(SHOP)
            self.assertEqual(tok, VENDOR_TOKEN)
            self.assertEqual(
                vendor_stores.get_bot_token_for_shop(OTHER_SHOP), "other-bot-token"
            )
            self.assertIsNone(vendor_stores.get_bot_token_for_shop(99999))


class CustomerDmTests(PanelOrdersBase):
    def test_notify_uses_vendor_token_and_customer_id(self):
        sent: list[tuple] = []

        def fake_send(bot_token, chat_id, text, **kwargs):
            sent.append((bot_token, chat_id, text, kwargs))
            return True

        with mock.patch.object(
            vendor_stores, "get_bot_token_for_shop", return_value=VENDOR_TOKEN
        ), mock.patch.object(
            webpanel, "telegram_send_with_token", side_effect=fake_send
        ):
            ok = webpanel.notify_order_customer(
                SHOP, CUSTOMER, "✅ Payment received for order <code>UF1</code>!"
            )
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], VENDOR_TOKEN)
        self.assertEqual(sent[0][1], CUSTOMER)
        self.assertIn("Payment received", sent[0][2])

    def test_confirm_returns_customer_notified_false_when_dm_fails(self):
        o = self._order()
        with mock.patch.object(webpanel, "notify_order_customer", return_value=False):
            code, data = webpanel.api_confirm_payment(
                self.tok, {"order_id": o["id"]}
            )
        self.assertEqual(code, 200, data)
        self.assertTrue(data["ok"])
        self.assertFalse(data["customer_notified"])
        self.assertEqual(db.get_order(int(o["id"]))["status"], "paid")


class HistoryTxtTests(PanelOrdersBase):
    def test_history_filters_by_date_and_lists_items(self):
        o1 = self._order(username="alice", full_name="Alice A")
        o2 = self._order(username="bob", full_name="Bob B")
        # Force created_at days
        today = datetime.now(timezone.utc).date()
        old = (today - timedelta(days=10)).isoformat()
        older = (today - timedelta(days=40)).isoformat()
        with db.get_db() as conn:
            conn.execute(
                "UPDATE orders SET created_at = ? WHERE id = ?",
                (f"{old} 12:00:00", o1["id"]),
            )
            conn.execute(
                "UPDATE orders SET created_at = ? WHERE id = ?",
                (f"{older} 12:00:00", o2["id"]),
            )
        start = (today - timedelta(days=15)).isoformat()
        end = today.isoformat()
        text, filename = webpanel.api_order_history_txt(self.tok, start, end)
        self.assertIn(f"orders_{SHOP}_{start}_{end}.txt", filename)
        self.assertIn(o1["payment_code"], text)
        self.assertIn("BPC-157 10MG", text)
        self.assertIn("Alice A", text)
        # o2 is outside range
        self.assertNotIn(o2["payment_code"], text)
        self.assertIn("× 1", text)

    def test_history_default_range_last_30_days(self):
        o = self._order()
        text, filename = webpanel.api_order_history_txt(self.tok, None, None)
        self.assertIn(str(SHOP), filename)
        self.assertIn(o["payment_code"], text)
        self.assertTrue(filename.startswith(f"orders_{SHOP}_"))
        self.assertTrue(filename.endswith(".txt"))

    def test_history_scoped_to_shop(self):
        mine = self._order()
        foreign = self._order(shop=OTHER_SHOP, pid=self.other_pid)
        text, _ = webpanel.api_order_history_txt(self.tok, "2000-01-01", "2099-12-31")
        self.assertIn(mine["payment_code"], text)
        self.assertNotIn(foreign["payment_code"], text)


class OrdersListTests(PanelOrdersBase):
    def test_api_orders_actionable_first(self):
        paid = self._order()
        db.confirm_order_payment(int(paid["id"]), USER)
        pending = self._order(username="pend")
        code, data = webpanel.api_orders(self.tok, {})
        self.assertEqual(code, 200)
        statuses = [o["status"] for o in data["orders"]]
        # first actionable then paid
        self.assertEqual(statuses[0], "pending_payment")
        self.assertIn("paid", statuses)
        # summaries present
        row = data["orders"][0]
        self.assertIn("BPC-157", row["items_summary"])
        self.assertEqual(row["customer"]["user_id"], CUSTOMER)


class TrackingUrlTests(unittest.TestCase):
    def test_known_carriers(self):
        self.assertIn("ups.com", webpanel.tracking_url("UPS", "1Z123") or "")
        self.assertIn("usps.com", webpanel.tracking_url("USPS", "9400") or "")
        self.assertIn("fedex.com", webpanel.tracking_url("FedEx", "123") or "")
        self.assertIn("dhl.com", webpanel.tracking_url("DHL", "123") or "")
        self.assertIsNone(webpanel.tracking_url("OnTrac", "123"))
        self.assertIsNone(webpanel.tracking_url("UPS", ""))


if __name__ == "__main__":
    unittest.main()
