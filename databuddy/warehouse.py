"""湖仓引擎：本地 DuckDB 承载 Catalog.Schema.Table 三级命名空间。

设计要点
--------
* 原产品的 Catalog 由云上 TCLake/TCCatalog 托管，这里落成本地 DuckDB 的 schema：
  逻辑名 `catalog.schema.table` 物理映射为 `"catalog__schema"."table"`。
* 所有面向用户的 SQL 都要先过 `guard()`：只允许只读查询、单语句、禁文件读写函数，
  防止 NL2SQL 生成的语句把本地数据删了或读到任意路径。
* 同一把锁串行化执行，避免 DuckDB 单连接并发问题（本地单机场景足够）。
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from . import config

_lock = threading.RLock()
_conn: duckdb.DuckDBPyConnection | None = None

ALLOWED_STARTS = ("select", "with", "describe", "desc", "show", "summarize", "explain", "values", "pivot")
DENY_KEYWORDS = (
    "attach", "detach", "install", "load", "copy", "export", "import", "pragma",
    "call", "create", "insert", "update", "delete", "drop", "alter", "truncate",
    "grant", "revoke", "vacuum", "checkpoint", "set", "reset", "use", "begin",
    "commit", "rollback", "threads", "force",
)
DENY_FUNCS = (
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "parquet_scan", "csv_scan", "sniff_csv", "read_text", "read_blob", "glob(",
    "getenv", "shell", "system",
)


class SqlRejected(ValueError):
    """被安全护栏拒绝的 SQL。"""


def connect() -> duckdb.DuckDBPyConnection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_dirs()
            _conn = duckdb.connect(str(config.WAREHOUSE_PATH))
            _conn.execute("SET TimeZone='Asia/Shanghai'")
        return _conn


def physical_name(catalog: str, schema: str) -> str:
    return f"{catalog}__{schema}"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ensure_namespace(catalog: str, schema: str) -> None:
    with _lock:
        conn = connect()
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(physical_name(catalog, schema))}")


def drop_namespace(catalog: str, schema: str) -> None:
    with _lock:
        conn = connect()
        conn.execute(f"DROP SCHEMA IF EXISTS {_q(physical_name(catalog, schema))} CASCADE")


def drop_table(catalog: str, schema: str, table: str) -> None:
    with _lock:
        conn = connect()
        conn.execute(f"DROP TABLE IF EXISTS {_q(physical_name(catalog, schema))}.{_q(table)}")


def rename_table(catalog: str, schema: str, old: str, new: str) -> None:
    with _lock:
        conn = connect()
        conn.execute(
            f"ALTER TABLE {_q(physical_name(catalog, schema))}.{_q(old)} RENAME TO {_q(new)}"
        )


def guard(sql: str) -> str:
    """校验并返回清洗后的单条只读 SQL。"""
    text = (sql or "").strip()
    if not text:
        raise SqlRejected("SQL 为空")
    # 去掉结尾分号后仍含分号 => 多语句
    stripped = text.rstrip(";").strip()
    if ";" in stripped:
        raise SqlRejected("一次只允许执行一条语句")
    if "--" in stripped or "/*" in stripped:
        raise SqlRejected("暂不支持 SQL 注释，请去掉注释后重试")

    lowered = stripped.lower()
    first = re.split(r"\s+", lowered, maxsplit=1)[0].strip("(")
    if first not in ALLOWED_STARTS:
        raise SqlRejected(f"只允许只读查询（{first.upper()} 不被允许）")

    for kw in DENY_KEYWORDS:
        if re.search(rf"(?<![\w.]){re.escape(kw)}(?![\w.])", lowered):
            raise SqlRejected(f"检测到受限关键字：{kw.upper()}")
    for fn in DENY_FUNCS:
        if fn in lowered:
            raise SqlRejected(f"检测到受限函数：{fn.rstrip('(')}")
    if "from '" in lowered or 'from "' in lowered:
        raise SqlRejected("不支持从文件路径直接读取，请先通过数据接入建表")
    return stripped


def _qualified_refs(mapping: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """mapping: [(catalog, schema)] -> [(logical_prefix, physical_prefix)]"""
    return [
        (
            rf'(?<![\w"])"?{re.escape(cat)}"?\s*\.\s*"?{re.escape(sch)}"?\s*\.',
            f'{_q(physical_name(cat, sch))}.',
        )
        for cat, sch in mapping
    ]


def qualify(sql: str, mapping: list[tuple[str, str]], tables: dict[str, str] | None = None) -> str:
    """把 catalog.schema.table 与裸表名改写成物理位置。"""
    out = sql
    for pattern, repl in _qualified_refs(mapping):
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    if tables:
        for name, target in tables.items():
            out = re.sub(
                rf'(?i)\b(from|join|into|update|table)\s+"?{re.escape(name)}"?(?![\w.])',
                lambda m, t=target: f"{m.group(1)} {t}",
                out,
            )
    return out


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<binary {len(value)} bytes>"
    return str(value)


def query(
    sql: str,
    mapping: list[tuple[str, str]] | None = None,
    tables: dict[str, str] | None = None,
    limit: int = 500,
    check: bool = True,
) -> dict:
    """执行只读查询，返回列 + 行 + 耗时。"""
    started = time.perf_counter()
    prepared = guard(sql) if check else sql.strip().rstrip(";")
    if mapping:
        prepared = qualify(prepared, mapping, tables)
    with _lock:
        conn = connect()
        cur = conn.execute(prepared)
        columns = [d[0] for d in cur.description] if cur.description else []
        types = [str(d[1]) for d in cur.description] if cur.description else []
        fetched = cur.fetchmany(limit + 1)
    truncated = len(fetched) > limit
    rows = [[_jsonable(v) for v in r] for r in fetched[:limit]]
    return {
        "columns": columns,
        "types": types,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "sql": prepared,
    }


def execute_ddl(sql: str) -> None:
    with _lock:
        connect().execute(sql)


def describe(catalog: str, schema: str, table: str) -> list[dict]:
    phys = physical_name(catalog, schema)
    with _lock:
        rows = connect().execute(f"DESCRIBE {_q(phys)}.{_q(table)}").fetchall()
    out = []
    for i, r in enumerate(rows):
        out.append(
            {
                "name": r[0],
                "data_type": r[1],
                "is_nullable": bool(r[2]) if len(r) > 2 else True,
                "ordinal": i,
            }
        )
    return out


def count_rows(catalog: str, schema: str, table: str) -> int:
    phys = physical_name(catalog, schema)
    try:
        with _lock:
            r = connect().execute(f"SELECT count(*) FROM {_q(phys)}.{_q(table)}").fetchone()
        return int(r[0]) if r else 0
    except Exception:
        return 0


def create_from_file(catalog: str, schema: str, table: str, path: str) -> int:
    """从 CSV / JSON / Parquet 直接建表，返回行数。"""
    phys = physical_name(catalog, schema)
    ensure_namespace(catalog, schema)
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        reader = f"read_csv_auto('{_sql_str(path)}', header=true, sample_size=20000)"
    elif suffix in (".json", ".ndjson", ".jsonl"):
        reader = f"read_json_auto('{_sql_str(path)}')"
    elif suffix in (".parquet", ".pq"):
        reader = f"read_parquet('{_sql_str(path)}')"
    else:
        raise ValueError(f"不支持的文件类型：{suffix}")
    target = f"{_q(phys)}.{_q(table)}"
    with _lock:
        conn = connect()
        conn.execute(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {reader}")
    return count_rows(catalog, schema, table)


def create_from_rows(
    catalog: str, schema: str, table: str, columns: list[tuple[str, str]], rows: list[list[Any]]
) -> int:
    """按显式列类型建表并写入（用于 XLSX / 手工数据）。"""
    phys = physical_name(catalog, schema)
    ensure_namespace(catalog, schema)
    cols_ddl = ", ".join(f"{_q(name)} {dtype}" for name, dtype in columns)
    target = f"{_q(phys)}.{_q(table)}"
    with _lock:
        conn = connect()
        conn.execute(f"CREATE OR REPLACE TABLE {target} ({cols_ddl})")
        if rows:
            marks = ", ".join("?" for _ in columns)
            conn.executemany(f"INSERT INTO {target} VALUES ({marks})", rows)
    return count_rows(catalog, schema, table)


def preview(catalog: str, schema: str, table: str, limit: int = 20) -> dict:
    phys = physical_name(catalog, schema)
    return query(f"SELECT * FROM {_q(phys)}.{_q(table)}", limit=limit, check=False)


def _sql_str(value: str) -> str:
    return value.replace("'", "''")


def sample_column(catalog: str, schema: str, table: str, column: str, limit: int = 200) -> list[Any]:
    phys = physical_name(catalog, schema)
    try:
        with _lock:
            fetched = connect().execute(
                f"SELECT {_q(column)} FROM {_q(phys)}.{_q(table)} "
                f"WHERE {_q(column)} IS NOT NULL LIMIT {int(limit)}"
            ).fetchall()
        return [r[0] for r in fetched]
    except Exception:
        return []
