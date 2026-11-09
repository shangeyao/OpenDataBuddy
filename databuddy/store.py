"""元数据存储（SQLite）。所有平台实体 —— 用户、工作空间、Catalog、指标、工作流、审计 —— 都在这里。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',          -- admin / member
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    owner_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',          -- admin / member
    PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS catalogs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, name)
);

CREATE TABLE IF NOT EXISTS schemas (
    id TEXT PRIMARY KEY,
    catalog_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (catalog_id, name)
);

CREATE TABLE IF NOT EXISTS tables (
    id TEXT PRIMARY KEY,
    schema_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT DEFAULT 'table',                    -- table / view
    description TEXT DEFAULT '',
    row_count INTEGER DEFAULT 0,
    source_id TEXT,
    tags TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    UNIQUE (schema_id, name)
);

CREATE TABLE IF NOT EXISTS columns (
    id TEXT PRIMARY KEY,
    table_id TEXT NOT NULL,
    name TEXT NOT NULL,
    data_type TEXT,
    description TEXT DEFAULT '',
    ordinal INTEGER DEFAULT 0,
    is_nullable INTEGER DEFAULT 1,
    created_at TEXT,
    UNIQUE (table_id, name)
);

CREATE TABLE IF NOT EXISTS column_tags (
    id TEXT PRIMARY KEY,
    column_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    level TEXT DEFAULT 'L1',                      -- L1 公开 / L2 内部 / L3 敏感 / L4 机密
    source TEXT DEFAULT 'manual',                 -- manual / ai / rule
    confidence REAL,
    evidence TEXT,
    created_at TEXT,
    UNIQUE (column_id, tag)
);

CREATE TABLE IF NOT EXISTS lineage (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    upstream TEXT,
    downstream TEXT,
    kind TEXT DEFAULT 'sql',
    detail TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,                           -- file / sqlite / postgres / mysql / http
    config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'unknown',                -- unknown / ok / failed
    last_test_at TEXT,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT,
    description TEXT DEFAULT '',
    expr TEXT NOT NULL,                           -- 聚合表达式，如 sum(gmv)
    table_ref TEXT NOT NULL,                       -- catalog.schema.table
    filters TEXT DEFAULT '',
    unit TEXT DEFAULT '',
    dimensions TEXT DEFAULT '[]',
    joins TEXT DEFAULT '[]',
    owner TEXT DEFAULT '',
    status TEXT DEFAULT 'published',
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, name)
);

CREATE TABLE IF NOT EXISTS knowledge_docs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT DEFAULT 'manual',            -- manual / upload / catalog
    tags TEXT DEFAULT '',
    scope TEXT DEFAULT 'workspace',
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    user_id TEXT,
    key TEXT,
    value TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    user_id TEXT,
    mode TEXT DEFAULT 'engineering',              -- engineering / governance / analysis
    chat_mode TEXT DEFAULT 'agent',               -- agent / ask
    title TEXT DEFAULT '新会话',
    model TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    role TEXT,                                    -- user / assistant / system
    content TEXT,
    meta TEXT DEFAULT '{}',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    tool TEXT,
    args TEXT,
    reason TEXT,
    status TEXT DEFAULT 'pending',                -- pending / approved / rejected
    created_at TEXT,
    decided_at TEXT,
    decided_by TEXT
);

CREATE TABLE IF NOT EXISTS flows (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    nodes TEXT DEFAULT '[]',
    edges TEXT DEFAULT '[]',
    cron TEXT,
    enabled INTEGER DEFAULT 0,
    retries INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS flow_runs (
    id TEXT PRIMARY KEY,
    flow_id TEXT,
    status TEXT DEFAULT 'running',                -- running / success / failed
    trigger TEXT DEFAULT 'manual',                -- manual / schedule
    started_at TEXT,
    finished_at TEXT,
    log TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS dashboards (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    spec TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS quality_rules (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    table_ref TEXT NOT NULL,
    column_name TEXT,
    rule_type TEXT NOT NULL,                      -- null_rate / uniqueness / range / freshness / custom_sql
    config TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    last_status TEXT,
    last_value TEXT,
    last_run_at TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    user_id TEXT,
    action TEXT,
    target TEXT,
    detail TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    workspace_id TEXT,
    step INTEGER DEFAULT 0,
    tool TEXT,
    args TEXT,
    result TEXT,
    ok INTEGER DEFAULT 1,
    duration_ms INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_tables_schema ON tables(schema_id);
CREATE INDEX IF NOT EXISTS idx_columns_table ON columns(table_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_traces_conv ON traces(conversation_id, step);
CREATE INDEX IF NOT EXISTS idx_flow_runs_flow ON flow_runs(flow_id, started_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:16]
    return f"{prefix}{raw}" if prefix else raw


@contextmanager
def db():
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    config.ensure_dirs()
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：给老库补列，避免升级时手工改库。"""
    additions = {
        "metrics": {"joins": "TEXT DEFAULT '[]'"},
        "tables": {"tags": "TEXT DEFAULT ''"},
        "flows": {"retries": "INTEGER DEFAULT 0"},
    }
    for table, cols in additions.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {decl}')


def rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def row(sql: str, params: Iterable[Any] = ()) -> dict | None:
    with db() as conn:
        r = conn.execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None


def scalar(sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    with db() as conn:
        r = conn.execute(sql, tuple(params)).fetchone()
        return r[0] if r and r[0] is not None else default


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with db() as conn:
        conn.execute(sql, tuple(params))


def insert(table: str, data: dict, auto_id: bool = True) -> dict:
    """插入一行。复合主键表（如 workspace_members）需传 auto_id=False。"""
    payload = dict(data)
    if auto_id:
        payload.setdefault("id", new_id())
    cols = ", ".join(f'"{k}"' for k in payload)
    marks = ", ".join("?" for _ in payload)
    with db() as conn:
        conn.execute(
            f'INSERT INTO "{table}" ({cols}) VALUES ({marks})', tuple(payload.values())
        )
    return payload


def update(table: str, row_id: str, data: dict) -> None:
    if not data:
        return
    sets = ", ".join(f'"{k}" = ?' for k in data)
    with db() as conn:
        conn.execute(
            f'UPDATE "{table}" SET {sets} WHERE id = ?', (*data.values(), row_id)
        )


def delete(table: str, row_id: str) -> None:
    with db() as conn:
        conn.execute(f'DELETE FROM "{table}" WHERE id = ?', (row_id,))


def get_setting(key: str, default: str = "") -> str:
    return scalar("SELECT value FROM settings WHERE key = ?", (key,), default) or default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_json_setting(key: str, default: Any = None) -> Any:
    raw = get_setting(key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def audit(
    workspace_id: str | None,
    user_id: str | None,
    action: str,
    target: str = "",
    detail: Any = None,
) -> None:
    insert(
        "audit_logs",
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "action": action,
            "target": target,
            "detail": json.dumps(detail, ensure_ascii=False) if detail is not None else "",
            "created_at": now(),
        },
    )
