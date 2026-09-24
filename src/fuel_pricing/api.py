"""无第三方依赖的汽油指导价 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import PricingError, ValidationFailed
from .service import FuelPricingService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: FuelPricingService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            service = self.service

            if method == "POST" and path == "/pricing/users":
                return Response(201, service.create_user(payload["user_id"], payload["display_name"], payload["role"]))

            if method == "POST" and path == "/pricing/quotes":
                return Response(201, service.record_quote(actor, payload))

            if method == "POST" and path == "/pricing/rules":
                return Response(201, service.create_rule_draft(actor, payload))
            if method == "PUT" and len(parts) == 4 and parts[:2] == ["pricing", "rules"]:
                return Response(200, service.update_rule_draft(actor, parts[2], int(parts[3]), payload))
            if method == "GET" and path == "/pricing/rules":
                return Response(200, service.list_rules())
            if method == "GET" and len(parts) == 4 and parts[:2] == ["pricing", "rules"]:
                return Response(200, service.get_rule_version(parts[2], int(parts[3])))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["pricing", "rules"] and parts[2] == "effective":
                return Response(200, service.effective_rule(query["date"][0]))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["pricing", "rules"] and parts[3] == "submit":
                return Response(200, service.submit_rule(actor, parts[2], int(payload["version"]), int(payload["expected_revision"]), payload.get("comment", "")))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["pricing", "rules"] and parts[3] == "review":
                return Response(200, service.review_rule(actor, parts[2], int(payload["version"]), int(payload["expected_revision"]), bool(payload["approve"]), payload.get("comment", "")))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["pricing", "rules"] and parts[3] == "retire":
                return Response(200, service.retire_rule(actor, parts[2], int(payload["version"]), int(payload["expected_revision"]), payload["retire_date"], payload.get("comment", "")))
            if method == "POST" and path == "/pricing/rules/activate":
                return Response(200, service.activate_due_rules(actor, payload.get("as_of")))

            if method == "POST" and path == "/pricing/decisions/preview":
                return Response(200, service.preview(actor, payload["grade"], payload["evaluation_date"], payload.get("rule_id"), payload.get("rule_version")))
            if method == "POST" and path == "/pricing/decisions":
                body = service.publish(actor, payload["decision_id"], payload["grade"], payload["evaluation_date"], payload["idempotency_key"], payload.get("rule_id"), payload.get("rule_version"))
                return Response(200 if body.get("replayed") else 201, body)
            if method == "GET" and len(parts) == 3 and parts[:2] == ["pricing", "decisions"]:
                return Response(200, service.get_decision(actor, parts[2]))
            if method == "GET" and path == "/pricing/decisions":
                return Response(200, service.list_decisions(actor, query.get("grade", [None])[0]))
            if method == "GET" and path == "/pricing/carry":
                return Response(200, service.carry_status(actor))

            if method == "GET" and path == "/pricing/impacts":
                return Response(200, service.list_impacts(actor))
            if method == "GET" and path == "/pricing/recalc-suggestions":
                return Response(200, service.list_recalc_suggestions(actor, query.get("status", [None])[0]))
            if method == "POST" and len(parts) == 3 and parts[0] == "pricing" and parts[1] == "recalc-suggestions":
                return Response(200, service.preview_recalc(actor, parts[2]))
            if method == "POST" and len(parts) == 4 and parts[0] == "pricing" and parts[1] == "recalc-suggestions" and parts[3] == "resolve":
                return Response(200, service.resolve_recalc_suggestion(actor, parts[2], payload["action"], payload.get("comment", "")))

            if method == "GET" and path == "/pricing/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except PricingError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FuelPricing/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动汽油指导价服务")
    parser.add_argument("--database", type=Path, default=Path("fuel_pricing.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(FuelPricingService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
