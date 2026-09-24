"""汽油指导价引擎、规则生命周期、不可变决定与修订影响测试。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from fuel_pricing.clock import FrozenClock
from fuel_pricing.engine import (
    cycle_dates,
    money,
    shift_workdays,
    window_workdays,
)
from fuel_pricing.errors import Conflict, Forbidden, InvalidState
from fuel_pricing.service import FuelPricingService
from datetime import date


def rule_document(
    effective_from: str = "2026-01-05",
    *,
    vat: str = "0.13",
    threshold: str = "0.05",
    floor: str | None = None,
    ceiling: str | None = None,
    cycle: int = 10,
    window: int = 5,
    base_92: str = "8.65",
    base_95: str = "9.20",
    spread: str = "3.00",
    fx: str = "7.10",
) -> dict[str, object]:
    def product(base: str) -> dict[str, object]:
        return {
            "base_guide_price": base,
            "processing_spread": spread,
            "min_change_threshold": threshold,
            "floor_price": floor,
            "ceiling_price": ceiling,
            "tax_components": [
                {"name": "消费税", "kind": "fixed", "value": "1.52"},
                {"name": "增值税", "kind": "rate", "value": vat, "base": "cost_plus_fixed"},
            ],
        }

    return {
        "rule_id": "gasoline-guide",
        "price_index": "BRENT",
        "effective_from": effective_from,
        "cycle_anchor_date": effective_from,
        "cycle_workdays": cycle,
        "quote_window_workdays": window,
        "fx_rate_cny_per_usd": fx,
        "products": {"92": product(base_92), "95": product(base_95)},
        "note": "测试政策",
    }


class EngineTests(unittest.TestCase):
    def test_round_half_up_not_bankers(self) -> None:
        self.assertEqual(money(Decimal("1.005")), Decimal("1.01"))
        self.assertEqual(money(Decimal("1.015")), Decimal("1.02"))
        self.assertEqual(money(Decimal("-1.005")), Decimal("-1.01"))

    def test_shift_workdays_skips_weekends(self) -> None:
        self.assertEqual(shift_workdays(date(2026, 1, 16), 1), date(2026, 1, 19))
        self.assertEqual(shift_workdays(date(2026, 1, 19), -10), date(2026, 1, 5))

    def test_window_excludes_evaluation_date(self) -> None:
        dates = window_workdays(date(2026, 1, 19), 5)
        self.assertEqual(
            [day.isoformat() for day in dates],
            ["2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16"],
        )

    def test_ten_workday_cycle_lands_on_january_19(self) -> None:
        matched, previous, aligned = cycle_dates(date(2026, 1, 5), 10, date(2026, 1, 19))
        self.assertTrue(matched)
        self.assertEqual(previous, date(2026, 1, 5))
        matched, _, _ = cycle_dates(date(2026, 1, 5), 10, date(2026, 1, 20))
        self.assertFalse(matched)


class FuelPricingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc))
        self.service = FuelPricingService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("review", "reviewer"),
            ("risk", "risk"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def approve_rule(self, document: dict[str, object], *, as_of: str = "2026-01-05") -> None:
        self.service.create_rule_draft("plan", document)
        self.service.submit_rule("plan", "gasoline-guide", 1, 1)
        self.service.review_rule("review", "gasoline-guide", 1, 2, True)
        self.service.activate_due_rules("review", as_of=as_of)

    def quote_window(self, start_day: int, closes: tuple[str, ...], *, month: str = "01") -> None:
        days = []
        cursor = date(2026, int(month), start_day)
        while len(days) < 5:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor = date.fromordinal(cursor.toordinal() + 1)
        for day, close in zip(days, closes, strict=True):
            self.service.record_quote("plan", {
                "price_index": "BRENT", "trade_date": day.isoformat(),
                "close_usd": close, "source_revision": f"r-{day.isoformat()}",
            })

    # ----- 规则生命周期 ---------------------------------------------------

    def test_rule_draft_submit_review_flow_and_revision_guards(self) -> None:
        self.service.create_rule_draft("plan", rule_document())
        draft = self.service.get_rule_version("gasoline-guide", 1)
        self.assertEqual(draft["state"], "draft")
        # 错误的 expected_revision 不能提交。
        with self.assertRaises(InvalidState):
            self.service.submit_rule("plan", "gasoline-guide", 1, 9)
        self.service.submit_rule("plan", "gasoline-guide", 1, 1)
        # 复核中的版本不能再修改。
        with self.assertRaises(InvalidState):
            self.service.update_rule_draft("plan", "gasoline-guide", 1, rule_document())
        # planner 无权复核。
        with self.assertRaises(Forbidden):
            self.service.review_rule("plan", "gasoline-guide", 1, 2, True)
        self.service.review_rule("review", "gasoline-guide", 1, 2, True)
        activated = self.service.activate_due_rules("review", as_of="2026-01-05")
        self.assertEqual(activated["activated"], ["gasoline-guide:v1"])
        # 重复激活幂等。
        again = self.service.activate_due_rules("review", as_of="2026-01-05")
        self.assertEqual(again["activated"], [])

    def test_rejected_draft_allows_new_version(self) -> None:
        self.service.create_rule_draft("plan", rule_document())
        self.service.submit_rule("plan", "gasoline-guide", 1, 1)
        self.service.review_rule("review", "gasoline-guide", 1, 2, False, "税费计算有误")
        rejected = self.service.get_rule_version("gasoline-guide", 1)
        self.assertEqual(rejected["state"], "rejected")
        self.service.create_rule_draft("plan", rule_document())
        self.assertEqual(self.service.get_rule_version("gasoline-guide", 2)["version"], 2)

    def test_effective_ranges_must_not_overlap(self) -> None:
        self.approve_rule(rule_document("2026-01-05"))
        # 旧版本未退役时批准重叠版本被拒绝。
        self.service.create_rule_draft("plan", rule_document("2026-03-02", vat="0.12"))
        self.service.submit_rule("plan", "gasoline-guide", 2, 1)
        with self.assertRaises(Conflict):
            self.service.review_rule("review", "gasoline-guide", 2, 2, True)
        # 旧版本在新版本生效日半开区间相接退役后可以批准。
        self.service.retire_rule("plan", "gasoline-guide", 1, 3, "2026-03-02")
        self.service.review_rule("review", "gasoline-guide", 2, 2, True)
        rollover = self.service.activate_due_rules("review", as_of="2026-03-02")
        self.assertEqual(rollover["activated"], ["gasoline-guide:v2"])
        self.assertEqual(rollover["retired"], ["gasoline-guide:v1"])
        self.assertEqual(self.service.effective_rule("2026-03-01")["version"], 1)
        self.assertEqual(self.service.effective_rule("2026-03-02")["version"], 2)

    def test_queued_successor_versions_chain_without_overlap(self) -> None:
        self.approve_rule(rule_document("2026-01-05"))
        # v1 预定 2026-03-02 退役，v2 自 2026-03-02 生效。
        self.service.retire_rule("plan", "gasoline-guide", 1, 3, "2026-03-02")
        self.service.create_rule_draft("plan", rule_document("2026-03-02", vat="0.12"))
        self.service.submit_rule("plan", "gasoline-guide", 2, 1)
        self.service.review_rule("review", "gasoline-guide", 2, 2, True)
        # v2 预定 2026-05-04 退役后，v3 才能排队 2026-05-04 生效。
        self.service.retire_rule("plan", "gasoline-guide", 2, 3, "2026-05-04")
        self.service.create_rule_draft("plan", rule_document("2026-05-04", vat="0.11"))
        self.service.submit_rule("plan", "gasoline-guide", 3, 1)
        self.service.review_rule("review", "gasoline-guide", 3, 2, True)
        rollover = self.service.activate_due_rules("review", as_of="2026-05-04")
        self.assertEqual(rollover["activated"], ["gasoline-guide:v2", "gasoline-guide:v3"])
        self.assertEqual(rollover["retired"], ["gasoline-guide:v1", "gasoline-guide:v2"])
        self.assertEqual(self.service.effective_rule("2026-05-03")["version"], 2)
        self.assertEqual(self.service.effective_rule("2026-05-04")["version"], 3)

    def test_cancel_pending_approved_version(self) -> None:
        self.approve_rule(rule_document("2026-01-05"))
        self.service.retire_rule("plan", "gasoline-guide", 1, 3, "2026-03-02")
        self.service.create_rule_draft("plan", rule_document("2026-03-02", vat="0.12"))
        self.service.submit_rule("plan", "gasoline-guide", 2, 1)
        self.service.review_rule("review", "gasoline-guide", 2, 2, True)
        cancelled = self.service.retire_rule("plan", "gasoline-guide", 2, 3, "2026-03-02")
        self.assertEqual(cancelled["state"], "retired")
        self.assertEqual(cancelled["retired_at"], "2026-03-02")
        # 取消后 v1 区间在 03-02 前仍有效；03-02 起没有生效版本（后继被取消）。
        self.assertEqual(self.service.effective_rule("2026-03-01")["version"], 1)
        from fuel_pricing.errors import NotFound
        with self.assertRaises(NotFound):
            self.service.effective_rule("2026-03-02")

    def test_today_rule_cannot_recompute_past(self) -> None:
        self.approve_rule(rule_document("2026-03-02"))
        self.quote_window(12, ("70", "70", "70", "70", "70"))
        with self.assertRaises(InvalidState):
            self.service.preview("plan", "92", "2026-01-19")
        with self.assertRaises(InvalidState):
            self.service.publish("plan", "dec-x", "92", "2026-01-19", "key-x",
                                 rule_id="gasoline-guide", version=1)

    # ----- 预览与决定 -----------------------------------------------------

    def _setup_first_window(self) -> None:
        self.approve_rule(rule_document())
        self.quote_window(12, ("70.00", "70.10", "69.90", "70.05", "69.95"))

    def test_preview_does_not_persist(self) -> None:
        self._setup_first_window()
        preview = self.service.preview("plan", "92", "2026-01-19")
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual(len(preview["snapshot"]["locked_quotes"]), 5)
        self.assertEqual(self.service.list_decisions("risk")["decisions"], [])
        self.assertEqual(self.service.carry_status("risk")["ledger"], [])

    def test_below_threshold_defers_and_carries_forward(self) -> None:
        self._setup_first_window()
        decision = self.service.publish("plan", "dec-1", "92", "2026-01-19", "key-1")
        self.assertTrue(decision["result"]["deferred"])
        self.assertEqual(decision["result"]["defer_reason"], "below_threshold")
        self.assertEqual(decision["result"]["guide_price_cny"], "8.65")
        self.assertEqual(decision["result"]["carry_out_cny"], "-0.01")
        ledger = self.service.carry_status("risk")["ledger"]
        self.assertEqual(ledger[0]["accumulated_change_cny"], "-0.01")

    def test_cumulative_crossing_threshold_applies_change(self) -> None:
        self._setup_first_window()
        first = self.service.publish("plan", "dec-1", "92", "2026-01-19", "key-1")
        self.assertEqual(first["result"]["carry_out_cny"], "-0.01")
        self.quote_window(26, ("68.60", "68.40", "68.55", "68.45", "68.50"))
        second = self.service.publish("plan", "dec-2", "92", "2026-02-02", "key-2")
        self.assertFalse(second["result"]["deferred"])
        self.assertEqual(second["result"]["carry_in_cny"], "-0.01")
        self.assertEqual(second["result"]["applied_change_cny"], "-0.10")
        self.assertEqual(second["result"]["guide_price_cny"], "8.55")
        self.assertEqual(second["result"]["carry_out_cny"], "0.00")

    def test_publish_is_immutable_and_replays_same_record(self) -> None:
        self._setup_first_window()
        first = self.service.publish("plan", "dec-1", "92", "2026-01-19", "key-1")
        replay = self.service.publish("plan", "dec-other-id", "92", "2026-01-19", "key-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["decision_id"], "dec-1")
        # 报价更正前，同周期不同输入的发布一律冲突。
        self.service.record_quote("plan", {
            "price_index": "BRENT", "trade_date": "2026-01-14",
            "close_usd": "71.00", "source_revision": "r-corrected",
        })
        with self.assertRaises(Conflict):
            self.service.publish("plan", "dec-3", "92", "2026-01-19", "key-3")
        self.assertEqual(first["decision_id"], "dec-1")

    def test_idempotency_key_with_different_payload_conflicts(self) -> None:
        self._setup_first_window()
        self.service.publish("plan", "dec-1", "92", "2026-01-19", "shared-key")
        with self.assertRaises(Conflict):
            self.service.publish("plan", "dec-2", "95", "2026-01-19", "shared-key")

    def test_non_cycle_date_cannot_publish_but_can_preview(self) -> None:
        self._setup_first_window()
        # 2026-01-20 的预览窗口为 01-13..01-19，需补齐 01-19 的报价。
        self.service.record_quote("plan", {
            "price_index": "BRENT", "trade_date": "2026-01-19",
            "close_usd": "69.90", "source_revision": "r-2026-01-19",
        })
        preview = self.service.preview("plan", "92", "2026-01-20")
        self.assertFalse(preview["result"]["is_cycle_date"])
        with self.assertRaises(InvalidState):
            self.service.publish("plan", "dec-x", "92", "2026-01-20", "key-x")

    def test_incomplete_quote_window_blocks_decision(self) -> None:
        self.approve_rule(rule_document())
        # 只登记窗口中 3 个交易日。
        for day, close in (("2026-01-12", "70"), ("2026-01-13", "70"), ("2026-01-14", "70")):
            self.service.record_quote("plan", {
                "price_index": "BRENT", "trade_date": day,
                "close_usd": close, "source_revision": f"r-{day}",
            })
        with self.assertRaises(InvalidState):
            self.service.preview("plan", "92", "2026-01-19")

    def test_floor_freeze_defers_and_carries_full_gap(self) -> None:
        self.approve_rule(rule_document(floor="8.60"))
        # 原油约 65 美元，理论价明显低于地板价 8.60。
        self.quote_window(12, ("65", "65", "65", "65", "65"))
        decision = self.service.publish("plan", "dec-floor", "92", "2026-01-19", "key-floor")
        self.assertTrue(decision["result"]["deferred"])
        self.assertEqual(decision["result"]["defer_reason"], "floor_frozen")
        self.assertEqual(decision["result"]["guide_price_cny"], "8.65")
        self.assertLess(Decimal(decision["result"]["carry_out_cny"]), Decimal("0"))

    def test_decision_locks_rule_snapshot_and_quote_revisions(self) -> None:
        self._setup_first_window()
        decision = self.service.publish("plan", "dec-1", "92", "2026-01-19", "key-1")
        snapshot = decision  # response 不含完整快照，取库存记录核对
        stored = self.connection.execute(
            "SELECT input_snapshot_json FROM pricing_decisions WHERE decision_id='dec-1'"
        ).fetchone()
        import json
        snapshot_data = json.loads(stored["input_snapshot_json"])
        self.assertEqual(snapshot_data["rule_id"], "gasoline-guide")
        self.assertEqual(len(snapshot_data["locked_quotes"]), 5)
        self.assertTrue(all("quote_id" in q and "source_revision" in q for q in snapshot_data["locked_quotes"]))

    # ----- 报价更正 -------------------------------------------------------

    def test_quote_correction_flags_only_and_never_rewrites_history(self) -> None:
        self._setup_first_window()
        first = self.service.publish("plan", "dec-1", "92", "2026-01-19", "key-1")
        self.quote_window(26, ("68.60", "68.40", "68.55", "68.45", "68.50"))
        second = self.service.publish("plan", "dec-2", "92", "2026-02-02", "key-2")
        original_price = first["result"]["guide_price_cny"]
        correction = self.service.record_quote("plan", {
            "price_index": "BRENT", "trade_date": "2026-01-14",
            "close_usd": "69.20", "source_revision": "r-14-corrected",
        })
        # 直接窗口决定与 carry 链下游决定都被标出。
        self.assertEqual(set(correction["affected_decisions"]), {"dec-1", "dec-2"})
        suggestions = self.service.list_recalc_suggestions("risk", "open")["suggestions"]
        self.assertEqual(len(suggestions), 1)
        recalc = self.service.preview_recalc("risk", suggestions[0]["suggestion_id"])
        self.assertTrue(recalc["history_unchanged"])
        self.assertTrue(recalc["would_change"])
        self.assertNotEqual(
            recalc["recomputed_result"]["theoretical_price_cny"],
            recalc["original_result"]["theoretical_price_cny"],
        )
        # 历史决定内容原样保留，只打上取代标记。
        untouched = self.service.get_decision("audit", "dec-1")
        self.assertEqual(untouched["result"]["guide_price_cny"], original_price)
        self.assertTrue(untouched["input_superseded"])
        self.assertTrue(second["decision_id"])
        impacts = self.service.list_impacts("risk")["impacts"]
        notes = {row["note"] for row in impacts}
        self.assertEqual(notes, {"window_input", "carry_chain"})
        resolved = self.service.resolve_recalc_suggestion(
            "risk", suggestions[0]["suggestion_id"], "accept"
        )
        self.assertEqual(resolved["status"], "accepted")
        # 已处理的建议不能重复处理。
        with self.assertRaises(InvalidState):
            self.service.resolve_recalc_suggestion(
                "risk", suggestions[0]["suggestion_id"], "reject"
            )

    def test_grade_95_has_own_price_chain_and_carry(self) -> None:
        document = rule_document()
        # 95 号加工价差更高，使其理论价约 9.19，变动不足门槛而暂缓。
        document["products"]["95"]["processing_spread"] = "3.49"
        self.approve_rule(document)
        self.quote_window(12, ("70.00", "70.10", "69.90", "70.05", "69.95"))
        decision_92 = self.service.publish("plan", "dec-92", "92", "2026-01-19", "key-92")
        decision_95 = self.service.publish("plan", "dec-95", "95", "2026-01-19", "key-95")
        self.assertEqual(decision_92["result"]["guide_price_cny"], "8.65")
        self.assertEqual(decision_95["result"]["guide_price_cny"], "9.20")
        self.assertTrue(decision_95["result"]["deferred"])
        ledger = {row["grade"]: row for row in self.service.carry_status("risk")["ledger"]}
        self.assertEqual(set(ledger), {"92", "95"})

    def test_tax_on_accumulated_tax_base_is_deterministic(self) -> None:
        document = rule_document()
        # 在增值税后追加基于已累计税费的城建税（7%）。
        for product in document["products"].values():
            product["tax_components"].append(
                {"name": "城建税", "kind": "rate", "value": "0.07", "base": "tax"}
            )
        self.approve_rule(document)
        self.quote_window(12, ("70.00", "70.10", "69.90", "70.05", "69.95"))
        decision = self.service.publish("plan", "dec-1", "92", "2026-01-19", "key-1")
        names = [tax["name"] for tax in decision["result"]["taxes"]]
        self.assertEqual(names, ["消费税", "增值税", "城建税"])
        # 重放必须逐位一致。
        replay = self.service.publish("plan", "dec-1", "92", "2026-01-19", "key-1")
        self.assertEqual(
            [tax["amount_cny"] for tax in decision["result"]["taxes"]],
            [tax["amount_cny"] for tax in replay["result"]["taxes"]],
        )

    def test_unrelated_quote_revision_does_not_flag_decisions(self) -> None:
        self._setup_first_window()
        self.service.publish("plan", "dec-1", "92", "2026-01-19", "key-1")
        # 2026-01-12 虽在窗口内，但先更正再发布的假设不成立；此处改为窗口外日期。
        result = self.service.record_quote("plan", {
            "price_index": "BRENT", "trade_date": "2026-01-08",
            "close_usd": "72.00", "source_revision": "r-08-late",
        })
        self.assertEqual(result["affected_decisions"], [])

    # ----- 审计 -----------------------------------------------------------

    def test_audit_chain_is_valid(self) -> None:
        self._setup_first_window()
        self.service.publish("plan", "dec-1", "92", "2026-01-19", "key-1")
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 0)
        with self.assertRaises(Forbidden):
            self.service.audit_chain("plan")


if __name__ == "__main__":
    unittest.main()
