"""数据剖析：snapshot / timeseries / inference 三种模式 + 基线管理。

设计要点
--------
* 剖析结果是「漂移检测的参照物」，所以统计口径必须稳定：分位数统一用
  ``quantile_cont``，直方图统一 10 桶且边界固定为 [min, max]，类别 TopN 固定 10 条。
  口径一变，PSI/KS 就不可比，漂移检测会变成噪声发生器。
* 三种模式的差别只在「数据范围」：
  - snapshot：全表一次快照，最准但最贵；
  - timeseries：按时间列分桶，看每桶的行数与数值均值漂移；
  - inference：只采样不落全量，表很大时用，结果标记 ``sampled=True``。
* 列类型判断以 ``warehouse.describe`` 的真实类型为准（目录里的元数据可能过期），
  但剖析只依赖真实类型，不依赖人工维护的字段描述。
* 每列统计一次 SQL 聚合取全，直方图与 TopN 各补一条 —— 单列最多 3 条查询，
  40 列的宽表也不会把本地 DuckDB 打爆。
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from typing import Any

from . import catalog, store, warehouse

MODES = ("snapshot", "timeseries", "inference")

MODE_LABELS = {
    "snapshot": "单次快照",
    "timeseries": "时间分桶",
    "inference": "推理采样",
}

MODE_DESC = {
    "snapshot": "对全表做一次完整统计，结果最准，适合中小表与发布前校验",
    "timeseries": "按时间列分桶统计，观察数据量与数值随时间的漂移",
    "inference": "只抽样不落全量，适合大表与低成本巡检，结果会标记 sampled",
}

DEFAULT_SAMPLE_ROWS = 5000
MAX_COLUMNS = 60
HISTOGRAM_BINS = 10
TOP_N = 10
QUANTILES = (0.25, 0.5, 0.75, 0.9, 0.99)
QUANTILE_KEYS = ("p25", "p50", "p75", "p90", "p99")

_NUMERIC_HINTS = (
    "INT", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT", "UBIGINT", "UINTEGER",
    "USMALLINT", "UTINYINT", "DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC",
)
_DATE_HINTS = ("DATE", "TIMESTAMP", "DATETIME", "TIME")
_BOOL_HINTS = ("BOOL",)
_STRING_HINTS = ("CHAR", "TEXT", "STRING", "UUID", "BLOB", "JSON")


# --------------------------------------------------------------------- 小工具


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _j(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _round(value: Any, digits: int = 6) -> float | None:
    out = _num(value)
    return None if out is None else round(out, digits)


def kind_of(data_type: str) -> str:
    """把 DuckDB 类型名归一成 numeric / date / boolean / string / other。"""
    text = str(data_type or "").upper()
    if any(h in text for h in _NUMERIC_HINTS):
        return "numeric"
    if any(h in text for h in _DATE_HINTS):
        return "date"
    if any(h in text for h in _BOOL_HINTS):
        return "boolean"
    if any(h in text for h in _STRING_HINTS):
        return "string"
    return "other"


def _run(ws: str, sql: str, limit: int = 200) -> list[list]:
    return catalog.run_workspace_sql(ws, sql, limit=limit)["rows"]


def _one(ws: str, sql: str) -> list:
    rows = _run(ws, sql, limit=1)
    return rows[0] if rows else []


def ensure_tables() -> None:
    with store.db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profile_baselines (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                table_ref TEXT NOT NULL,
                column_name TEXT DEFAULT '',
                mode TEXT DEFAULT 'snapshot',
                stats TEXT DEFAULT '{}',
                captured_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_baseline_ref ON profile_baselines(table_ref, column_name);
            """
        )


def _resolve(ws: str, table_ref: str) -> dict:
    found = catalog.resolve(ws, table_ref)
    if not found:
        raise ValueError(f"表 {table_ref} 不存在（请确认已在数据目录中登记）")
    return found


def _describe(found: dict) -> list[dict]:
    """真实列类型：优先 DESCRIBE，失败再退回目录元数据。"""
    try:
        cols = warehouse.describe(found["catalog_name"], found["schema_name"], found["name"])
        if cols:
            return [{"name": c["name"], "data_type": c.get("data_type") or ""} for c in cols]
    except Exception:  # noqa: BLE001 —— 目录元数据兜底
        pass
    return [
        {"name": c["name"], "data_type": c.get("data_type") or ""}
        for c in catalog.columns_of(found["id"])
    ]


def _source_sql(ref: str, sample_rows: int | None) -> str:
    if sample_rows and sample_rows > 0:
        return f"(SELECT * FROM {ref} USING SAMPLE reservoir({int(sample_rows)} ROWS))"
    return ref


def _row_count(ws: str, src: str) -> int:
    row = _one(ws, f"SELECT count(*) FROM {src}")
    return int((row[0] if row else 0) or 0)


# --------------------------------------------------------------------- 列统计


def _numeric_stats(ws: str, src: str, column: str) -> dict:
    q = _q(column)
    # 注意：quantile_cont 传列表会返回 DuckDB LIST，而 warehouse.query 会把非标量
    # 值转成字符串。所以每个分位数单独取一列，保证拿到的是 float。
    qcols = ", ".join(f"quantile_cont({q}, {x})" for x in QUANTILES)
    row = _one(
        ws,
        f"SELECT min({q}), max({q}), avg({q}), stddev_samp({q}), {qcols} "
        f"FROM {src} WHERE {q} IS NOT NULL",
    )
    lo = row[0] if len(row) > 0 else None
    hi = row[1] if len(row) > 1 else None
    mean = row[2] if len(row) > 2 else None
    std = row[3] if len(row) > 3 else None
    quantiles: dict[str, float | None] = {}
    for idx, key in enumerate(QUANTILE_KEYS):
        pos = 4 + idx
        quantiles[key] = _round(row[pos]) if len(row) > pos else None
    out = {
        "min": _round(lo),
        "max": _round(hi),
        "mean": _round(mean),
        "std": _round(std),
        "quantiles": quantiles,
    }
    hist = _histogram(ws, src, column, _num(lo), _num(hi))
    if hist:
        out["histogram"] = hist
    return out


def _histogram(ws: str, src: str, column: str, lo: float | None, hi: float | None) -> dict | None:
    """固定 [min,max] 分 10 桶的等宽直方图，空桶补 0。"""
    q = _q(column)
    if lo is None or hi is None:
        row = _one(ws, f"SELECT min({q}), max({q}) FROM {src} WHERE {q} IS NOT NULL")
        lo = _num(row[0]) if row else None
        hi = _num(row[1]) if len(row) > 1 else None
    if lo is None or hi is None:
        return None
    bins = HISTOGRAM_BINS
    if hi <= lo:
        row = _one(ws, f"SELECT count(*) FROM {src} WHERE {q} IS NOT NULL")
        total = int((row[0] if row else 0) or 0)
        return {
            "bins": [{"lo": _round(lo), "hi": _round(hi), "count": total}],
            "min": _round(lo),
            "max": _round(hi),
            "width": 0,
            "total": total,
        }
    width = (hi - lo) / bins
    rows = _run(
        ws,
        f"SELECT CAST(floor(({q} - {lo}) / {width}) AS INTEGER) AS b, count(*) "
        f"FROM {src} WHERE {q} IS NOT NULL GROUP BY 1 ORDER BY 1",
        limit=bins + 5,
    )
    counts = [0] * bins
    for bucket, count in rows:
        try:
            idx = int(bucket)
        except (TypeError, ValueError):
            continue
        idx = max(0, min(bins - 1, idx))
        counts[idx] += int(count or 0)
    return {
        "bins": [
            {"lo": _round(lo + width * i), "hi": _round(lo + width * (i + 1)), "count": counts[i]}
            for i in range(bins)
        ],
        "min": _round(lo),
        "max": _round(hi),
        "width": _round(width),
        "total": sum(counts),
    }


def _top_values(ws: str, src: str, column: str, limit: int = TOP_N) -> list[dict]:
    q = _q(column)
    rows = _run(
        ws,
        f"SELECT CAST({q} AS VARCHAR) AS v, count(*) AS n FROM {src} "
        f"WHERE {q} IS NOT NULL GROUP BY 1 ORDER BY n DESC, v LIMIT {int(limit)}",
        limit=limit,
    )
    total = sum(int(r[1] or 0) for r in rows)
    out = []
    for value, count in rows:
        count = int(count or 0)
        out.append(
            {
                "value": value,
                "count": count,
                "ratio": round(count / total, 6) if total else None,
            }
        )
    return out


def profile_column(
    ws: str, src: str, column: str, data_type: str = "", total_rows: int = 0
) -> dict:
    """单列统计。任何异常都收敛成 error 字段，不让一列拖垮整表剖析。"""
    q = _q(column)
    kind = kind_of(data_type)
    out: dict = {
        "name": column,
        "data_type": data_type,
        "kind": kind,
        "count": 0,
        "null_count": 0,
        "null_rate": None,
        "distinct": None,
        "distinct_rate": None,
    }
    try:
        row = _one(
            ws,
            f"SELECT count(*) AS total, count({q}) AS filled, "
            f"count(DISTINCT {q}) AS distinct_cnt FROM {src}",
        )
        total = int((row[0] if row else 0) or 0)
        filled = int((row[1] if len(row) > 1 else 0) or 0)
        distinct = int((row[2] if len(row) > 2 else 0) or 0)
        out.update(
            count=total,
            null_count=total - filled,
            null_rate=round((total - filled) / total, 6) if total else None,
            distinct=distinct,
            distinct_rate=round(distinct / filled, 6) if filled else None,
        )
        if kind == "numeric":
            out.update(_numeric_stats(ws, src, column))
        elif kind == "date":
            drow = _one(ws, f"SELECT min({q}), max({q}) FROM {src} WHERE {q} IS NOT NULL")
            out["min"] = _j(drow[0]) if drow else None
            out["max"] = _j(drow[1]) if len(drow) > 1 else None
        elif kind == "boolean":
            out["top_values"] = _top_values(ws, src, column, 5)
        else:
            out["top_values"] = _top_values(ws, src, column)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# --------------------------------------------------------------------- 时间分桶


def detect_time_column(columns: list[dict]) -> str | None:
    """挑一个最像时间轴的列：先看类型，再按命名优先级排序。"""
    candidates = [c for c in columns if kind_of(c.get("data_type")) == "date"]
    if not candidates:
        return None
    priority = ("date", "dt", "day", "order_date", "created_at", "updated_at", "ts", "time", "timestamp")
    def score(col: dict) -> tuple:
        name = str(col.get("name") or "").lower()
        for idx, key in enumerate(priority):
            if name == key:
                return (0, idx)
        for idx, key in enumerate(priority):
            if key in name:
                return (1, idx)
        return (2, 0)
    return sorted(candidates, key=score)[0]["name"]


def _pick_granularity(ws: str, src: str, time_column: str) -> str:
    row = _one(
        ws,
        f"SELECT date_diff('day', min({_q(time_column)}), max({_q(time_column)})) "
        f"FROM {src} WHERE {_q(time_column)} IS NOT NULL",
    )
    span = int((row[0] if row else 0) or 0)
    if span <= 45:
        return "day"
    if span <= 400:
        return "week"
    if span <= 1460:
        return "month"
    return "year"


def _timeseries(
    ws: str,
    src: str,
    columns: list[dict],
    time_column: str,
    granularity: str,
    numeric_columns: list[str],
    limit: int = 400,
) -> list[dict]:
    q = _q(time_column)
    aggs = ["count(*) AS total"]
    for idx, column in enumerate(numeric_columns[:6]):
        aggs.append(f"avg({_q(column)}) AS avg_{idx}")
        aggs.append(f"min({_q(column)}) AS min_{idx}")
        aggs.append(f"max({_q(column)}) AS max_{idx}")
    rows = _run(
        ws,
        f"SELECT date_trunc('{granularity}', {q}) AS bucket, {', '.join(aggs)} "
        f"FROM {src} WHERE {q} IS NOT NULL GROUP BY 1 ORDER BY 1",
        limit=limit,
    )
    out: list[dict] = []
    for row in rows:
        bucket = _j(row[0])
        item: dict = {"bucket": bucket, "total": int(row[1] or 0), "columns": {}}
        cursor = 2
        for column in numeric_columns[:6]:
            item["columns"][column] = {
                "avg": _round(row[cursor]),
                "min": _round(row[cursor + 1]),
                "max": _round(row[cursor + 2]),
            }
            cursor += 3
        out.append(item)
    return out


# --------------------------------------------------------------------- 主入口


def profile_table(
    table_ref: str,
    mode: str = "snapshot",
    workspace_id: str | None = None,
    time_column: str | None = None,
    granularity: str | None = None,
    sample_rows: int | None = None,
    max_columns: int = MAX_COLUMNS,
    save: bool = False,
) -> dict:
    """剖析一张表。

    返回结构：{table_ref, mode, row_count, columns:[...], series:[...], warnings:[]}
    mode 不合法时抛 ValueError；其它问题写进 warnings，不中断。
    """
    ensure_tables()
    mode = str(mode or "snapshot").lower()
    if mode not in MODES:
        raise ValueError(f"不支持的剖析模式：{mode}（可选：{'/'.join(MODES)}）")
    started = time.perf_counter()
    ws = workspace_id or _workspace_of(table_ref)
    found = _resolve(ws, table_ref)
    ref = found["ref"]
    columns = _describe(found)[: max(1, max_columns)]
    warnings: list[str] = []

    sampled = mode == "inference"
    effective_sample = int(sample_rows or DEFAULT_SAMPLE_ROWS) if sampled else None
    src = _source_sql(ref, effective_sample)
    total_rows = _row_count(ws, src)
    if sampled and effective_sample and total_rows < effective_sample:
        warnings.append(
            f"采样行数 {effective_sample} 大于实际行数 {total_rows}，本次等同全量"
        )

    profiled = [profile_column(ws, src, c["name"], c.get("data_type"), total_rows) for c in columns]
    numeric_columns = [c["name"] for c in profiled if c["kind"] == "numeric"]

    result: dict = {
        "table_ref": ref,
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "sampled": sampled,
        "sample_rows": effective_sample if sampled else None,
        "row_count": total_rows,
        "column_count": len(profiled),
        "columns": profiled,
        "numeric_columns": numeric_columns,
        "warnings": warnings,
        "captured_at": store.now(),
    }

    if mode == "timeseries":
        chosen = time_column or detect_time_column(columns)
        if not chosen:
            warnings.append("未找到可用的时间列，已自动降级为 snapshot 全量统计")
            result["degraded"] = True
        elif chosen not in {c["name"] for c in columns}:
            warnings.append(f"时间列 {chosen} 不在剖析列范围内，已自动降级为 snapshot")
            result["degraded"] = True
        else:
            gran = granularity or _pick_granularity(ws, src, chosen)
            if gran not in ("day", "week", "month", "year", "hour"):
                warnings.append(f"不支持的分桶粒度 {gran}，改用 day")
                gran = "day"
            series = _timeseries(ws, src, columns, chosen, gran, numeric_columns)
            result.update(
                time_column=chosen,
                granularity=gran,
                series=series,
                bucket_count=len(series),
            )
            if not series:
                warnings.append("时间列没有可用数据，分桶结果为空")
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)

    if save:
        result["baseline"] = save_baseline(
            ref, "", mode, result, workspace_id=ws, captured_at=result["captured_at"]
        )
    return result


def _workspace_of(table_ref: str) -> str:
    hit = store.row("SELECT workspace_id FROM quality_rules WHERE table_ref = ? LIMIT 1", (table_ref,))
    if hit and hit.get("workspace_id"):
        return hit["workspace_id"]
    hit = store.row("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
    return hit["id"] if hit else ""


# --------------------------------------------------------------------- 基线


def _compact(profile: dict) -> dict:
    """基线只存漂移检测真正需要的部分，避免 stats 膨胀。"""
    columns = {}
    for col in profile.get("columns") or []:
        entry: dict = {
            "data_type": col.get("data_type"),
            "kind": col.get("kind"),
            "null_rate": col.get("null_rate"),
            "distinct": col.get("distinct"),
            "count": col.get("count"),
        }
        for key in ("min", "max", "mean", "std"):
            if key in col:
                entry[key] = col.get(key)
        if col.get("quantiles"):
            entry["quantiles"] = col["quantiles"]
        if col.get("histogram"):
            entry["histogram"] = {
                "min": col["histogram"].get("min"),
                "max": col["histogram"].get("max"),
                "width": col["histogram"].get("width"),
                "counts": [b.get("count") for b in col["histogram"].get("bins") or []],
            }
        if col.get("top_values"):
            entry["top_values"] = col["top_values"]
        columns[col.get("name")] = entry
    return {
        "table_ref": profile.get("table_ref"),
        "mode": profile.get("mode"),
        "row_count": profile.get("row_count"),
        "column_count": profile.get("column_count"),
        "sampled": profile.get("sampled"),
        "columns": columns,
    }


def save_baseline(
    table_ref: str,
    column_name: str = "",
    mode: str = "snapshot",
    stats: dict | None = None,
    workspace_id: str | None = None,
    captured_at: str | None = None,
) -> dict:
    """落一条基线。stats 可以是完整剖析结果，也可以已经是精简结构。"""
    ensure_tables()
    ws = workspace_id or _workspace_of(table_ref)
    payload = stats if stats is not None else {}
    compact = payload if payload.get("columns") and isinstance(payload.get("columns"), dict) else _compact(payload)
    if payload.get("series"):
        compact["series"] = [
            {"bucket": s.get("bucket"), "total": s.get("total")} for s in payload["series"]
        ]
    row = {
        "id": store.new_id(),
        "workspace_id": ws,
        "table_ref": table_ref,
        "column_name": column_name or "",
        "mode": mode,
        "stats": json.dumps(compact, ensure_ascii=False, default=str),
        "captured_at": captured_at or store.now(),
    }
    store.insert("profile_baselines", row)
    return {**row, "stats": compact}


def list_baselines(table_ref: str = "", workspace_id: str | None = None, limit: int = 50) -> list[dict]:
    ensure_tables()
    sql = "SELECT * FROM profile_baselines WHERE 1=1"
    params: list[Any] = []
    if workspace_id:
        sql += " AND workspace_id = ?"
        params.append(workspace_id)
    if table_ref:
        sql += " AND table_ref = ?"
        params.append(table_ref)
    sql += " ORDER BY captured_at DESC LIMIT ?"
    params.append(limit)
    rows = store.rows(sql, params)
    for row in rows:
        row["stats"] = _load_json(row.get("stats"))
    return rows


def get_baseline(
    table_ref: str,
    column_name: str = "",
    baseline_id: str | None = None,
    mode: str | None = None,
    workspace_id: str | None = None,
) -> dict | None:
    """取基线：显式 id > 该表最近一条（可按 mode 过滤）。"""
    ensure_tables()
    if baseline_id:
        row = store.row("SELECT * FROM profile_baselines WHERE id = ?", (baseline_id,))
    else:
        sql = "SELECT * FROM profile_baselines WHERE table_ref = ?"
        params: list[Any] = [table_ref]
        if column_name:
            sql += " AND IFNULL(column_name, '') = ?"
            params.append(column_name)
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        if workspace_id:
            sql += " AND workspace_id = ?"
            params.append(workspace_id)
        sql += " ORDER BY captured_at DESC LIMIT 1"
        row = store.row(sql, params)
    if not row:
        return None
    row["stats"] = _load_json(row.get("stats"))
    return row


def latest_stats(table_ref: str, workspace_id: str | None = None) -> dict | None:
    """便捷方法：拿最近一条基线的 stats。"""
    row = get_baseline(table_ref, workspace_id=workspace_id)
    return (row or {}).get("stats") if row else None


def delete_baseline(baseline_id: str, workspace_id: str | None = None) -> bool:
    ensure_tables()
    row = store.row("SELECT id FROM profile_baselines WHERE id = ?", (baseline_id,))
    if not row:
        return False
    if workspace_id:
        owner = store.row("SELECT workspace_id FROM profile_baselines WHERE id = ?", (baseline_id,))
        if owner and owner.get("workspace_id") not in (None, "", workspace_id):
            return False
    store.execute("DELETE FROM profile_baselines WHERE id = ?", (baseline_id,))
    return True


def _load_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def describe_modes() -> list[dict]:
    return [
        {"mode": mode, "label": MODE_LABELS[mode], "desc": MODE_DESC[mode]} for mode in MODES
    ]
