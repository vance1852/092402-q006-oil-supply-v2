"""汽油指导价策略的草稿、复核、定时生效、退役与调价决定。

设计要点：

- 政策规则（税费、加工价差、调整周期、最低变动门槛、暂缓口径）按
  ``gasoline_rule_versions`` 整体快照保存，计算只引用锚定日生效的版本；
- 每个调价决定落账时锁定当时采用的报价修订（quote_id 明细）和规则哈希，
  输入哈希相同的重复发布直接返回同一条不可变决定；
- 预览不写库、不写审计；发布在 IMMEDIATE 事务内生成不可变记录；
- 报价更正不会改写历史，只生成 ``gasoline_quote_corrections`` 标记受影响
  决定，并提供基于最新报价的级联重算建议（仍不落账）。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import parse_utc
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import CRUDE_GRADES
from .planning import canonical_json, decimal_text, digest
from .pricing import (
    GASOLINE_PRODUCTS,
    PricingRule,
    QuotePoint,
    evaluate_window,
    quantize_price,
)
from .service import ROLE_PERMISSIONS, SupplyService
from .storage import transaction


class GasolineService(SupplyService):
    def _require_any(self, user_id: str, *permissions: str) -> sqlite3.Row:
        user = self._user(user_id)
        granted = ROLE_PERMISSIONS[user["role"]]
        if not any(permission in granted for permission in permissions):
            raise Forbidden(f"角色 {user['role']} 无权执行 {permissions[0]}")
        return user

    # ------------------------------------------------------------------ 策略

    def create_gasoline_strategy(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "gasoline.strategy.write")
        strategy_id = raw.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise ValidationFailed("strategy_id 不能为空")
        strategy_id = strategy_id.strip()
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationFailed("name 不能为空")
        indexes_raw = raw.get("price_indexes")
        if not isinstance(indexes_raw, list) or not indexes_raw:
            raise ValidationFailed("price_indexes 必须是非空数组")
        indexes: list[str] = []
        for item in indexes_raw:
            index = str(item).upper()
            if index not in CRUDE_GRADES - {"CUSTOM"}:
                raise ValidationFailed(f"不支持的原油基准 {index}")
            if index in indexes:
                raise ValidationFailed(f"基准 {index} 重复")
            indexes.append(index)
        baseline_anchor = raw.get("baseline_anchor_date")
        try:
            baseline_anchor = _dt.date.fromisoformat(str(baseline_anchor)).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("baseline_anchor_date 必须是 YYYY-MM-DD") from exc
        baselines: dict[str, Decimal] = {}
        for product in GASOLINE_PRODUCTS:
            value = raw.get(f"baseline_{product[-2:]}")
            try:
                baseline = Decimal(str(value))
            except Exception as exc:
                raise ValidationFailed(f"baseline_{product[-2:]} 必须是数值") from exc
            if not baseline.is_finite() or baseline <= 0:
                raise ValidationFailed(f"baseline_{product[-2:]} 必须是正数")
            baselines[product] = quantize_price(baseline)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO gasoline_price_strategies(strategy_id,name,price_indexes_json,"
                    "baseline_92,baseline_95,baseline_anchor_date,state,created_by,created_at,"
                    "updated_by,updated_at) VALUES(?,?,?,?,?,?, 'draft',?,?,?,?)",
                    (
                        strategy_id,
                        name.strip(),
                        canonical_json(indexes),
                        decimal_text(baselines["gasoline-92"]),
                        decimal_text(baselines["gasoline-95"]),
                        baseline_anchor,
                        actor_id,
                        now,
                        actor_id,
                        now,
                    ),
                )
                self._audit("gasoline_strategy", strategy_id, "gasoline.strategy.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("策略编号已经存在") from exc
        return self.gasoline_strategy(strategy_id)

    def gasoline_strategy(self, strategy_id: str, actor_id: str | None = None) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM gasoline_price_strategies WHERE strategy_id=?", (strategy_id,)
        ).fetchone()
        if row is None:
            raise NotFound("汽油调价策略不存在")
        if actor_id is not None:
            self._require_any(actor_id, "gasoline.preview", "gasoline.review", "audit.read")
        result = dict(row)
        result["price_indexes"] = json.loads(result.pop("price_indexes_json"))
        result["rule_versions"] = [
            dict(version) for version in self.connection.execute(
                "SELECT rule_version_id,effective_from,effective_to,rule_sha256,created_at "
                "FROM gasoline_rule_versions WHERE strategy_id=? ORDER BY effective_from",
                (strategy_id,),
            ).fetchall()
        ]
        return result

    def add_rule_version(
        self,
        actor_id: str,
        strategy_id: str,
        effective_from: str,
        rule: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require(actor_id, "gasoline.strategy.write")
        strategy = self._strategy_row(strategy_id)
        if strategy["state"] != "draft":
            raise InvalidState("只有草稿策略可以增补政策规则版本")
        try:
            start = _dt.date.fromisoformat(effective_from).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("effective_from 必须是 YYYY-MM-DD") from exc
        parsed = PricingRule.from_dict(rule)
        weighted_indexes = set(parsed.index_weights)
        if weighted_indexes != set(json.loads(strategy["price_indexes_json"])):
            raise ValidationFailed("规则中的原油基准必须与策略 price_indexes 完全一致")
        # 区间为左闭右开 [effective_from, effective_to)。更早的开口区间
        # 允许存在——本次插入后会被收口到 start；同起点或起点严格落入某
        # 个已收口区间内部才构成重叠。
        overlapping = self.connection.execute(
            "SELECT rule_version_id FROM gasoline_rule_versions WHERE strategy_id=? "
            "AND (effective_from=? OR (effective_to IS NOT NULL AND effective_from<? AND effective_to>?))",
            (strategy_id, start, start, start),
        ).fetchall()
        if overlapping:
            raise Conflict("新生效日落入已有规则版本区间，生效区间不得重叠")
        now = self._now()
        with transaction(self.connection, immediate=True):
            later = self.connection.execute(
                "SELECT MIN(effective_from) AS next_from FROM gasoline_rule_versions "
                "WHERE strategy_id=? AND effective_from>?",
                (strategy_id, start),
            ).fetchone()
            new_effective_to = None if later is None or later["next_from"] is None else later["next_from"]
            cursor = self.connection.execute(
                "INSERT INTO gasoline_rule_versions(strategy_id,effective_from,effective_to,rule_json,rule_sha256,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (strategy_id, start, new_effective_to, canonical_json(parsed.payload), parsed.sha256, actor_id, now),
            )
            version_id = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE gasoline_rule_versions SET effective_to=? WHERE strategy_id=? AND effective_to IS NULL "
                "AND effective_from<?",
                (start, strategy_id, start),
            )
            self.connection.execute(
                "UPDATE gasoline_price_strategies SET current_rule_version_id=?,revision=revision+1,"
                "updated_by=?,updated_at=? WHERE strategy_id=?",
                (version_id, actor_id, now, strategy_id),
            )
            self._audit(
                "gasoline_strategy",
                strategy_id,
                "gasoline.rule_version.added",
                actor_id,
                {"rule_version_id": version_id, "effective_from": start, "rule_sha256": parsed.sha256},
            )
        return {
            "rule_version_id": version_id,
            "strategy_id": strategy_id,
            "effective_from": start,
            "effective_to": new_effective_to,
            "rule_sha256": parsed.sha256,
        }

    def submit_strategy_for_review(self, actor_id: str, strategy_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "gasoline.strategy.write")
        strategy = self._strategy_row(strategy_id)
        self._expect_revision(strategy, expected_revision)
        if strategy["state"] != "draft":
            raise InvalidState("只有草稿策略可以送审")
        covering = self.connection.execute(
            "SELECT 1 FROM gasoline_rule_versions WHERE strategy_id=? AND effective_from<=? "
            "AND (effective_to IS NULL OR effective_to>?)",
            (strategy_id, strategy["baseline_anchor_date"], strategy["baseline_anchor_date"]),
        ).fetchone()
        if covering is None:
            raise InvalidState("缺少覆盖基准价日期的规则版本，无法形成完整规则快照")
        return self._transition(
            actor_id, strategy_id, expected_revision, "draft", "in_review",
            "gasoline.strategy.submitted", {},
        )

    def approve_strategy(
        self,
        actor_id: str,
        strategy_id: str,
        expected_revision: int,
        scheduled_publish_at: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "gasoline.review")
        strategy = self._strategy_row(strategy_id)
        self._expect_revision(strategy, expected_revision)
        if strategy["state"] != "in_review":
            raise InvalidState("只有复核中的策略可以批准")
        when = parse_utc(scheduled_publish_at, "scheduled_publish_at")
        scheduled_text = when.isoformat().replace("+00:00", "Z")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE gasoline_price_strategies SET state='scheduled',scheduled_publish_at=?,"
                "revision=revision+1,updated_by=?,updated_at=? "
                "WHERE strategy_id=? AND state='in_review' AND revision=?",
                (scheduled_text, actor_id, now, strategy_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("策略状态已变化，请刷新后重试")
            self._audit(
                "gasoline_strategy", strategy_id, "gasoline.strategy.approved", actor_id,
                {"scheduled_publish_at": scheduled_text},
            )
        return self.gasoline_strategy(strategy_id)

    def activate_due_strategies(self, actor_id: str) -> dict[str, Any]:
        """把到达定时生效时刻、且当前没有生效中策略的计划策略激活。"""
        self._require(actor_id, "gasoline.review")
        now = self._now()
        active = self.connection.execute(
            "SELECT strategy_id FROM gasoline_price_strategies WHERE state='active'"
        ).fetchone()
        activated: list[str] = []
        if active is None:
            row = self.connection.execute(
                "SELECT strategy_id,revision FROM gasoline_price_strategies "
                "WHERE state='scheduled' AND scheduled_publish_at<=? "
                "ORDER BY scheduled_publish_at,strategy_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is not None:
                with transaction(self.connection, immediate=True):
                    cursor = self.connection.execute(
                        "UPDATE gasoline_price_strategies SET state='active',effective_from=date(?),"
                        "published_at=?,revision=revision+1,updated_by=?,updated_at=? "
                        "WHERE strategy_id=? AND state='scheduled'",
                        (now, now, actor_id, now, row["strategy_id"]),
                    )
                    if cursor.rowcount == 1:
                        activated.append(row["strategy_id"])
                        self._audit(
                            "gasoline_strategy", row["strategy_id"], "gasoline.strategy.activated",
                            actor_id, {"scheduled_at": now},
                        )
        return {"activated": activated, "deferred": active is not None and not activated, "as_of": now}

    def retire_strategy(self, actor_id: str, strategy_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "gasoline.retire")
        strategy = self._strategy_row(strategy_id)
        self._expect_revision(strategy, expected_revision)
        if strategy["state"] not in ("active", "scheduled"):
            raise InvalidState("只有生效中或待生效策略可以退役")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE gasoline_price_strategies SET state='retired',retired_at=?,"
                "effective_to=CASE WHEN effective_from IS NOT NULL THEN date(?) ELSE effective_to END,"
                "revision=revision+1,updated_by=?,updated_at=? "
                "WHERE strategy_id=? AND revision=? AND state IN ('active','scheduled')",
                (now, now, actor_id, now, strategy_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("策略状态已变化，请刷新后重试")
            self._audit("gasoline_strategy", strategy_id, "gasoline.strategy.retired", actor_id, {})
        return self.gasoline_strategy(strategy_id)

    # ------------------------------------------------------------------ 决定

    def preview_gasoline_decision(self, actor_id: str, strategy_id: str, anchor_date: str) -> dict[str, Any]:
        self._require_any(actor_id, "gasoline.preview", "gasoline.review", "audit.read")
        computed = self._compute_decision(strategy_id, anchor_date)
        return {"state": "preview", "persisted": False, **computed}

    def publish_gasoline_decision(self, actor_id: str, strategy_id: str, anchor_date: str) -> dict[str, Any]:
        self._require(actor_id, "gasoline.publish")
        strategy = self._strategy_row(strategy_id)
        if strategy["state"] != "active":
            raise InvalidState("只有生效中的策略可以发布正式决定")
        existing = self.connection.execute(
            "SELECT * FROM gasoline_decisions WHERE strategy_id=? AND anchor_date=? AND state='published'",
            (strategy_id, anchor_date),
        ).fetchone()
        if existing is not None:
            return {"state": "published", "replayed": True, **self._decision_payload(existing)}
        later = self.connection.execute(
            "SELECT MIN(anchor_date) AS later_anchor FROM gasoline_decisions "
            "WHERE strategy_id=? AND state='published' AND anchor_date>?",
            (strategy_id, anchor_date),
        ).fetchone()
        if later is not None and later["later_anchor"] is not None:
            raise InvalidState("正式决定必须按锚定日顺序发布，不能向更早周期补录")
        computed = self._compute_decision(strategy_id, anchor_date)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO gasoline_decisions(strategy_id,anchor_date,rule_version_id,rule_sha256,"
                    "input_sha256,result_json,carry_in_json,carry_out_json,state,mode,created_by,created_at,"
                    "published_by,published_at) VALUES(?,?,?,?,?,?,?,?,'published','manual',?,?,?,?)",
                    (
                        strategy_id,
                        anchor_date,
                        computed["rule_version_id"],
                        computed["rule_sha256"],
                        computed["input_sha256"],
                        canonical_json(computed["result"]),
                        canonical_json(computed["carry_in"]),
                        canonical_json(computed["carry_out"]),
                        actor_id,
                        now,
                        actor_id,
                        now,
                    ),
                )
                decision_id = int(cursor.lastrowid)
                self.connection.executemany(
                    "INSERT INTO gasoline_decision_quotes(decision_id,price_index,trade_date,quote_id,close_usd)"
                    " VALUES(?,?,?,?,?)",
                    [
                        (decision_id, point["price_index"], point["trade_date"], point["quote_id"], point["close_usd"])
                        for point in computed["result"]["locked_quotes"]
                    ],
                )
                self._audit(
                    "gasoline_decision", str(decision_id), "gasoline.decision.published", actor_id,
                    {"strategy_id": strategy_id, "anchor_date": anchor_date,
                     "outcome": computed["result"]["outcome"], "input_sha256": computed["input_sha256"]},
                )
            stored_row = self.connection.execute(
                "SELECT * FROM gasoline_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
        except sqlite3.IntegrityError as exc:
            stored = self.connection.execute(
                "SELECT * FROM gasoline_decisions WHERE strategy_id=? AND anchor_date=? AND state='published'",
                (strategy_id, anchor_date),
            ).fetchone()
            if stored is not None:
                return {"state": "published", "replayed": True, **self._decision_payload(stored)}
            raise Conflict("决定输入与既有记录冲突") from exc
        return {"state": "published", "replayed": False, **self._decision_payload(stored_row)}

    def gasoline_decision(self, decision_id: int, actor_id: str) -> dict[str, Any]:
        self._require_any(actor_id, "gasoline.preview", "gasoline.review", "audit.read")
        row = self.connection.execute(
            "SELECT * FROM gasoline_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise NotFound("调价决定不存在")
        return self._decision_payload(row)

    def gasoline_decisions(self, strategy_id: str, actor_id: str) -> dict[str, Any]:
        self._require_any(actor_id, "gasoline.preview", "gasoline.review", "audit.read")
        self._strategy_row(strategy_id)
        rows = self.connection.execute(
            "SELECT decision_id,anchor_date,state,rule_sha256,input_sha256,published_at "
            "FROM gasoline_decisions WHERE strategy_id=? ORDER BY anchor_date,decision_id",
            (strategy_id,),
        ).fetchall()
        return {"strategy_id": strategy_id, "decisions": [dict(row) for row in rows]}

    # ------------------------------------------------------------ 报价更正

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        response = super().record_quote(actor_id, raw)
        self._flag_quote_correction(actor_id, response["quote_id"])
        return response

    def quote_corrections(self, actor_id: str) -> dict[str, Any]:
        self._require_any(actor_id, "gasoline.preview", "gasoline.review", "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM gasoline_quote_corrections ORDER BY correction_id"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["affected_decision_ids"] = json.loads(item.pop("affected_decision_ids_json"))
            item["recalc_suggestion_stub"] = json.loads(item.pop("recalc_suggestion_json"))
            result.append(item)
        return {"corrections": result}

    def recalculation_suggestion(self, actor_id: str, correction_id: int) -> dict[str, Any]:
        """按最新报价修订级联重算；只返回建议，绝不改写决定。

        从每个受影响策略的最早受影响锚定日起，后续已发布决定也按最新报价
        修订重新推导（间接受影响），以呈现跨周期累计的连锁变化。
        """
        self._require_any(actor_id, "gasoline.preview", "gasoline.review", "audit.read")
        correction = self.connection.execute(
            "SELECT * FROM gasoline_quote_corrections WHERE correction_id=?", (correction_id,)
        ).fetchone()
        if correction is None:
            raise NotFound("报价更正记录不存在")
        direct_ids = set(json.loads(correction["affected_decision_ids_json"]))
        affected_rows = [
            row for row in (
                self.connection.execute(
                    "SELECT * FROM gasoline_decisions WHERE decision_id=? AND state='published'",
                    (decision_id,),
                ).fetchone()
                for decision_id in sorted(direct_ids)
            )
            if row is not None
        ]
        strategies = sorted({row["strategy_id"] for row in affected_rows})
        suggestions: list[dict[str, Any]] = []
        for strategy_id in strategies:
            earliest = min(row["anchor_date"] for row in affected_rows if row["strategy_id"] == strategy_id)
            chain = self.connection.execute(
                "SELECT * FROM gasoline_decisions WHERE strategy_id=? AND state='published' "
                "AND anchor_date>=? ORDER BY anchor_date,decision_id",
                (strategy_id, earliest),
            ).fetchall()
            prior = self.connection.execute(
                "SELECT * FROM gasoline_decisions WHERE strategy_id=? AND state='published' "
                "AND anchor_date<? ORDER BY anchor_date DESC,decision_id DESC LIMIT 1",
                (strategy_id, earliest),
            ).fetchone()
            if prior is None:
                current, carry, reference = self._baseline_state(self._strategy_row(strategy_id))
            else:
                current, carry, reference = self._state_after(prior)
            for row in chain:
                computed = self._compute_decision(
                    strategy_id, row["anchor_date"],
                    current_prices=current, incoming=carry, reference_prices=reference,
                    rule_version_id=row["rule_version_id"],
                )
                current = {
                    product: Decimal(computed["result"]["products"][product]["resulting_per_liter"])
                    for product in GASOLINE_PRODUCTS
                }
                carry = {
                    product: Decimal(computed["result"]["products"][product]["outgoing_change"])
                    for product in GASOLINE_PRODUCTS
                }
                reference = {
                    product: Decimal(computed["result"]["products"][product]["expected_per_liter"])
                    for product in GASOLINE_PRODUCTS
                }
                published_result = json.loads(row["result_json"])
                suggestions.append({
                    "decision_id": row["decision_id"],
                    "strategy_id": strategy_id,
                    "anchor_date": row["anchor_date"],
                    "directly_affected": row["decision_id"] in direct_ids,
                    "published_outcome": published_result["outcome"],
                    "published_products": published_result["products"],
                    "suggested_outcome": computed["result"]["outcome"],
                    "suggested_products": computed["result"]["products"],
                    "changed": canonical_json(published_result) != canonical_json(computed["result"]),
                })
        return {
            "correction_id": correction_id,
            "historical_decisions_immutable": True,
            "suggestions": suggestions,
        }

    def reject_strategy(self, actor_id: str, strategy_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
        """复核驳回，策略回到草稿，可继续增补规则版本后重新送审。"""
        self._require(actor_id, "gasoline.review")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("驳回原因不能为空")
        strategy = self._strategy_row(strategy_id)
        self._expect_revision(strategy, expected_revision)
        if strategy["state"] != "in_review":
            raise InvalidState("只有复核中的策略可以驳回")
        return self._transition(
            actor_id, strategy_id, expected_revision, "in_review", "draft",
            "gasoline.strategy.rejected", {"reason": reason.strip()[:500]},
        )

    def resolve_quote_correction(self, actor_id: str, correction_id: int, state: str) -> dict[str, Any]:
        self._require(actor_id, "gasoline.review")
        if state not in ("reviewed", "resolved"):
            raise ValidationFailed("state 必须是 reviewed 或 resolved")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE gasoline_quote_corrections SET affected_state=?,resolved_at=? "
                "WHERE correction_id=? AND affected_state<>?",
                (state, self._now(), correction_id, "resolved"),
            )
            if cursor.rowcount != 1:
                raise InvalidState("更正记录不存在或已终结")
            self._audit(
                "gasoline_quote_correction", str(correction_id),
                "gasoline.correction.resolved", actor_id, {"state": state},
            )
        return {"correction_id": correction_id, "affected_state": state}

    # ------------------------------------------------------------ 内部辅助

    def _strategy_row(self, strategy_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM gasoline_price_strategies WHERE strategy_id=?", (strategy_id,)
        ).fetchone()
        if row is None:
            raise NotFound("汽油调价策略不存在")
        return row

    @staticmethod
    def _expect_revision(strategy: sqlite3.Row, expected_revision: int) -> None:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision <= 0:
            raise ValidationFailed("expected_revision 必须是正整数")
        if strategy["revision"] != expected_revision:
            raise Conflict(f"策略版本不是 {expected_revision}，请刷新后重试")

    def _transition(
        self, actor_id: str, strategy_id: str, expected_revision: int,
        from_state: str, to_state: str, event: str, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                f"UPDATE gasoline_price_strategies SET state=?,revision=revision+1,"
                f"updated_by=?,updated_at=? WHERE strategy_id=? AND state=? AND revision=?",
                (to_state, actor_id, now, strategy_id, from_state, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState(f"策略不是 {from_state} 状态或版本已变化")
            self._audit("gasoline_strategy", strategy_id, event, actor_id, payload)
        return self.gasoline_strategy(strategy_id)

    def _baseline_state(
        self, strategy: sqlite3.Row
    ) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, Decimal]]:
        current = {
            "gasoline-92": Decimal(strategy["baseline_92"]),
            "gasoline-95": Decimal(strategy["baseline_95"]),
        }
        zero = {product: Decimal("0") for product in GASOLINE_PRODUCTS}
        # 首轮的上一窗口目标价就是基准价本身。
        return current, zero, dict(current)

    @staticmethod
    def _state_after(
        row: sqlite3.Row,
    ) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, Decimal]]:
        result = json.loads(row["result_json"])
        carry = json.loads(row["carry_out_json"])
        return (
            {product: Decimal(result["products"][product]["resulting_per_liter"]) for product in GASOLINE_PRODUCTS},
            {product: Decimal(carry[product]) for product in GASOLINE_PRODUCTS},
            {product: Decimal(result["products"][product]["expected_per_liter"]) for product in GASOLINE_PRODUCTS},
        )

    def _rule_version_at(self, strategy_id: str, anchor_date: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM gasoline_rule_versions WHERE strategy_id=? AND effective_from<=? "
            "AND (effective_to IS NULL OR effective_to>?) ORDER BY effective_from DESC LIMIT 1",
            (strategy_id, anchor_date, anchor_date),
        ).fetchone()
        if row is None:
            raise InvalidState(f"{anchor_date} 没有生效中的政策规则版本")
        return row

    def _lock_quotes(
        self, indexes: Sequence[str], sessions: int, anchor_date: str
    ) -> dict[str, list[QuotePoint]]:
        locked: dict[str, list[QuotePoint]] = {}
        for index in indexes:
            rows = self.connection.execute(
                "SELECT q.quote_id,q.trade_date,q.close_usd FROM price_index_quotes q "
                "JOIN (SELECT trade_date,max(quote_id) AS quote_id FROM price_index_quotes "
                "WHERE price_index=? AND trade_date<=? GROUP BY trade_date) latest "
                "ON latest.quote_id=q.quote_id ORDER BY q.trade_date DESC LIMIT ?",
                (index, anchor_date, sessions),
            ).fetchall()
            if len(rows) < sessions:
                raise InvalidState(
                    f"{index} 在 {anchor_date} 前仅有 {len(rows)} 个交易日报价，需要 {sessions} 个"
                )
            locked[index] = [
                QuotePoint(index, item["trade_date"], int(item["quote_id"]), Decimal(item["close_usd"]))
                for item in rows
            ]
        return locked

    def _compute_decision(
        self,
        strategy_id: str,
        anchor_date: str,
        *,
        current_prices: Mapping[str, Decimal] | None = None,
        incoming: Mapping[str, Decimal] | None = None,
        reference_prices: Mapping[str, Decimal] | None = None,
        rule_version_id: int | None = None,
    ) -> dict[str, Any]:
        try:
            anchor = _dt.date.fromisoformat(anchor_date).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("anchor_date 必须是 YYYY-MM-DD") from exc
        strategy = self._strategy_row(strategy_id)
        if rule_version_id is None:
            version = self._rule_version_at(strategy_id, anchor)
        else:
            version = self.connection.execute(
                "SELECT * FROM gasoline_rule_versions WHERE rule_version_id=? AND strategy_id=?",
                (rule_version_id, strategy_id),
            ).fetchone()
            if version is None:
                raise NotFound("政策规则版本不存在")
        rule = PricingRule.from_dict(json.loads(version["rule_json"]))
        indexes = json.loads(strategy["price_indexes_json"])
        quotes = self._lock_quotes(indexes, rule.window_sessions, anchor)

        if current_prices is None or incoming is None or reference_prices is None:
            prior = self.connection.execute(
                "SELECT * FROM gasoline_decisions WHERE strategy_id=? AND state='published' "
                "AND anchor_date<? ORDER BY anchor_date DESC,decision_id DESC LIMIT 1",
                (strategy_id, anchor),
            ).fetchone()
            if prior is None:
                if anchor < strategy["baseline_anchor_date"]:
                    raise InvalidState("首个调价锚定日不能早于策略基准价日期")
                default_current, default_carry, default_reference = self._baseline_state(strategy)
            else:
                default_current, default_carry, default_reference = self._state_after(prior)
            current_prices = current_prices if current_prices is not None else default_current
            incoming = incoming if incoming is not None else default_carry
            reference_prices = reference_prices if reference_prices is not None else default_reference

        result = evaluate_window(rule, quotes, current_prices, incoming, anchor, reference_prices)
        carry_out = {
            product: Decimal(str(result["products"][product]["outgoing_change"]))
            for product in GASOLINE_PRODUCTS
        }
        carry_in_text = {product: decimal_text(Decimal(str(incoming[product]))) for product in GASOLINE_PRODUCTS}
        carry_out_text = {product: decimal_text(carry_out[product]) for product in GASOLINE_PRODUCTS}
        input_value = {
            "strategy_id": strategy_id,
            "anchor_date": anchor,
            "rule_version_id": version["rule_version_id"],
            "rule_sha256": rule.sha256,
            "current_prices": {product: decimal_text(Decimal(str(current_prices[product]))) for product in GASOLINE_PRODUCTS},
            "reference_prices": {product: decimal_text(Decimal(str(reference_prices[product]))) for product in GASOLINE_PRODUCTS},
            "carry_in": carry_in_text,
            "locked_quotes": result["locked_quotes"],
        }
        return {
            "strategy_id": strategy_id,
            "anchor_date": anchor,
            "rule_version_id": version["rule_version_id"],
            "rule_sha256": rule.sha256,
            "input_sha256": digest(input_value),
            "result": result,
            "carry_in": carry_in_text,
            "carry_out": carry_out_text,
        }

    def _decision_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "decision_id": row["decision_id"],
            "strategy_id": row["strategy_id"],
            "anchor_date": row["anchor_date"],
            "rule_version_id": row["rule_version_id"],
            "rule_sha256": row["rule_sha256"],
            "input_sha256": row["input_sha256"],
            "result": json.loads(row["result_json"]),
            "carry_in": json.loads(row["carry_in_json"]),
            "carry_out": json.loads(row["carry_out_json"]),
            "mode": row["mode"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
        }

    def _flag_quote_correction(self, actor_id: str, new_quote_id: int) -> None:
        new_row = self.connection.execute(
            "SELECT * FROM price_index_quotes WHERE quote_id=?", (new_quote_id,)
        ).fetchone()
        if new_row is None:
            return
        old_row = self.connection.execute(
            "SELECT quote_id,close_usd FROM price_index_quotes WHERE price_index=? AND trade_date=? "
            "AND quote_id<? ORDER BY quote_id DESC LIMIT 1",
            (new_row["price_index"], new_row["trade_date"], new_quote_id),
        ).fetchone()
        if old_row is None:
            return
        locked = self.connection.execute(
            "SELECT d.decision_id,d.strategy_id,d.anchor_date FROM gasoline_decision_quotes q "
            "JOIN gasoline_decisions d ON d.decision_id=q.decision_id "
            "WHERE q.price_index=? AND q.trade_date=? AND q.quote_id<? AND d.state='published' "
            "ORDER BY d.anchor_date,d.decision_id",
            (new_row["price_index"], new_row["trade_date"], new_quote_id),
        ).fetchall()
        if not locked:
            return
        affected_ids = [int(item["decision_id"]) for item in locked]
        stub = {
            "price_index": new_row["price_index"],
            "trade_date": new_row["trade_date"],
            "affected_anchor_dates": [item["anchor_date"] for item in locked],
            "note": "最新报价修订与落账决定锁定的修订不同，请查看级联重算建议",
        }
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO gasoline_quote_corrections(price_index,trade_date,old_quote_id,new_quote_id,"
                    "old_close_usd,new_close_usd,source_revision,affected_decision_ids_json,"
                    "recalc_suggestion_json,noted_by,noted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_row["price_index"],
                        new_row["trade_date"],
                        old_row["quote_id"],
                        new_quote_id,
                        old_row["close_usd"],
                        new_row["close_usd"],
                        new_row["source_revision"],
                        canonical_json(affected_ids),
                        canonical_json(stub),
                        actor_id,
                        self._now(),
                    ),
                )
                correction_id = int(cursor.lastrowid)
                self._audit(
                    "gasoline_quote_correction", str(correction_id),
                    "gasoline.quote_correction.flagged", actor_id,
                    {"affected_decision_ids": affected_ids, "new_quote_id": new_quote_id},
                )
        except sqlite3.IntegrityError:
            # 同一修订重复处理时保持幂等。
            return
