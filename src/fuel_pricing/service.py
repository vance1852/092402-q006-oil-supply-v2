"""汽油指导价的规则版本、报价锁定、决定发布与修订影响用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .engine import EngineError, cycle_dates, evaluate_grade, money, text, window_workdays
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import RuleDraft
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "rule.write", "rule.submit", "rule.retire", "decision.run"},
    "reviewer": {"rule.review", "rule.activate"},
    "risk": {"recalc.manage", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

CANONICAL_SEPARATORS = (",", ":")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=CANONICAL_SEPARATORS)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class FuelPricingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ----- 基础 -----------------------------------------------------------

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM pricing_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM pricing_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = digest(body)
        self.connection.execute(
            "INSERT INTO pricing_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO pricing_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ----- 报价登记与修订 -------------------------------------------------

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        price_index = str(raw.get("price_index", "")).strip().upper()
        trade_date = str(raw.get("trade_date", "")).strip()
        source_revision = str(raw.get("source_revision", "")).strip()
        if not price_index or not trade_date or not source_revision:
            raise ValidationFailed("price_index、trade_date、source_revision 不能为空")
        try:
            date.fromisoformat(trade_date)
            close_decimal = Decimal(str(raw.get("close_usd")))
        except (ValueError, ArithmeticError) as exc:
            raise ValidationFailed("close_usd 必须是十进制数值") from exc
        if not close_decimal.is_finite() or close_decimal <= 0:
            raise ValidationFailed("close_usd 必须是正数")
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM crude_quotes WHERE price_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (price_index, trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == source_revision:
            raise Conflict("同一来源修订已登记")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO crude_quotes(price_index,trade_date,close_usd,source_revision,"
                "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?)",
                (
                    price_index,
                    trade_date,
                    text(money(close_decimal)),
                    source_revision,
                    None if previous is None else previous["quote_id"],
                    actor_id,
                    self._now(),
                ),
            )
            quote_id = int(cursor.lastrowid)
            affected = self._flag_decisions_for_quote(quote_id, price_index, trade_date, actor_id)
            self._audit(
                "quote",
                str(quote_id),
                "quote.recorded",
                actor_id,
                {"price_index": price_index, "trade_date": trade_date,
                 "is_revision": previous is not None, "affected_decisions": affected},
            )
        return {"quote_id": quote_id, "price_index": price_index, "trade_date": trade_date,
                "revision": source_revision, "affected_decisions": affected}

    def _locked_quotes(self, price_index: str, trade_dates: list[str]) -> list[dict[str, Any]]:
        """每个交易日锁定登记时刻最新（quote_id 最大）的来源修订。"""

        if not trade_dates:
            return []
        placeholders = ",".join("?" for _ in trade_dates)
        rows = self.connection.execute(
            f"SELECT q.quote_id,q.trade_date,q.close_usd,q.source_revision FROM crude_quotes q "
            f"JOIN (SELECT trade_date,max(quote_id) AS quote_id FROM crude_quotes "
            f"WHERE price_index=? AND trade_date IN ({placeholders}) GROUP BY trade_date) latest "
            f"ON latest.quote_id=q.quote_id ORDER BY q.trade_date",
            [price_index, *trade_dates],
        ).fetchall()
        return [dict(row) for row in rows]

    def _flag_decisions_for_quote(
        self, quote_id: int, price_index: str, trade_date: str, actor_id: str
    ) -> list[str]:
        """报价修订只标出受影响的已发布决定并发起重算建议，绝不改写历史。"""

        decisions = self.connection.execute(
            "SELECT * FROM pricing_decisions ORDER BY evaluation_date,grade"
        ).fetchall()
        now_text = self._now()
        affected_ids: list[str] = []
        for row in decisions:
            snapshot = json.loads(row["input_snapshot_json"])
            if snapshot.get("price_index") != price_index:
                continue
            locked_ids = {quote["quote_id"] for quote in snapshot.get("locked_quotes", [])}
            if trade_date not in snapshot.get("window_trade_dates", []) or quote_id in locked_ids:
                continue
            # 仅当新修订确实取代了该决定锁定的旧报价时标记。
            if not any(q["trade_date"] == trade_date for q in snapshot["locked_quotes"]):
                continue
            self._insert_impact(quote_id, row["decision_id"], "window_input", now_text)
            self._mark_superseded(row["decision_id"])
            affected_ids.append(row["decision_id"])
            # 账面价格经 carry 链传递，标出同牌号全部后续周期决定。
            later = self.connection.execute(
                "SELECT * FROM pricing_decisions WHERE grade=? AND evaluation_date>? ORDER BY evaluation_date",
                (row["grade"], row["evaluation_date"]),
            ).fetchall()
            for downstream in later:
                self._insert_impact(quote_id, downstream["decision_id"], "carry_chain", now_text)
                self._mark_superseded(downstream["decision_id"])
                affected_ids.append(downstream["decision_id"])
            self._open_recalc_suggestion(row, price_index, trade_date, actor_id, now_text)
        return affected_ids

    def _mark_superseded(self, decision_id: str) -> None:
        self.connection.execute(
            "UPDATE pricing_decisions SET superseded_flag=1 WHERE decision_id=?", (decision_id,)
        )

    def _insert_impact(self, quote_id: int, decision_id: str, note: str, now_text: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO pricing_revision_impacts(quote_id,decision_id,note,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (quote_id, decision_id, note, now_text, now_text),
        )

    def _open_recalc_suggestion(
        self,
        decision_row: sqlite3.Row,
        price_index: str,
        trade_date: str,
        actor_id: str,
        now_text: str,
    ) -> None:
        existing = self.connection.execute(
            "SELECT suggestion_id FROM pricing_recalc_suggestions WHERE decision_id=? AND status='open'",
            (decision_row["decision_id"],),
        ).fetchone()
        if existing is not None:
            return
        suggestion_id = f"recalc-{decision_row['decision_id']}"
        reason = (
            f"报价修订：{price_index} {trade_date} 出现更新来源修订，"
            f"决定 {decision_row['decision_id']} 的锁定输入已被取代，建议以历史规则快照重算。"
        )
        self.connection.execute(
            "INSERT INTO pricing_recalc_suggestions(suggestion_id,decision_id,reason,created_by,created_at) "
            "VALUES(?,?,?,?,?)",
            (suggestion_id, decision_row["decision_id"], reason, actor_id, now_text),
        )
        self._audit(
            "recalc_suggestion",
            suggestion_id,
            "recalc.suggested",
            actor_id,
            {"decision_id": decision_row["decision_id"], "trade_date": trade_date},
        )

    # ----- 规则版本生命周期 -----------------------------------------------

    def create_rule_draft(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        draft = RuleDraft.from_dict(raw)
        document = draft.document()
        content_sha = digest(document)
        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT version,state FROM pricing_rule_versions WHERE rule_id=? ORDER BY version DESC LIMIT 1",
                (draft.rule_id,),
            ).fetchone()
            if latest is not None and latest["state"] in ("draft", "in_review"):
                raise InvalidState("该规则存在尚未完成复核的草稿版本，不能再开新版本")
            version = 1 if latest is None else int(latest["version"]) + 1
            try:
                self.connection.execute(
                    "INSERT INTO pricing_rule_versions(rule_id,version,document_json,content_sha256,"
                    "effective_from,state,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        draft.rule_id,
                        version,
                        canonical_json(document),
                        content_sha,
                        draft.effective_from,
                        "draft",
                        actor_id,
                        self._now(),
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("规则编号或内容已经存在") from exc
            self._audit("rule", f"{draft.rule_id}:v{version}", "rule.draft_created", actor_id,
                        {"effective_from": draft.effective_from, "sha256": content_sha})
        return self.get_rule_version(draft.rule_id, version)

    def update_rule_draft(self, actor_id: str, rule_id: str, version: int, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        row = self._rule_row(rule_id, version)
        if row["state"] != "draft":
            raise InvalidState("只有草稿版本可以修改")
        draft = RuleDraft.from_dict(raw)
        if draft.rule_id != rule_id:
            raise ValidationFailed("不能修改规则编号")
        document = draft.document()
        content_sha = digest(document)
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "UPDATE pricing_rule_versions SET document_json=?,content_sha256=?,effective_from=?,"
                    "updated_at=? WHERE rule_id=? AND version=? AND state='draft'",
                    (canonical_json(document), content_sha, draft.effective_from, self._now(), rule_id, version),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("规则内容与其他版本重复") from exc
            self._audit("rule", f"{rule_id}:v{version}", "rule.draft_updated", actor_id,
                        {"effective_from": draft.effective_from, "sha256": content_sha})
        return self.get_rule_version(rule_id, version)

    def _rule_row(self, rule_id: str, version: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM pricing_rule_versions WHERE rule_id=? AND version=?", (rule_id, version)
        ).fetchone()
        if row is None:
            raise NotFound("规则版本不存在")
        return row

    def _review_note(self, rule_id: str, version: int, action: str, actor_id: str, comment: str) -> None:
        self.connection.execute(
            "INSERT INTO pricing_rule_reviews(rule_id,version,action,actor_id,comment,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (rule_id, version, action, actor_id, comment, self._now()),
        )

    def submit_rule(self, actor_id: str, rule_id: str, version: int, expected_revision: int, comment: str = "") -> dict[str, Any]:
        self._require(actor_id, "rule.submit")
        self._rule_row(rule_id, version)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE pricing_rule_versions SET state='in_review',revision=revision+1,updated_at=? "
                "WHERE rule_id=? AND version=? AND state='draft' AND revision=?",
                (self._now(), rule_id, version, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("规则不是当前草稿版本")
            self._review_note(rule_id, version, "submit", actor_id, comment)
            self._audit("rule", f"{rule_id}:v{version}", "rule.submitted", actor_id, {"comment": comment})
        return self.get_rule_version(rule_id, version)

    def review_rule(
        self,
        actor_id: str,
        rule_id: str,
        version: int,
        expected_revision: int,
        approve: bool,
        comment: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "rule.review")
        self._rule_row(rule_id, version)
        target_state = "approved" if approve else "rejected"
        with transaction(self.connection, immediate=True):
            if approve:
                self._assert_no_overlap(rule_id, version)
            cursor = self.connection.execute(
                "UPDATE pricing_rule_versions SET state=?,revision=revision+1,updated_at=? "
                "WHERE rule_id=? AND version=? AND state='in_review' AND revision=?",
                (target_state, self._now(), rule_id, version, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("规则不是当前复核版本")
            self._review_note(rule_id, version, "approve" if approve else "reject", actor_id, comment)
            self._audit("rule", f"{rule_id}:v{version}",
                        "rule.approved" if approve else "rule.rejected", actor_id, {"comment": comment})
        return self.get_rule_version(rule_id, version)

    def _assert_no_overlap(self, rule_id: str, version: int) -> None:
        """生效半开区间 [effective_from, retired_at) 不得与任何在途版本重叠。"""

        row = self._rule_row(rule_id, version)
        start = row["effective_from"]
        others = self.connection.execute(
            "SELECT rule_id,version,effective_from,retired_at,state FROM pricing_rule_versions "
            "WHERE state IN ('approved','active') AND NOT (rule_id=? AND version=?)",
            (rule_id, version),
        ).fetchall()
        for other in others:
            other_end = other["retired_at"]
            if other_end is None or other_end > start:
                raise Conflict(
                    f"生效区间与 {other['rule_id']} v{other['version']} "
                    f"（{other['effective_from']} 起生效）重叠；请先将旧版本退役日期设定为 {start}"
                )

    def activate_due_rules(self, actor_id: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        """定时生效与定时退役：由定时任务每日调用，幂等。

        - 生效日已到的 approved 版本转为 active；
        - retired_at 已到的 active 版本转为 retired。
        """

        if actor_id is not None:
            self._require(actor_id, "rule.activate")
        today_text = as_of or self._now()[:10]
        date.fromisoformat(today_text)
        activated: list[str] = []
        retired: list[str] = []
        with transaction(self.connection, immediate=True):
            due = self.connection.execute(
                "SELECT * FROM pricing_rule_versions WHERE state='approved' AND effective_from<=? "
                "ORDER BY effective_from,version",
                (today_text,),
            ).fetchall()
            for row in due:
                self.connection.execute(
                    "UPDATE pricing_rule_versions SET state='active',updated_at=? WHERE rule_id=? AND version=?",
                    (self._now(), row["rule_id"], row["version"]),
                )
                activated.append(f"{row['rule_id']}:v{row['version']}")
                if actor_id is not None:
                    self._audit("rule", f"{row['rule_id']}:v{row['version']}", "rule.activated",
                                actor_id, {"effective_from": row["effective_from"]})
            expired = self.connection.execute(
                "SELECT * FROM pricing_rule_versions WHERE state='active' AND retired_at IS NOT NULL "
                "AND retired_at<=? ORDER BY retired_at,version",
                (today_text,),
            ).fetchall()
            for row in expired:
                self.connection.execute(
                    "UPDATE pricing_rule_versions SET state='retired',updated_at=? WHERE rule_id=? AND version=?",
                    (self._now(), row["rule_id"], row["version"]),
                )
                retired.append(f"{row['rule_id']}:v{row['version']}")
                if actor_id is not None:
                    self._audit("rule", f"{row['rule_id']}:v{row['version']}", "rule.retired_effective",
                                actor_id, {"retired_at": row["retired_at"]})
        return {"as_of": today_text, "activated": activated, "retired": retired}

    def retire_rule(
        self,
        actor_id: str,
        rule_id: str,
        version: int,
        expected_revision: int,
        retire_date: str,
        comment: str = "",
    ) -> dict[str, Any]:
        """设定版本区间结束日。

        - active 版本：retire_date 晚于生效日时预定退役（到点由定时任务转 retired），
          不晚于今天则立即退役；
        - approved 待生效版本：retire_date 等于生效日表示取消（空区间），
          晚于生效日表示预定其未来退役日；
        无论哪种情况都校验半开区间不与其他在途版本重叠。
        """

        self._require(actor_id, "rule.retire")
        date.fromisoformat(retire_date)
        row = self._rule_row(rule_id, version)
        if row["state"] not in ("active", "approved"):
            raise InvalidState("只有生效中或已批准待生效的版本可以退役")
        if retire_date < row["effective_from"]:
            raise ValidationFailed("退役日期不能早于生效日期")
        cancel_pending = row["state"] == "approved" and retire_date == row["effective_from"]
        if row["state"] == "active" and retire_date == row["effective_from"]:
            raise ValidationFailed("退役日期必须晚于生效日期")
        with transaction(self.connection, immediate=True):
            self._assert_interval_clear(rule_id, version, row["effective_from"], retire_date)
            new_state = "retired" if cancel_pending or (
                row["state"] == "active" and retire_date <= self._now()[:10]
            ) else row["state"]
            cursor = self.connection.execute(
                "UPDATE pricing_rule_versions SET state=?,retired_at=?,revision=revision+1,updated_at=? "
                "WHERE rule_id=? AND version=? AND revision=? AND state IN ('active','approved')",
                (new_state, retire_date, self._now(), rule_id, version, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("规则不是当前版本")
            self._review_note(rule_id, version, "retire", actor_id, comment)
            event_type = (
                "rule.retired" if new_state == "retired" else "rule.retire_scheduled"
            )
            self._audit("rule", f"{rule_id}:v{version}", event_type,
                        actor_id, {"retire_date": retire_date, "comment": comment})
        return self.get_rule_version(rule_id, version)

    def _assert_interval_clear(
        self, rule_id: str, version: int, start: str, end: str
    ) -> None:
        others = self.connection.execute(
            "SELECT rule_id,version,effective_from,retired_at,state FROM pricing_rule_versions "
            "WHERE state IN ('approved','active') AND NOT (rule_id=? AND version=?)",
            (rule_id, version),
        ).fetchall()
        for other in others:
            other_end = other["retired_at"]
            # 半开区间重叠：other.start < end 且 start < other.end(open => True)
            if other["effective_from"] < end and (other_end is None or start < other_end):
                raise Conflict(
                    f"退役区间与 {other['rule_id']} v{other['version']} "
                    f"（{other['effective_from']} 起生效）重叠"
                )

    def get_rule_version(self, rule_id: str, version: int) -> dict[str, Any]:
        row = self._rule_row(rule_id, version)
        return {
            "rule_id": row["rule_id"],
            "version": row["version"],
            "state": row["state"],
            "revision": row["revision"],
            "effective_from": row["effective_from"],
            "retired_at": row["retired_at"],
            "content_sha256": row["content_sha256"],
            "document": json.loads(row["document_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_rules(self) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT rule_id,version,state,revision,effective_from,retired_at,content_sha256 "
            "FROM pricing_rule_versions ORDER BY rule_id,version"
        ).fetchall()
        return {"versions": [dict(row) for row in rows]}

    def effective_rule(self, evaluation_date: str) -> dict[str, Any]:
        date.fromisoformat(evaluation_date)
        row = self._effective_rule_row(evaluation_date)
        return self.get_rule_version(row["rule_id"], row["version"])

    def _effective_rule_row(self, evaluation_date: str) -> sqlite3.Row:
        # approved 版本必须先由定时任务转为 active 才可用于计算（失败关闭）。
        row = self.connection.execute(
            "SELECT * FROM pricing_rule_versions WHERE state IN ('active','retired') "
            "AND effective_from<=? AND (retired_at IS NULL OR retired_at>?) "
            "ORDER BY effective_from DESC,version DESC,rule_id LIMIT 1",
            (evaluation_date, evaluation_date),
        ).fetchone()
        if row is None:
            raise NotFound(f"{evaluation_date} 没有生效规则版本")
        return row

    # ----- 决定预览与发布 -------------------------------------------------

    def _resolve_rule(self, evaluation_date: str, rule_id: str | None, version: int | None) -> sqlite3.Row:
        if rule_id is not None and version is not None:
            row = self._rule_row(rule_id, version)
            if row["state"] not in ("active", "retired"):
                raise InvalidState("指定规则版本尚未定时生效")
            if row["effective_from"] > evaluation_date or (
                row["retired_at"] is not None and row["retired_at"] <= evaluation_date
            ):
                raise InvalidState("指定规则版本在该日期不在生效区间内")
            return row
        try:
            return self._effective_rule_row(evaluation_date)
        except NotFound as exc:
            raise InvalidState(str(exc)) from exc

    def _previous_decision(self, grade: str, evaluation_date: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM pricing_decisions WHERE grade=? AND evaluation_date<? "
            "ORDER BY evaluation_date DESC LIMIT 1",
            (grade, evaluation_date),
        ).fetchone()

    def _compute(
        self,
        *,
        grade: str,
        evaluation_date: str,
        rule_row: sqlite3.Row,
        previous_decision_id: str | None,
        previous_price: Decimal,
        carry_in: Decimal,
        quotes: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        document = json.loads(rule_row["document_json"])
        eval_d = date.fromisoformat(evaluation_date)
        window = [day.isoformat() for day in window_workdays(eval_d, int(document["quote_window_workdays"]))]
        locked = quotes if quotes is not None else self._locked_quotes(document["price_index"], window)
        try:
            result = evaluate_grade(
                rule_document=document,
                grade=grade,
                evaluation_date=evaluation_date,
                window_dates=window,
                quotes=locked,
                previous_price=previous_price,
                carry_in=carry_in,
            )
        except EngineError as exc:
            raise InvalidState(str(exc)) from exc
        cycle_workdays = int(document["cycle_workdays"])
        matched, previous_cycle, _ = cycle_dates(
            date.fromisoformat(str(document["cycle_anchor_date"])), cycle_workdays, eval_d
        )
        result["cycle_workdays"] = cycle_workdays
        result["is_cycle_date"] = bool(matched)
        result["previous_cycle_date"] = None if previous_cycle is None else previous_cycle.isoformat()
        snapshot = {
            "rule_id": rule_row["rule_id"],
            "rule_version": rule_row["version"],
            "rule_content_sha256": rule_row["content_sha256"],
            "price_index": document["price_index"],
            "grade": grade,
            "evaluation_date": evaluation_date,
            "cycle_anchor_date": document["cycle_anchor_date"],
            "cycle_workdays": cycle_workdays,
            "window_trade_dates": window,
            "locked_quotes": [
                {
                    "quote_id": int(quote["quote_id"]),
                    "trade_date": quote["trade_date"],
                    "source_revision": quote["source_revision"],
                    "close_usd": quote["close_usd"],
                }
                for quote in sorted(locked, key=lambda item: item["trade_date"])
            ],
            "previous_decision_id": previous_decision_id,
            "previous_guide_price_cny": text(money(previous_price)),
            "carry_in_cny": text(money(carry_in)),
        }
        return result, snapshot

    def _preceding_context(
        self, grade: str, evaluation_date: str, rule_document: Mapping[str, Any]
    ) -> tuple[str | None, Decimal, Decimal]:
        """返回 (上一决定ID, 上期指导价, 结转累计)。"""

        previous_row = self._previous_decision(grade, evaluation_date)
        if previous_row is not None:
            previous_result = json.loads(previous_row["result_json"])
            return (
                previous_row["decision_id"],
                Decimal(previous_result["guide_price_cny"]),
                Decimal(previous_result["carry_out_cny"]),
            )
        products = {item["grade"]: item for item in rule_document["products"]}
        return None, Decimal(str(products[grade]["base_guide_price"])), Decimal("0")

    def preview(
        self,
        actor_id: str,
        grade: str,
        evaluation_date: str,
        rule_id: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        """预览某日结果，不落账、不改累计台账。"""

        self._require(actor_id, "decision.run")
        self._validate_grade_date(grade, evaluation_date)
        rule_row = self._resolve_rule(evaluation_date, rule_id, version)
        document = json.loads(rule_row["document_json"])
        prev_id, prev_price, carry_in = self._preceding_context(grade, evaluation_date, document)
        result, snapshot = self._compute(
            grade=grade,
            evaluation_date=evaluation_date,
            rule_row=rule_row,
            previous_decision_id=prev_id,
            previous_price=prev_price,
            carry_in=carry_in,
        )
        return {
            "mode": "preview",
            "rule_id": rule_row["rule_id"],
            "rule_version": rule_row["version"],
            "input_sha256": digest(snapshot),
            "snapshot": snapshot,
            "result": result,
        }

    @staticmethod
    def _validate_grade_date(grade: str, evaluation_date: str) -> None:
        if grade not in ("92", "95"):
            raise ValidationFailed("grade 只能是 92 或 95")
        try:
            date.fromisoformat(evaluation_date)
        except ValueError as exc:
            raise ValidationFailed("evaluation_date 必须是 YYYY-MM-DD 日期") from exc

    def publish(
        self,
        actor_id: str,
        decision_id: str,
        grade: str,
        evaluation_date: str,
        idempotency_key: str,
        rule_id: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        """正式发布不可变决定；重复发布（同键或同输入）返回同一记录。"""

        self._require(actor_id, "decision.run")
        self._validate_grade_date(grade, evaluation_date)
        if not decision_id.strip() or not idempotency_key.strip():
            raise ValidationFailed("decision_id 与 idempotency_key 不能为空")
        request_digest = digest(
            {"grade": grade, "evaluation_date": evaluation_date}
        )
        stored_key = self.connection.execute(
            "SELECT request_sha256,response_json FROM pricing_idempotency WHERE scope='decision' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored_key is not None:
            if stored_key["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同发布请求")
            return {**json.loads(stored_key["response_json"]), "replayed": True}
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT * FROM pricing_decisions WHERE grade=? AND evaluation_date=?",
                (grade, evaluation_date),
            ).fetchone()
            rule_row = self._resolve_rule(evaluation_date, rule_id, version)
            document = json.loads(rule_row["document_json"])
            prev_id, prev_price, carry_in = self._preceding_context(grade, evaluation_date, document)
            result, snapshot = self._compute(
                grade=grade,
                evaluation_date=evaluation_date,
                rule_row=rule_row,
                previous_decision_id=prev_id,
                previous_price=prev_price,
                carry_in=carry_in,
            )
            if existing is not None:
                # 不可变决定：同输入的重复发布返回同一记录，不同输入一律拒绝。
                if existing["input_sha256"] == digest(snapshot):
                    response = self._decision_response(existing)
                    self.connection.execute(
                        "INSERT OR IGNORE INTO pricing_idempotency(scope,idempotency_key,request_sha256,"
                        "response_json,created_at) VALUES('decision',?,?,?,?)",
                        (idempotency_key, request_digest, canonical_json(response), self._now()),
                    )
                    return {**response, "replayed": True}
                raise Conflict("该牌号与评估日已存在不可变决定，不能发布不同结果")
            if not result["is_cycle_date"]:
                raise InvalidState("评估日不是调整周期日，不能正式发布；可使用预览")
            input_sha = digest(snapshot)
            now_text = self._now()
            try:
                self.connection.execute(
                    "INSERT INTO pricing_decisions(decision_id,evaluation_date,grade,rule_id,rule_version,"
                    "input_sha256,input_snapshot_json,result_json,publish_idempotency_key,"
                    "created_by,created_at,published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision_id,
                        evaluation_date,
                        grade,
                        rule_row["rule_id"],
                        rule_row["version"],
                        input_sha,
                        canonical_json(snapshot),
                        canonical_json(result),
                        idempotency_key,
                        actor_id,
                        now_text,
                        now_text,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("决定编号、幂等键或同周期决定冲突") from exc
            response = self._decision_response(
                self.connection.execute(
                    "SELECT * FROM pricing_decisions WHERE decision_id=?", (decision_id,)
                ).fetchone()
            )
            self.connection.execute(
                "INSERT INTO pricing_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('decision',?,?,?,?)",
                (idempotency_key, request_digest, canonical_json(response), now_text),
            )
            self.connection.execute(
                "INSERT INTO pricing_carry_ledger(grade,accumulated_change_cny,as_of_date,last_decision_id,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(grade) DO UPDATE SET accumulated_change_cny=excluded.accumulated_change_cny,"
                "as_of_date=excluded.as_of_date,last_decision_id=excluded.last_decision_id,updated_at=excluded.updated_at",
                (grade, result["carry_out_cny"], evaluation_date, decision_id, now_text),
            )
            self._audit("decision", decision_id, "decision.published", actor_id,
                        {"grade": grade, "evaluation_date": evaluation_date,
                         "rule": f"{rule_row['rule_id']}:v{rule_row['version']}",
                         "guide_price_cny": result["guide_price_cny"], "deferred": result["deferred"],
                         "input_sha256": input_sha})
        return {**response, "replayed": False}

    def _decision_response(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "decision_id": row["decision_id"],
            "evaluation_date": row["evaluation_date"],
            "grade": row["grade"],
            "state": row["state"],
            "rule_id": row["rule_id"],
            "rule_version": row["rule_version"],
            "input_sha256": row["input_sha256"],
            "input_superseded": bool(row["superseded_flag"]),
            "result": json.loads(row["result_json"]),
            "published_at": row["published_at"],
        }

    def get_decision(self, actor_id: str, decision_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM pricing_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise NotFound("决定不存在")
        return self._decision_response(row)

    def list_decisions(self, actor_id: str, grade: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        sql = (
            "SELECT decision_id,evaluation_date,grade,state,rule_id,rule_version,input_sha256,"
            "superseded_flag,published_at FROM pricing_decisions"
        )
        if grade is None:
            rows = self.connection.execute(f"{sql} ORDER BY evaluation_date,grade").fetchall()
        else:
            rows = self.connection.execute(
                f"{sql} WHERE grade=? ORDER BY evaluation_date", (grade,)
            ).fetchall()
        return {
            "decisions": [
                {**dict(row), "superseded_flag": bool(row["superseded_flag"])} for row in rows
            ]
        }

    def carry_status(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT * FROM pricing_carry_ledger ORDER BY grade"
        ).fetchall()
        return {"ledger": [dict(row) for row in rows]}

    # ----- 修订影响与重算建议 ---------------------------------------------

    def list_impacts(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT i.*,q.price_index,q.trade_date AS quote_trade_date,q.source_revision "
            "FROM pricing_revision_impacts i JOIN crude_quotes q ON q.quote_id=i.quote_id "
            "ORDER BY i.impact_id"
        ).fetchall()
        return {"impacts": [dict(row) for row in rows]}

    def list_recalc_suggestions(self, actor_id: str, status: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        if status is None:
            rows = self.connection.execute(
                "SELECT * FROM pricing_recalc_suggestions ORDER BY created_at"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM pricing_recalc_suggestions WHERE status=? ORDER BY created_at", (status,)
            ).fetchall()
        return {"suggestions": [dict(row) for row in rows]}

    def preview_recalc(self, actor_id: str, suggestion_id: str) -> dict[str, Any]:
        """以历史规则快照、历史账面价与当前最新报价修订重算，只返回对比，不改写历史。"""

        self._require(actor_id, "recalc.manage")
        suggestion = self.connection.execute(
            "SELECT * FROM pricing_recalc_suggestions WHERE suggestion_id=?", (suggestion_id,)
        ).fetchone()
        if suggestion is None:
            raise NotFound("重算建议不存在")
        decision = self.connection.execute(
            "SELECT * FROM pricing_decisions WHERE decision_id=?", (suggestion["decision_id"],)
        ).fetchone()
        rule_row = self._rule_row(decision["rule_id"], decision["rule_version"])
        document = json.loads(rule_row["document_json"])
        stored_snapshot = json.loads(decision["input_snapshot_json"])
        stored_result = json.loads(decision["result_json"])
        evaluation_date = decision["evaluation_date"]
        eval_d = date.fromisoformat(evaluation_date)
        window = [day.isoformat() for day in window_workdays(eval_d, int(document["quote_window_workdays"]))]
        current_quotes = self._locked_quotes(document["price_index"], window)
        result, snapshot = self._compute(
            grade=decision["grade"],
            evaluation_date=evaluation_date,
            rule_row=rule_row,
            previous_decision_id=stored_snapshot["previous_decision_id"],
            previous_price=Decimal(stored_snapshot["previous_guide_price_cny"]),
            carry_in=Decimal(stored_snapshot["carry_in_cny"]),
            quotes=current_quotes,
        )
        # 周期属性是发布时刻的事实，不随报价修订而变化，沿用原结果。
        result["is_cycle_date"] = stored_result["is_cycle_date"]
        result["previous_cycle_date"] = stored_result["previous_cycle_date"]
        proposed_sha = digest(snapshot)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE pricing_recalc_suggestions SET proposed_preview_sha256=? WHERE suggestion_id=?",
                (proposed_sha, suggestion_id),
            )
        return {
            "suggestion_id": suggestion_id,
            "decision_id": decision["decision_id"],
            "original_input_sha256": decision["input_sha256"],
            "original_result": stored_result,
            "recomputed_input_sha256": proposed_sha,
            "recomputed_result": result,
            "would_change": decision["input_sha256"] != proposed_sha,
            "history_unchanged": True,
        }

    def resolve_recalc_suggestion(
        self, actor_id: str, suggestion_id: str, action: str, comment: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "recalc.manage")
        if action not in ("accept", "reject"):
            raise ValidationFailed("action 只能是 accept 或 reject")
        with transaction(self.connection, immediate=True):
            suggestion = self.connection.execute(
                "SELECT * FROM pricing_recalc_suggestions WHERE suggestion_id=? AND status='open'",
                (suggestion_id,),
            ).fetchone()
            if suggestion is None:
                raise InvalidState("重算建议不存在或已处理")
            self.connection.execute(
                "UPDATE pricing_recalc_suggestions SET status=?,resolved_at=? WHERE suggestion_id=?",
                ("accepted" if action == "accept" else "rejected", self._now(), suggestion_id),
            )
            impacts = self.connection.execute(
                "SELECT impact_id FROM pricing_revision_impacts WHERE decision_id=?",
                (suggestion["decision_id"],),
            ).fetchall()
            new_status = "recalculated" if action == "accept" else "dismissed"
            for impact in impacts:
                self.connection.execute(
                    "UPDATE pricing_revision_impacts SET status=?,note=?,updated_at=? WHERE impact_id=?",
                    (new_status, comment or new_status, self._now(), impact["impact_id"]),
                )
            self._audit("recalc_suggestion", suggestion_id,
                        "recalc.accepted" if action == "accept" else "recalc.rejected",
                        actor_id, {"comment": comment, "decision_id": suggestion["decision_id"]})
        row = self.connection.execute(
            "SELECT * FROM pricing_recalc_suggestions WHERE suggestion_id=?", (suggestion_id,)
        ).fetchone()
        return dict(row)

    # ----- 审计 -----------------------------------------------------------

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM pricing_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            if row["previous_hash"] != previous_hash or row["event_hash"] != digest(body):
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
