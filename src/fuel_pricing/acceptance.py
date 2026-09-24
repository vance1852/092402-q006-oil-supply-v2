"""贯通规则版本、报价锁定、暂缓累计、不可变决定与修订重算的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def run(workspace: Path) -> dict[str, object]:
    from .clock import FrozenClock
    from .service import FuelPricingService

    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc))
    service = FuelPricingService(connection, clock)
    for user_id, role in (
        ("plan", "planner"),
        ("review", "reviewer"),
        ("risk", "risk"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    def rule_document(effective_from: str, *, vat: str, threshold: str, ceiling: str | None = None) -> dict[str, object]:
        def product(base: str) -> dict[str, object]:
            return {
                "base_guide_price": base,
                "processing_spread": "3.00",
                "min_change_threshold": threshold,
                "floor_price": None,
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
            "cycle_workdays": 10,
            "quote_window_workdays": 5,
            "fx_rate_cny_per_usd": "7.10",
            "products": {"92": product("8.65"), "95": product("9.20")},
            "note": "验收用政策版本",
        }

    # 第一版政策：2026-01-05 起生效，增值税 13%，最低变动门槛 0.05 元/升。
    service.create_rule_draft("plan", rule_document("2026-01-05", vat="0.13", threshold="0.05"))
    service.submit_rule("plan", "gasoline-guide", 1, 1)
    service.review_rule("review", "gasoline-guide", 1, 2, True, "核对税费与周期无误")
    activated = service.activate_due_rules("review", as_of="2026-01-05")

    # 第一周期窗口 2026-01-12..16：原油 70 美元，92 号理论价约等于基准价，变动不足门槛。
    for day, close in zip(range(12, 17), ("70.00", "70.10", "69.90", "70.05", "69.95"), strict=True):
        service.record_quote("plan", {
            "price_index": "BRENT", "trade_date": f"2026-01-{day}",
            "close_usd": close, "source_revision": f"r1-{day}",
        })

    # 预览 2026-01-19（周期日）：不落账。
    preview = service.preview("plan", "92", "2026-01-19")
    decisions_before_publish = len(service.list_decisions("risk")["decisions"])
    first = service.publish(
        "plan", "dec-92-2026-01-19", "92", "2026-01-19", "publish-key-1"
    )
    # 重复发布（同幂等键）返回同一记录。
    replay = service.publish(
        "plan", "dec-92-2026-01-19", "92", "2026-01-19", "publish-key-1"
    )

    # 第二周期窗口 2026-01-26..30：原油跌至 68.5 美元，累计变动跨过门槛。
    for day, close in zip(range(26, 31), ("68.60", "68.40", "68.55", "68.45", "68.50"), strict=True):
        service.record_quote("plan", {
            "price_index": "BRENT", "trade_date": f"2026-01-{day}",
            "close_usd": close, "source_revision": f"r1-{day}",
        })
    second = service.publish(
        "plan", "dec-92-2026-02-02", "92", "2026-02-02", "publish-key-2"
    )

    # 第二版政策：2026-03-02 起生效，增值税降至 12%、门槛 0.03。
    # 先把旧版本退役日设定为新版本生效日（半开区间不重叠），复核才能通过。
    service.retire_rule("plan", "gasoline-guide", 1, 3, "2026-03-02", "新政策期开始")
    service.create_rule_draft("plan", rule_document("2026-03-02", vat="0.12", threshold="0.03"))
    service.submit_rule("plan", "gasoline-guide", 2, 1)
    service.review_rule("review", "gasoline-guide", 2, 2, True)
    rollover = service.activate_due_rules("review", as_of="2026-03-02")
    old_period_rule = service.effective_rule("2026-02-10")
    new_period_rule = service.effective_rule("2026-03-02")

    # 报价更正：修订 2026-01-14 的报价。历史决定不被改写，只标记并产生重算建议。
    original_first_price = first["result"]["guide_price_cny"]
    correction = service.record_quote("plan", {
        "price_index": "BRENT", "trade_date": "2026-01-14",
        "close_usd": "69.20", "source_revision": "r1-14-corrected",
    })
    impacts = service.list_impacts("risk")
    suggestions = service.list_recalc_suggestions("risk", "open")
    recalc = service.preview_recalc("risk", suggestions["suggestions"][0]["suggestion_id"])
    untouched = service.get_decision("audit", "dec-92-2026-01-19")
    resolve = service.resolve_recalc_suggestion(
        "risk", suggestions["suggestions"][0]["suggestion_id"], "accept", "确认修订有效"
    )

    result = {
        "status": "ok",
        "activated_first": activated["activated"],
        "preview_did_not_persist": decisions_before_publish == 0 and preview["mode"] == "preview",
        "first_cycle": {
            "deferred": first["result"]["deferred"],
            "defer_reason": first["result"]["defer_reason"],
            "carry_out_cny": first["result"]["carry_out_cny"],
            "guide_price_cny": first["result"]["guide_price_cny"],
        },
        "republish_replayed": replay["replayed"] is True
        and replay["decision_id"] == first["decision_id"],
        "second_cycle": {
            "deferred": second["result"]["deferred"],
            "carry_in_cny": second["result"]["carry_in_cny"],
            "applied_change_cny": second["result"]["applied_change_cny"],
            "guide_price_cny": second["result"]["guide_price_cny"],
        },
        "policy_rollover": {
            "retired": rollover["retired"],
            "activated": rollover["activated"],
            "old_period_version": old_period_rule["version"],
            "new_period_version": new_period_rule["version"],
            "new_period_vat": new_period_rule["document"]["products"][0]["tax_components"][1]["value"],
        },
        "quote_correction": {
            "affected_decisions": correction["affected_decisions"],
            "impact_count": len(impacts["impacts"]),
            "recalc_would_change": recalc["would_change"],
            "history_unchanged": untouched["result"]["guide_price_cny"] == original_first_price
            and untouched["input_superseded"] is True,
            "suggestion_resolved": resolve["status"],
        },
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行汽油指导价服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
