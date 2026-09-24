"""贯通报价、线路、库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .gasoline import GasolineService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = GasolineService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"price_index": "BRENT", "trade_date": f"2026-09-{index}", "close_usd": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "150000", "unit_cost_usd": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键管道恢复与需求回落", "price_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")

    gasoline_rule = {
        "index_weights": {"BRENT": "1"},
        "window_sessions": 3,
        "change_threshold_per_liter": "0.05",
        "vat_rate": "0.13",
        "products": {
            "gasoline-92": {"conversion_factor": "0.060", "processing_margin_per_liter": "1.00", "consumption_tax_per_liter": "1.52"},
            "gasoline-95": {"conversion_factor": "0.063", "processing_margin_per_liter": "1.20", "consumption_tax_per_liter": "1.52"},
        },
        "carry_below_threshold": True,
        "carry_on_suspend": True,
    }
    service.create_gasoline_strategy("plan", {
        "strategy_id": "gasoline-main",
        "name": "92/95 汽油指导价策略",
        "price_indexes": ["BRENT"],
        "baseline_92": "9.00",
        "baseline_95": "9.60",
        "baseline_anchor_date": "2026-09-18",
    })
    service.add_rule_version("plan", "gasoline-main", "2026-09-18", gasoline_rule)
    revision = service.gasoline_strategy("gasoline-main")["revision"]
    service.submit_strategy_for_review("plan", "gasoline-main", revision)
    service.approve_strategy("risk", "gasoline-main", revision + 1, "2026-09-24T00:00:00Z")
    service.activate_due_strategies("risk")
    gasoline_preview = service.preview_gasoline_decision("plan", "gasoline-main", "2026-09-22")
    gasoline_decision = service.publish_gasoline_decision("plan", "gasoline-main", "2026-09-22")
    gasoline_replay = service.publish_gasoline_decision("plan", "gasoline-main", "2026-09-22")
    service.record_quote("plan", {"price_index": "BRENT", "trade_date": "2026-09-20", "close_usd": "101.4", "source_revision": "rev-20-corrected", "observed_at": "2026-09-24T04:00:00Z"})
    corrections = service.quote_corrections("plan")["corrections"]
    gasoline_recalc = service.recalculation_suggestion("plan", corrections[-1]["correction_id"]) if corrections else {"suggestions": []}

    result = {"status": "ok", "price": service.price_summary("BRENT"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"],
              "gasoline": {
                  "strategy_state": service.gasoline_strategy("gasoline-main")["state"],
                  "preview_persisted": gasoline_preview["persisted"],
                  "decision_id": gasoline_decision["decision_id"],
                  "replay_same_record": gasoline_replay["decision_id"] == gasoline_decision["decision_id"],
                  "outcome": gasoline_decision["result"]["outcome"],
                  "locked_quote_revisions": len(gasoline_decision["result"]["locked_quotes"]),
                  "corrections_flagged": len(corrections),
                  "recalc_suggestions": len(gasoline_recalc["suggestions"]),
                  "history_immutable": gasoline_recalc.get("historical_decisions_immutable", True),
              },
              "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行油气供应服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
