"""SPBC website orders must not land in Unicorn Magic Factory.

#11 stopped quoting her. #6 would insert a paid Unicorn shop order and deduct
her stock. Those conflict. The cut on master wins: quote/apply refuse Unicorn,
and paid POST /notify does not create an order there. Scratch DB only.
"""

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
import order_router  # noqa: E402
import spbc_notify  # noqa: E402

UNICORN = 91001
PATRIOT = 91002
OTHER = 91003
SPBC = 91004


class SpbcUnicornRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "route.db")
        db.init_db()
        order_router._pending.clear()
        db.ensure_shop(UNICORN, title="@unicornmagicfactory")
        db.ensure_shop(PATRIOT, title="Patriotic Peptides")
        db.ensure_shop(OTHER, title="Show Me Source")
        db.ensure_shop(SPBC, title="SPBC Shop")
        self.u_pid = db.add_product(UNICORN, "RETA 35 MG", 10.0, 40)
        self.p_pid = db.add_product(PATRIOT, "RETA 35 MG", 30.0, 8)
        db.add_product(OTHER, "RETA 35 MG", 18.0, 12)
        db.add_product(SPBC, "RETA 35 MG", 1.0, 999)
        self._cfg = mock.patch.object(order_router, "SPBC_SHOP_CHAT_ID", SPBC)
        self._cfg.start()
        self._env = mock.patch.dict(
            "os.environ", {"UNICORN_SHOP_CHAT_ID": ""}, clear=False
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._cfg.stop()
        order_router._pending.clear()
        self._tmp.cleanup()

    def _lines(self):
        parsed = order_router.parse_line({"name": "RETA 35 MG (Vial)", "qty": 2})
        self.assertIsNotNone(parsed)
        return [parsed]

    def _payload(self):
        return {
            "order_number": "PEP-RECON-1",
            "status": "paid",
            "items": [
                {
                    "name": "RETA 35 MG (Vial)",
                    "qty": 2,
                    "supplier": "Show Me Source",
                    "telegram_chat_id": "111",
                }
            ],
            "total_cents": 8000,
            "shipping": {"name": "Jane", "line1": "1 Main"},
        }

    def test_quote_shop_and_compute_quotes_skip_unicorn(self) -> None:
        self.assertTrue(
            order_router.spbc_routing_skips_shop(UNICORN, "@unicornmagicfactory")
        )
        self.assertIsNone(order_router.quote_shop(UNICORN, self._lines()))
        quotes = order_router.compute_quotes(self._payload())
        ids = [q["shop_chat_id"] for q in quotes]
        self.assertEqual(ids[0], OTHER)
        self.assertIn(PATRIOT, ids)
        self.assertNotIn(UNICORN, ids)
        self.assertNotIn(SPBC, ids)

    def test_env_shop_id_is_skipped_without_unicorn_title(self) -> None:
        quiet = 91099
        db.ensure_shop(quiet, title="Shelf")
        db.add_product(quiet, "RETA 35 MG", 4.0, 20)
        with mock.patch.dict("os.environ", {"UNICORN_SHOP_CHAT_ID": str(quiet)}):
            self.assertIsNone(order_router.quote_shop(quiet, self._lines()))
            ids = [
                q["shop_chat_id"]
                for q in order_router.compute_quotes(self._payload())
            ]
        self.assertNotIn(quiet, ids)
        self.assertIn(PATRIOT, ids)

    def test_only_unicorn_complete_fill_returns_no_quotes(self) -> None:
        with db.get_db() as conn:
            conn.execute(
                "UPDATE products SET active = 0 WHERE chat_id != ?", (UNICORN,)
            )
        self.assertEqual(order_router.compute_quotes(self._payload()), [])
        self.assertIsNone(order_router.suggest_for_order(self._payload()))

    def test_paid_notify_does_not_write_unicorn_order_or_stock(self) -> None:
        stock_before = int(db.get_product(self.u_pid)["stock"])
        patriot_before = int(db.get_product(self.p_pid)["stock"])
        with mock.patch.object(
            spbc_notify, "send_telegram", return_value={"message_id": 1}
        ), mock.patch.object(
            spbc_notify, "_telegram_api", return_value={"ok": True, "result": {}}
        ):
            code, body = spbc_notify.handle_notify(self._payload())
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"))
        self.assertNotIn("unicorn_order", body)
        self.assertEqual(int(db.get_product(self.u_pid)["stock"]), stock_before)
        self.assertEqual(int(db.get_product(self.p_pid)["stock"]), patriot_before)
        with db.get_db() as conn:
            rows = conn.execute("SELECT chat_id, status FROM orders").fetchall()
        self.assertEqual(list(rows), [])


if __name__ == "__main__":
    unittest.main()
