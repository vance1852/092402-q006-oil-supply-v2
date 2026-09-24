from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from oil_supply.api import JsonApplication
from oil_supply.clock import FrozenClock
from oil_supply.errors import Conflict, Forbidden, InvalidState
from oil_supply.gasoline import GasolineService
from oil_supply.pricing import (
    PricingRule,
    QuotePoint,
    evaluate_window,
    quantize_price,
)


def rule_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "index_weights": {"BRENT": "1"},
        "window_sessions": 3,
        "change_threshold_per_liter": "0.05",
        "vat_rate": "0.13",
        "products": {
            "gasoline-92": {
                "conversion_factor": "0.060",
                "processing_margin_per_liter": "1.00",
                "consumption_tax_per_liter": "1.52",
            },
            "gasoline-95": {
                "conversion_factor": "0.063",
                "processing_margin_per_liter": "1.20",
                "consumption_tax_per_liter": "1.52",
            },
        },
        "carry_below_threshold": True,
        "carry_on_suspend": True,
    }
    payload.update(overrides)
    return payload


def points(*pairs: tuple[str, str]) -> dict[str, list[QuotePoint]]:
    return {"BRENT": [QuotePoint("BRENT", day, index, Decimal(close)) for index, (day, close) in enumerate(pairs, start=1)]}


class PricingRuleTests(unittest.TestCase):
    def test_snapshot_hash_is_complete_and_stable(self) -> None:
        first = PricingRule.from_dict(rule_payload())
        second = PricingRule.from_dict(rule_payload())
        self.assertEqual(first.sha256, second.sha256)
        older = PricingRule.from_dict(rule_payload(products={
            "gasoline-92": {"conversion_factor": "0.060", "processing_margin_per_liter": "1.00", "consumption_tax_per_liter": "1.40"},
            "gasoline-95": {"conversion_factor": "0.063", "processing_margin_per_liter": "1.20", "consumption_tax_per_liter": "1.40"},
        }))
        self.assertNotEqual(first.sha256, older.sha256)

    def test_missing_product_rule_rejected(self) -> None:
        raw = rule_payload()
        del raw["products"]["gasoline-95"]  # type: ignore[union-attr]
        with self.assertRaises(Exception):
            PricingRule.from_dict(raw)

    def test_rounding_is_half_up_to_cent(self) -> None:
        self.assertEqual(quantize_price(Decimal("9.605")), Decimal("9.61"))
        self.assertEqual(quantize_price(Decimal("9.6049")), Decimal("9.60"))

    def test_threshold_hold_accumulates_then_moves_once(self) -> None:
        rule = PricingRule.from_dict(rule_payload(window_sessions=2))
        base = {"gasoline-92": Decimal("9.63"), "gasoline-95": Decimal("10.19")}
        zero = {"gasoline-92": Decimal("0"), "gasoline-95": Decimal("0")}
        first = evaluate_window(
            rule, points(("2026-09-01", "100"), ("2026-09-02", "101")),
            base, zero, "2026-09-02", base,
        )
        self.assertEqual(first["products"]["gasoline-92"]["decision"], "held")
        self.assertEqual(first["products"]["gasoline-92"]["outgoing_change"], "0.03")
        second = evaluate_window(
            rule, points(("2026-09-03", "101"), ("2026-09-04", "101")),
            base, {"gasoline-92": Decimal("0.03"), "gasoline-95": Decimal("0")},
            "2026-09-04",
            {"gasoline-92": Decimal("9.66"), "gasoline-95": Decimal("10.19")},
        )
        product = second["products"]["gasoline-92"]
        self.assertEqual(product["decision"], "moved")
        self.assertEqual(product["applied_change"], "0.07")
        self.assertEqual(product["resulting_per_liter"], "9.70")
        self.assertEqual(product["outgoing_change"], "0.00")

    def test_ceiling_suspends_and_carry_flag_is_deterministic(self) -> None:
        suspended = PricingRule.from_dict(rule_payload(window_sessions=2, suspend_basket_ceiling_usd="130"))
        base = {"gasoline-92": Decimal("9.63"), "gasoline-95": Decimal("10.19")}
        window = evaluate_window(
            suspended, points(("2026-09-01", "130"), ("2026-09-02", "132")),
            base, {"gasoline-92": Decimal("0.10"), "gasoline-95": Decimal("0")},
            "2026-09-02",
            {"gasoline-92": Decimal("9.70"), "gasoline-95": Decimal("10.19")},
        )
        self.assertTrue(window["suspended"])
        self.assertEqual(window["suspension_reason"], "ceiling")
        self.assertEqual(window["products"]["gasoline-92"]["decision"], "suspended")
        self.assertEqual(window["products"]["gasoline-92"]["resulting_per_liter"], "9.63")
        self.assertNotEqual(window["products"]["gasoline-92"]["outgoing_change"], "0.00")
        dropped = PricingRule.from_dict(rule_payload(window_sessions=2, suspend_basket_ceiling_usd="130", carry_on_suspend=False))
        window_dropped = evaluate_window(
            dropped, points(("2026-09-01", "130"), ("2026-09-02", "132")),
            base, {"gasoline-92": Decimal("0.10"), "gasoline-95": Decimal("0")},
            "2026-09-02",
            {"gasoline-92": Decimal("9.70"), "gasoline-95": Decimal("10.19")},
        )
        self.assertEqual(window_dropped["products"]["gasoline-92"]["outgoing_change"], "0.00")

    def test_floor_suspends_when_basket_at_or_below_floor(self) -> None:
        rule = PricingRule.from_dict(rule_payload(window_sessions=2, suspend_basket_floor_usd="40"))
        base = {"gasoline-92": Decimal("7.50"), "gasoline-95": Decimal("8.10")}
        zero = {"gasoline-92": Decimal("0"), "gasoline-95": Decimal("0")}
        window = evaluate_window(
            rule, points(("2026-09-01", "40"), ("2026-09-02", "39")),
            base, zero, "2026-09-02", base,
        )
        self.assertEqual(window["suspension_reason"], "floor")
        self.assertEqual(window["outcome"], "suspended")

    def test_hold_without_carry_resets_unadjusted_amount(self) -> None:
        rule = PricingRule.from_dict(rule_payload(window_sessions=2, carry_below_threshold=False))
        base = {"gasoline-92": Decimal("9.63"), "gasoline-95": Decimal("10.19")}
        window = evaluate_window(
            rule, points(("2026-09-01", "100"), ("2026-09-02", "101")),
            base, {"gasoline-92": Decimal("0"), "gasoline-95": Decimal("0")},
            "2026-09-02", base,
        )
        self.assertEqual(window["products"]["gasoline-92"]["decision"], "held")
        self.assertEqual(window["products"]["gasoline-92"]["outgoing_change"], "0.00")


class GasolineServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
        self.service = GasolineService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("risk", "risk"), ("audit", "auditor"), ("dispatch", "dispatcher")):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def quote(self, day: int, close: str, revision: str | None = None) -> None:
        self.service.record_quote("plan", {
            "price_index": "BRENT",
            "trade_date": f"2026-09-{day:02d}",
            "close_usd": close,
            "source_revision": revision or f"rev-{day:02d}",
            "observed_at": f"2026-09-{day:02d}T21:00:00Z",
        })

    def prepare_strategy(self, strategy_id: str = "gas", **rule_overrides: object) -> dict[str, object]:
        self.service.create_gasoline_strategy("plan", {
            "strategy_id": strategy_id,
            "name": "汽油指导价策略",
            "price_indexes": ["BRENT"],
            "baseline_92": "9.63",
            "baseline_95": "10.19",
            "baseline_anchor_date": "2026-09-01",
        })
        version = self.service.add_rule_version(
            "plan", strategy_id, "2026-09-01", rule_payload(**rule_overrides)
        )
        revision = self.service.gasoline_strategy(strategy_id)["revision"]
        self.service.submit_strategy_for_review("plan", strategy_id, revision)
        self.service.approve_strategy("risk", strategy_id, revision + 1, "2026-09-01T00:00:00Z")
        self.service.activate_due_strategies("risk")
        return version

    def test_rule_intervals_cannot_overlap_and_earlier_start_closes_previous(self) -> None:
        self.service.create_gasoline_strategy("plan", {
            "strategy_id": "gas", "name": "策略", "price_indexes": ["BRENT"],
            "baseline_92": "9.63", "baseline_95": "10.19", "baseline_anchor_date": "2026-09-01",
        })
        self.service.add_rule_version("plan", "gas", "2026-09-01", rule_payload())
        self.service.add_rule_version("plan", "gas", "2026-10-01", rule_payload(vat_rate="0.12"))
        with self.assertRaises(Conflict):
            self.service.add_rule_version("plan", "gas", "2026-09-15", rule_payload())
        earlier = self.service.add_rule_version("plan", "gas", "2026-08-01", rule_payload(vat_rate="0.17"))
        versions = self.service.gasoline_strategy("gas")["rule_versions"]
        intervals = {(row["effective_from"], row["effective_to"]) for row in versions}
        self.assertEqual(intervals, {
            ("2026-08-01", "2026-09-01"),
            ("2026-09-01", "2026-10-01"),
            ("2026-10-01", None),
        })
        self.assertEqual(earlier["effective_to"], "2026-09-01")

    def test_review_requires_covering_rule_and_approver_is_segregated(self) -> None:
        self.service.create_gasoline_strategy("plan", {
            "strategy_id": "late", "name": "晚", "price_indexes": ["BRENT"],
            "baseline_92": "9.63", "baseline_95": "10.19", "baseline_anchor_date": "2026-09-01",
        })
        self.service.add_rule_version("plan", "late", "2026-09-10", rule_payload())
        revision = self.service.gasoline_strategy("late")["revision"]
        with self.assertRaises(InvalidState):
            self.service.submit_strategy_for_review("plan", "late", revision)
        self.service.add_rule_version("plan", "late", "2026-09-01", rule_payload())
        revision = self.service.gasoline_strategy("late")["revision"]
        self.service.submit_strategy_for_review("plan", "late", revision)
        with self.assertRaises(Forbidden):
            self.service.approve_strategy("plan", "late", revision + 1, "2026-09-01T00:00:00Z")
        self.service.reject_strategy("risk", "late", revision + 1, "口径待补")
        self.assertEqual(self.service.gasoline_strategy("late")["state"], "draft")

    def test_scheduled_activation_waits_for_time_and_single_active(self) -> None:
        self.service.create_gasoline_strategy("plan", {
            "strategy_id": "a", "name": "甲", "price_indexes": ["BRENT"],
            "baseline_92": "9.63", "baseline_95": "10.19", "baseline_anchor_date": "2026-09-01",
        })
        self.service.add_rule_version("plan", "a", "2026-09-01", rule_payload())
        revision = self.service.gasoline_strategy("a")["revision"]
        self.service.submit_strategy_for_review("plan", "a", revision)
        self.service.approve_strategy("risk", "a", revision + 1, "2026-09-10T00:00:00Z")
        self.assertEqual(self.service.activate_due_strategies("risk")["activated"], [])
        self.clock.advance(days=20)
        self.assertEqual(self.service.activate_due_strategies("risk")["activated"], ["a"])
        self.service.create_gasoline_strategy("plan", {
            "strategy_id": "b", "name": "乙", "price_indexes": ["BRENT"],
            "baseline_92": "9.63", "baseline_95": "10.19", "baseline_anchor_date": "2026-09-01",
        })
        self.service.add_rule_version("plan", "b", "2026-09-01", rule_payload())
        revision = self.service.gasoline_strategy("b")["revision"]
        self.service.submit_strategy_for_review("plan", "b", revision)
        self.service.approve_strategy("risk", "b", revision + 1, "2026-09-01T00:00:00Z")
        result = self.service.activate_due_strategies("risk")
        self.assertEqual(result["activated"], [])
        self.assertTrue(result["deferred"])

    def test_preview_does_not_persist_but_publish_is_immutable_and_idempotent(self) -> None:
        self.prepare_strategy()
        for day, close in zip(range(1, 7), ["100", "100", "100", "99.7", "99.7", "99.7"]):
            self.quote(day, close)
        preview = self.service.preview_gasoline_decision("plan", "gas", "2026-09-03")
        self.assertFalse(preview["persisted"])
        self.assertEqual(self.service.gasoline_decisions("gas", "plan")["decisions"], [])
        published = self.service.publish_gasoline_decision("plan", "gas", "2026-09-03")
        replayed = self.service.publish_gasoline_decision("plan", "gas", "2026-09-03")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["decision_id"], published["decision_id"])
        self.assertEqual(replayed["input_sha256"], published["input_sha256"])
        locked = published["result"]["locked_quotes"]
        self.assertEqual({(q["trade_date"], q["quote_id"]) for q in locked}, {
            ("2026-09-01", 1), ("2026-09-02", 2), ("2026-09-03", 3),
        })
        before_audit = self.service.audit_chain("audit")["events"]
        self.service.preview_gasoline_decision("plan", "gas", "2026-09-03")
        self.assertEqual(self.service.audit_chain("audit")["events"], before_audit)

    def test_carry_accumulates_across_windows_and_rule_version_is_date_keyed(self) -> None:
        self.prepare_strategy()
        for day, close in zip(range(1, 13), [
            "100", "100", "100",
            "99.7", "99.7", "99.7",
            "99.4", "99.4", "99.4",
            "99.0", "99.0", "99.0",
        ]):
            self.quote(day, close)
        first = self.service.publish_gasoline_decision("plan", "gas", "2026-09-03")
        second = self.service.publish_gasoline_decision("plan", "gas", "2026-09-06")
        third = self.service.publish_gasoline_decision("plan", "gas", "2026-09-09")
        fourth = self.service.publish_gasoline_decision("plan", "gas", "2026-09-12")
        self.assertEqual(first["result"]["products"]["gasoline-92"]["decision"], "held")
        self.assertEqual(second["result"]["products"]["gasoline-92"]["outgoing_change"], "-0.02")
        self.assertEqual(third["result"]["products"]["gasoline-92"]["outgoing_change"], "-0.04")
        self.assertEqual(fourth["result"]["products"]["gasoline-92"]["decision"], "moved")
        self.assertEqual(fourth["result"]["products"]["gasoline-92"]["applied_change"], "-0.07")
        self.assertEqual(fourth["result"]["products"]["gasoline-92"]["resulting_per_liter"], "9.56")
        # 新政策自 2026-09-10 起提高定额税；去年的决定仍锁定旧快照。
        self.service.retire_strategy(
            "risk", "gas", self.service.gasoline_strategy("gas")["revision"]
        )
        with self.assertRaises(InvalidState):
            self.service.publish_gasoline_decision("plan", "gas", "2026-09-15")

    def test_out_of_order_publish_is_rejected(self) -> None:
        self.prepare_strategy()
        for day in range(1, 13):
            self.quote(day, "100")
        self.service.publish_gasoline_decision("plan", "gas", "2026-09-06")
        with self.assertRaises(InvalidState):
            self.service.publish_gasoline_decision("plan", "gas", "2026-09-03")

    def test_quote_correction_flags_decisions_and_recalculates_without_rewriting(self) -> None:
        self.prepare_strategy()
        for day, close in zip(range(1, 13), [
            "100", "100", "100",
            "99.7", "99.7", "99.7",
            "99.4", "99.4", "99.4",
            "99", "99", "99",
        ]):
            self.quote(day, close)
        first = self.service.publish_gasoline_decision("plan", "gas", "2026-09-03")
        self.service.publish_gasoline_decision("plan", "gas", "2026-09-06")
        self.service.publish_gasoline_decision("plan", "gas", "2026-09-09")
        self.service.publish_gasoline_decision("plan", "gas", "2026-09-12")
        self.quote(2, "99.0", revision="rev-02-corrected")
        corrections = self.service.quote_corrections("plan")["corrections"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["affected_decision_ids"], [first["decision_id"]])
        suggestion = self.service.recalculation_suggestion("plan", corrections[0]["correction_id"])
        anchors = [(row["anchor_date"], row["directly_affected"], row["changed"]) for row in suggestion["suggestions"]]
        # 直接受影响日与下一周期的增量不同；累计在后续窗口重新收敛，
        # 最终决定与已发布一致，不再被标记为变化。
        self.assertEqual(anchors, [
            ("2026-09-03", True, True),
            ("2026-09-06", False, True),
            ("2026-09-09", False, False),
            ("2026-09-12", False, False),
        ])
        final = suggestion["suggestions"][-1]
        self.assertEqual(final["suggested_products"]["gasoline-92"]["resulting_per_liter"], "9.56")
        # 历史决定与重复发布返回保持不变。
        historical = self.service.gasoline_decision(first["decision_id"], "plan")
        self.assertEqual(historical["result"]["products"]["gasoline-92"]["expected_per_liter"], "9.63")
        replay = self.service.publish_gasoline_decision("plan", "gas", "2026-09-03")
        self.assertEqual(replay["decision_id"], first["decision_id"])
        self.service.resolve_quote_correction("risk", corrections[0]["correction_id"], "reviewed")

    def test_correction_for_unlocked_quote_creates_no_flag(self) -> None:
        self.prepare_strategy()
        for day in range(1, 7):
            self.quote(day, "100")
        self.service.publish_gasoline_decision("plan", "gas", "2026-09-06")
        self.quote(1, "99", revision="rev-01-corrected")  # 窗口 04-06 未锁定 09-01
        self.assertEqual(self.service.quote_corrections("plan")["corrections"], [])

    def test_historical_anchor_uses_rule_effective_that_day(self) -> None:
        self.service.create_gasoline_strategy("plan", {
            "strategy_id": "dated", "name": "跨期", "price_indexes": ["BRENT"],
            "baseline_92": "9.63", "baseline_95": "10.19", "baseline_anchor_date": "2026-09-01",
        })
        old = self.service.add_rule_version("plan", "dated", "2026-09-01", rule_payload())
        new = self.service.add_rule_version("plan", "dated", "2026-09-10", rule_payload(
            products={
                "gasoline-92": {"conversion_factor": "0.060", "processing_margin_per_liter": "1.00", "consumption_tax_per_liter": "1.80"},
                "gasoline-95": {"conversion_factor": "0.063", "processing_margin_per_liter": "1.20", "consumption_tax_per_liter": "1.80"},
            }
        ))
        revision = self.service.gasoline_strategy("dated")["revision"]
        self.service.submit_strategy_for_review("plan", "dated", revision)
        self.service.approve_strategy("risk", "dated", revision + 1, "2026-09-01T00:00:00Z")
        self.service.activate_due_strategies("risk")
        for day in range(1, 13):
            self.quote(day, "100")
        september = self.service.publish_gasoline_decision("plan", "dated", "2026-09-03")
        later = self.service.publish_gasoline_decision("plan", "dated", "2026-09-12")
        self.assertEqual(september["rule_version_id"], old["rule_version_id"])
        self.assertEqual(september["result"]["products"]["gasoline-92"]["expected_per_liter"], "9.63")
        self.assertEqual(later["rule_version_id"], new["rule_version_id"])
        self.assertNotEqual(
            later["result"]["products"]["gasoline-92"]["expected_per_liter"],
            september["result"]["products"]["gasoline-92"]["expected_per_liter"],
        )


class GasolineApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
        self.service = GasolineService(self.connection, self.clock)
        self.app = JsonApplication(self.service)
        for user_id, role in (("plan", "planner"), ("risk", "risk")):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, actor: str, payload: dict[str, object]) -> object:
        response = self.app.handle(
            "POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8")
        )
        self.assertLess(response.status, 500, response.body)
        return response

    def test_strategy_lifecycle_over_http(self) -> None:
        created = self._post("/gasoline/strategies", "plan", {
            "strategy_id": "web", "name": "网页策略", "price_indexes": ["BRENT"],
            "baseline_92": "9.63", "baseline_95": "10.19", "baseline_anchor_date": "2026-09-01",
        })
        self.assertEqual(created.status, 201)
        rules = self._post("/gasoline/strategies/web/rules", "plan", {
            "effective_from": "2026-09-01", "rule": rule_payload(),
        })
        self.assertEqual(rules.status, 201)
        revision = self.service.gasoline_strategy("web")["revision"]
        submitted = self._post("/gasoline/strategies/web/submit", "plan", {"expected_revision": revision})
        self.assertEqual(submitted.status, 200)
        approved = self._post("/gasoline/strategies/web/approve", "risk", {
            "expected_revision": revision + 1, "scheduled_publish_at": "2026-09-01T00:00:00Z",
        })
        self.assertEqual(approved.status, 200)
        forbidden = self._post("/gasoline/strategies", "risk", {
            "strategy_id": "x", "name": "无权", "price_indexes": ["BRENT"],
            "baseline_92": "9.63", "baseline_95": "10.19", "baseline_anchor_date": "2026-09-01",
        })
        self.assertEqual(forbidden.status, 403)
        missing = self.app.handle("GET", "/gasoline/strategies/nope", {"X-Actor-Id": "plan"})
        self.assertEqual(missing.status, 404)


if __name__ == "__main__":
    unittest.main()
