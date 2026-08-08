"""Quote-and-suggest routing: line parsing, quoting, suggestion, apply."""

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

VENDOR_A = 1001
VENDOR_B = 1002
MASTER = 6086


def order_payload(items):
    return {
        "order_number": "PEP-TEST-1",
        "status": "paid",
        "items": items,
        "shipping": {"name": "Jane", "line1": "1 Main St", "city": "Springfield"},
    }


class RouterBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "route.db")
        db.init_db()
        order_router._pending.clear()
        db.ensure_shop(MASTER, title="SPBC Shop")
        db.ensure_shop(VENDOR_A, title="Vendor A")
        db.ensure_shop(VENDOR_B, title="Vendor B")
        # Vendor A: cheap RETA, has BAC; kit price on SEMA
        self.a_reta = db.add_product(VENDOR_A, "RETA 35 MG", 150.0, 10)
        self.a_bac = db.add_product(VENDOR_A, "BAC WATER 3ML", 4.0, 50)
        self.a_sema = db.add_product(VENDOR_A, "SEMA 10MG", 40.0, 40)
        db.update_product(self.a_sema, kit_price=350.0)
        # Vendor B: pricier RETA, has BAC too
        self.b_reta = db.add_product(VENDOR_B, "RETA 35 MG", 170.0, 10)
        self.b_bac = db.add_product(VENDOR_B, "BAC WATER 3ML", 5.0, 50)
        # Master shop has everything cheap — must be excluded
        db.add_product(MASTER, "RETA 35 MG", 1.0, 999)
        db.add_product(MASTER, "BAC WATER 3ML", 1.0, 999)
        self._cfg = mock.patch.object(order_router, "SPBC_SHOP_CHAT_ID", MASTER)
        self._cfg.start()

    def tearDown(self) -> None:
        self._cfg.stop()
        order_router._pending.clear()
        self._tmp.cleanup()


class ParseLineTests(unittest.TestCase):
    def test_vial_suffix_stripped(self):
        ln = order_router.parse_line({"name": "RETA 35 MG (Vial)", "qty": 3})
        self.assertEqual(ln, {"base": "RETA 35 MG", "qty": 3, "kind": "vial"})

    def test_kit_suffix_detected(self):
        for suffix in ("(Kit)", "(10-Pack)", "(10-Pack / Kit)"):
            ln = order_router.parse_line({"name": f"SEMA 10MG {suffix}", "qty": 1})
            self.assertEqual(ln["kind"], "kit")
            self.assertEqual(ln["base"], "SEMA 10MG")

    def test_bad_lines_dropped(self):
        self.assertIsNone(order_router.parse_line({"name": "", "qty": 2}))
        self.assertIsNone(order_router.parse_line({"name": "X", "qty": 0}))


class QuoteTests(RouterBase):
    def test_cheapest_vendor_first_and_master_excluded(self):
        payload = order_payload(
            [{"name": "RETA 35 MG (Vial)", "qty": 2},
             {"name": "BAC WATER 3ML", "qty": 3}]
        )
        quotes = order_router.compute_quotes(payload)
        self.assertEqual([q["shop_chat_id"] for q in quotes], [VENDOR_A, VENDOR_B])
        self.assertEqual(quotes[0]["total"], 2 * 150.0 + 3 * 4.0)
        self.assertNotIn(MASTER, [q["shop_chat_id"] for q in quotes])

    def test_vendor_missing_item_disqualified(self):
        payload = order_payload(
            [{"name": "SEMA 10MG (Vial)", "qty": 1},
             {"name": "RETA 35 MG (Vial)", "qty": 1}]
        )
        quotes = order_router.compute_quotes(payload)
        # Only Vendor A stocks SEMA
        self.assertEqual([q["shop_chat_id"] for q in quotes], [VENDOR_A])

    def test_insufficient_stock_disqualified(self):
        payload = order_payload([{"name": "RETA 35 MG (Vial)", "qty": 11}])
        self.assertEqual(order_router.compute_quotes(payload), [])

    def test_kit_uses_kit_price_and_vial_stock(self):
        payload = order_payload([{"name": "SEMA 10MG (Kit)", "qty": 2}])
        quotes = order_router.compute_quotes(payload)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["total"], 700.0)  # 2 × kit_price 350
        self.assertEqual(quotes[0]["breakdown"][0]["deduct"], 20)  # 2 × KIT_SIZE

    def test_kit_without_kit_price_falls_back(self):
        db.update_product(self.a_sema, kit_price=None)
        payload = order_payload([{"name": "SEMA 10MG (Kit)", "qty": 1}])
        quotes = order_router.compute_quotes(payload)
        self.assertEqual(quotes[0]["total"], 400.0)  # 10 × vial 40

    def test_inactive_product_disqualifies(self):
        db.update_product(self.b_reta, active=0)
        payload = order_payload([{"name": "RETA 35 MG (Vial)", "qty": 1}])
        quotes = order_router.compute_quotes(payload)
        self.assertEqual([q["shop_chat_id"] for q in quotes], [VENDOR_A])


class SuggestApplyTests(RouterBase):
    def test_suggest_registers_and_builds_buttons(self):
        payload = order_payload([{"name": "RETA 35 MG (Vial)", "qty": 1}])
        spec = order_router.suggest_for_order(payload)
        self.assertIn("Vendor A", spec["text"])
        buttons = spec["reply_markup"]["inline_keyboard"]
        # 2 vendor buttons + dismiss
        self.assertEqual(len(buttons), 3)
        self.assertTrue(buttons[0][0]["callback_data"].startswith("routeq:"))

    def test_no_quotes_returns_none(self):
        payload = order_payload([{"name": "UNKNOWN 1MG", "qty": 1}])
        self.assertIsNone(order_router.suggest_for_order(payload))

    def _register_one(self):
        payload = order_payload(
            [{"name": "RETA 35 MG (Vial)", "qty": 2},
             {"name": "BAC WATER 3ML", "qty": 3}]
        )
        quotes = order_router.compute_quotes(payload)
        reg = order_router.register_quotes(payload, quotes)
        return reg[0]  # cheapest = Vendor A

    def test_apply_deducts_and_audits(self):
        qid, _ = self._register_one()
        ok, msg, quote = order_router.apply_route(qid, actor_id=99)
        self.assertTrue(ok, msg)
        self.assertEqual(db.get_product(self.a_reta)["stock"], 8)
        self.assertEqual(db.get_product(self.a_bac)["stock"], 47)
        with db.get_db() as conn:
            audits = conn.execute(
                "SELECT * FROM stock_audit WHERE reason='order_route'"
            ).fetchall()
        self.assertEqual(len(audits), 2)
        self.assertEqual(audits[0]["actor_id"], 99)

    def test_apply_is_idempotent(self):
        qid, _ = self._register_one()
        ok1, _, _ = order_router.apply_route(qid, 99)
        ok2, msg2, _ = order_router.apply_route(qid, 99)
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertIn("Already", msg2)
        self.assertEqual(db.get_product(self.a_reta)["stock"], 8)

    def test_apply_fails_cleanly_if_stock_changed(self):
        qid, _ = self._register_one()
        db.update_product(self.a_reta, stock=1)  # sold out meanwhile
        ok, msg, _ = order_router.apply_route(qid, 99)
        self.assertFalse(ok)
        self.assertIn("Stock changed", msg)
        # nothing deducted anywhere
        self.assertEqual(db.get_product(self.a_bac)["stock"], 50)
        # quote can be retried after restock
        db.update_product(self.a_reta, stock=5)
        ok2, _, _ = order_router.apply_route(qid, 99)
        self.assertTrue(ok2)

    def test_vendor_message_contents(self):
        qid, _ = self._register_one()
        ok, _, quote = order_router.apply_route(qid, 99)
        text = order_router.build_vendor_message(quote)
        self.assertIn("PEP-TEST-1", text)
        self.assertIn("2× RETA 35 MG", text)
        self.assertIn("$312.00", text)
        self.assertIn("Jane", text)

    def test_offer_does_not_move_stock_or_leak_address(self):
        qid, _ = self._register_one()
        ok, msg, quote = order_router.offer_quote(qid)
        self.assertTrue(ok, msg)
        self.assertEqual(quote["state"], order_router.OFFERED)
        # nothing deducted yet
        self.assertEqual(db.get_product(self.a_reta)["stock"], 10)
        self.assertEqual(db.get_product(self.a_bac)["stock"], 50)
        # the offer text must NOT contain the shipping address
        text = order_router.build_vendor_offer(quote)
        self.assertNotIn("Jane", text)
        self.assertNotIn("1 Main St", text)
        self.assertIn("RETA 35 MG", text)
        self.assertIn("$312.00", text)
        # ...but the post-accept message does
        ok2, _, q2 = order_router.apply_route(qid, actor_id=5)
        self.assertTrue(ok2)
        full = order_router.build_vendor_message(q2)
        self.assertIn("1 Main St", full)

    def test_accept_requires_vendor_shop_admin(self):
        qid, _ = self._register_one()
        order_router.offer_quote(qid)
        stranger = 4242
        allowed, why, _ = order_router.can_accept(qid, stranger)
        self.assertFalse(allowed)
        db.add_admin(VENDOR_A, stranger, "vend", stranger)
        allowed2, _, _ = order_router.can_accept(qid, stranger)
        self.assertTrue(allowed2)

    def test_decline_leaves_stock_untouched_and_offers_alternative(self):
        payload = order_payload(
            [{"name": "RETA 35 MG (Vial)", "qty": 1},
             {"name": "BAC WATER 3ML", "qty": 1}]
        )
        quotes = order_router.compute_quotes(payload)
        reg = order_router.register_quotes(payload, quotes)
        qid_a, _ = reg[0]
        order_router.offer_quote(qid_a)
        ok, _, q = order_router.decline_quote(qid_a, actor_id=1, reason="no stock")
        self.assertTrue(ok)
        self.assertEqual(q["state"], order_router.DECLINED)
        self.assertEqual(db.get_product(self.a_reta)["stock"], 10)
        # Vendor B is still available as the next option
        alts = order_router.alternatives_for("PEP-TEST-1", qid_a)
        self.assertTrue(alts)
        self.assertEqual(alts[0][1]["shop_chat_id"], VENDOR_B)
        # a declined quote can't then be accepted
        ok2, _, _ = order_router.apply_route(qid_a, 1)
        self.assertTrue(ok2)  # apply_route is the owner-override path

    def test_expired_offer_reported_once(self):
        qid, _ = self._register_one()
        order_router.offer_quote(qid)
        first = order_router.expire_offer(qid)
        self.assertIsNotNone(first)
        self.assertIsNone(order_router.expire_offer(qid))

    def test_owner_suggestion_shows_margin(self):
        payload = order_payload([{"name": "RETA 35 MG (Vial)", "qty": 1}])
        payload["total_cents"] = 24000
        quotes = order_router.compute_quotes(payload)
        reg = order_router.register_quotes(payload, quotes)
        spec = order_router.build_owner_suggestion("PEP-TEST-1", reg)
        self.assertIn("Customer paid: $240.00", spec["text"])
        self.assertIn("margin $90.00", spec["text"])  # 240 - 150 (Vendor A)
        self.assertIn("Offer to", spec["reply_markup"]["inline_keyboard"][0][0]["text"])

    def test_dismiss_clears_order_quotes(self):
        qid, _ = self._register_one()
        n = order_router.dismiss_order("PEP-TEST-1")
        self.assertGreaterEqual(n, 1)
        self.assertIsNone(order_router.get_quote(qid))


if __name__ == "__main__":
    unittest.main()
