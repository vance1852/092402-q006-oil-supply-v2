"""定时生效任务入口，供外部调度器（cron/定时器）每日调用。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import SystemClock, utc_text
from .service import FuelPricingService
from .storage import connect, transaction

SCHEDULER_ACTOR = "system-scheduler"


def _ensure_actor(service: FuelPricingService) -> None:
    """确保保留的调度系统用户存在（reviewer 角色，拥有 rule.activate 权限）。"""

    existing = service.connection.execute(
        "SELECT 1 FROM pricing_users WHERE user_id=?", (SCHEDULER_ACTOR,)
    ).fetchone()
    if existing is not None:
        return
    with transaction(service.connection, immediate=True):
        service.connection.execute(
            "INSERT INTO pricing_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
            (SCHEDULER_ACTOR, "定时生效任务", "reviewer", utc_text(service.clock.now())),
        )


def run_database(database: Path, as_of: str | None = None) -> dict[str, object]:
    connection = connect(database)
    try:
        service = FuelPricingService(connection, SystemClock())
        _ensure_actor(service)
        result = service.activate_due_rules(actor_id=SCHEDULER_ACTOR, as_of=as_of)
    finally:
        connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行汽油指导价规则定时生效与定时退役")
    parser.add_argument("--database", type=Path, default=Path("fuel_pricing.sqlite3"))
    parser.add_argument("--as-of", default=None, help="覆盖生效判定日（YYYY-MM-DD），默认按系统时钟")
    args = parser.parse_args(argv)
    try:
        result = run_database(args.database, args.as_of)
    except sqlite3.Error as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    result["ran_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
