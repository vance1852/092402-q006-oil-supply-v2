"""汽油指导价服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pricing_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','reviewer','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 原油基准报价按交易日与来源修订登记，历史修订不覆盖。
CREATE TABLE IF NOT EXISTS crude_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    price_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_usd TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES crude_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES pricing_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(price_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_crude_quotes_series
ON crude_quotes(price_index, trade_date, quote_id);

-- 调价规则版本：草稿、复核中、已批准（等待定时生效）、生效中、已退役。
CREATE TABLE IF NOT EXISTS pricing_rule_versions (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    document_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    retired_at TEXT,
    state TEXT NOT NULL
        CHECK(state IN ('draft','in_review','approved','active','retired','rejected')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES pricing_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(rule_id, version)
);

CREATE INDEX IF NOT EXISTS idx_rule_effective
ON pricing_rule_versions(state, effective_from);

-- 复核与批准记录。
CREATE TABLE IF NOT EXISTS pricing_rule_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('submit','approve','reject','retire')),
    actor_id TEXT NOT NULL REFERENCES pricing_users(user_id),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(rule_id, version) REFERENCES pricing_rule_versions(rule_id, version)
);

-- 调价决定：只有正式发布才落账，一经发布不可变。
-- 预览只返回计算结果，不写入本表。
CREATE TABLE IF NOT EXISTS pricing_decisions (
    decision_id TEXT PRIMARY KEY,
    evaluation_date TEXT NOT NULL,
    grade TEXT NOT NULL CHECK(grade IN ('92','95')),
    rule_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'published' CHECK(state IN ('published')),
    input_sha256 TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    superseded_flag INTEGER NOT NULL DEFAULT 0 CHECK(superseded_flag IN (0,1)),
    publish_idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES pricing_users(user_id),
    created_at TEXT NOT NULL,
    published_at TEXT NOT NULL,
    FOREIGN KEY(rule_id, rule_version) REFERENCES pricing_rule_versions(rule_id, version),
    UNIQUE(grade, evaluation_date)
);

CREATE INDEX IF NOT EXISTS idx_decisions_grade_date
ON pricing_decisions(grade, evaluation_date);

CREATE INDEX IF NOT EXISTS idx_decisions_published
ON pricing_decisions(state, grade, evaluation_date);

-- 跨周期累计量（暂缓调整时结转入下一周期），按牌号链式保存。
CREATE TABLE IF NOT EXISTS pricing_carry_ledger (
    grade TEXT PRIMARY KEY CHECK(grade IN ('92','95')),
    accumulated_change_cny TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    last_decision_id TEXT,
    updated_at TEXT NOT NULL
);

-- 报价修订影响标记：只记录受影响的已发布决定与重算建议，绝不改写决定本身。
CREATE TABLE IF NOT EXISTS pricing_revision_impacts (
    impact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id INTEGER NOT NULL REFERENCES crude_quotes(quote_id),
    decision_id TEXT NOT NULL REFERENCES pricing_decisions(decision_id),
    status TEXT NOT NULL DEFAULT 'flagged' CHECK(status IN ('flagged','recalc_suggested','dismissed','recalculated')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(quote_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_impacts_decision
ON pricing_revision_impacts(decision_id, status);

-- 重算建议。
CREATE TABLE IF NOT EXISTS pricing_recalc_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES pricing_decisions(decision_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','accepted','rejected')),
    proposed_preview_sha256 TEXT,
    created_by TEXT NOT NULL REFERENCES pricing_users(user_id),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS pricing_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS pricing_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pricing_audit_entity
ON pricing_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
