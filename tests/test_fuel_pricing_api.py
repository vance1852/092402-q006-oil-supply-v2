"""汽油指导价 HTTP JSON 接口测试。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from fuel_pricing.api import JsonApplication
from fuel_pricing.clock import FrozenClock
from fuel_pricing.service import FuelPricingService


HEADERS = {"X-Actor-Id": "plan"}


def policy(effective_from: str = "2026-01-05", **overrides: object) -> dict[str, object]:
    def product(base: str) -> dict[str, object]:
        return {
            "base_guide_price": base,
            "processing_spread": "3.00",
            "min_change_threshold": "0.05",
            "floor_price": None,
            "ceiling_price": None,
            "tax_components": [
                {"name": "消费税", "kind": "fixed", "value": "1.52"},
                {"name": "增值税", "kind": "rate", "value": "0.13", "base": "cost_plus_fixed"},
            ],
        }

    document = {
        "rule_id": "gasoline-guide",
        "price_index": "BRENT",
        "effective_from": effective_from,
        "cycle_anchor_date": effective_from,
        "cycle_workdays": 10,
        "quote_window_workdays": 5,
        "fx_rate_cny_per_usd": "7.10",
        "products": {"92": product("8.65"), "95": product("9.20")},
        "note": "API 测试政策",
    }
    document.update(overrides)
    return document


class FuelPricingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = FuelPricingService(
            self.connection, FrozenClock(datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc))
        )
        self.app = JsonApplication(self.service)
        for user_id, role in (
            ("plan", "planner"),
            ("review", "reviewer"),
            ("risk", "risk"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict[str, object], actor: str = "plan"):
        import json
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode())

    def get(self, path: str, actor: str = "plan"):
        return self.app.handle("GET", path, {"X-Actor-Id": actor})

    def test_health_requires_no_actor(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_missing_actor_is_rejected(self) -> None:
        response = self.app.handle("GET", "/pricing/decisions")
        self.assertEqual(response.status, 422)

    def test_full_flow_over_http(self) -> None:
        created = self.post("/pricing/rules", policy())
        self.assertEqual(created.status, 201)
        submitted = self.post("/pricing/rules/gasoline-guide/submit",
                              {"version": 1, "expected_revision": 1})
        self.assertEqual(submitted.status, 200)
        reviewed = self.post("/pricing/rules/gasoline-guide/review",
                             {"version": 1, "expected_revision": 2, "approve": True}, actor="review")
        self.assertEqual(reviewed.status, 200)
        activated = self.post("/pricing/rules/activate", {"as_of": "2026-01-05"}, actor="review")
        self.assertEqual(activated.body["activated"], ["gasoline-guide:v1"])

        for day, close in zip(range(12, 17), ("70.00", "70.10", "69.90", "70.05", "69.95"), strict=True):
            response = self.post("/pricing/quotes", {
                "price_index": "BRENT", "trade_date": f"2026-01-{day}",
                "close_usd": close, "source_revision": f"r-{day}",
            })
            self.assertEqual(response.status, 201)

        preview = self.post("/pricing/decisions/preview",
                            {"grade": "92", "evaluation_date": "2026-01-19"})
        self.assertEqual(preview.status, 200)
        self.assertEqual(preview.body["mode"], "preview")

        published = self.post("/pricing/decisions", {
            "decision_id": "dec-http-1", "grade": "92",
            "evaluation_date": "2026-01-19", "idempotency_key": "http-key-1",
        })
        self.assertEqual(published.status, 201)
        self.assertEqual(published.body["result"]["defer_reason"], "below_threshold")

        replay = self.post("/pricing/decisions", {
            "decision_id": "dec-http-1", "grade": "92",
            "evaluation_date": "2026-01-19", "idempotency_key": "http-key-1",
        })
        self.assertEqual(replay.status, 200)
        self.assertEqual(replay.body["decision_id"], "dec-http-1")

        fetched = self.get("/pricing/decisions/dec-http-1", actor="risk")
        self.assertEqual(fetched.status, 200)
        chain = self.get("/pricing/audit/chain", actor="audit")
        self.assertTrue(chain.body["valid"])

    def test_overlap_conflict_returned_as_409(self) -> None:
        self.post("/pricing/rules", policy("2026-01-05"))
        self.post("/pricing/rules/gasoline-guide/submit", {"version": 1, "expected_revision": 1})
        self.post("/pricing/rules/gasoline-guide/review",
                  {"version": 1, "expected_revision": 2, "approve": True}, actor="review")
        self.post("/pricing/rules", policy("2026-02-02"))
        self.post("/pricing/rules/gasoline-guide/submit", {"version": 2, "expected_revision": 1})
        conflict = self.post("/pricing/rules/gasoline-guide/review",
                             {"version": 2, "expected_revision": 2, "approve": True}, actor="review")
        self.assertEqual(conflict.status, 409)
        self.assertEqual(conflict.body["error"]["code"], "conflict")

    def test_forbidden_role_returns_403(self) -> None:
        response = self.post("/pricing/rules", policy(), actor="audit")
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
