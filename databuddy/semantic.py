"""语义层：指标（度量）与维度注册，以及「指标 + 维度 + 时间」到 SQL 的确定性编译。

为什么不让模型直接写投影 SQL 就跑？因为没有口径约束时同一个问题两次问出两个数。
语义层把口径收敛到一处：模型只负责选指标、选维度、选时间范围。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from . import catalog, store, warehouse

BASE_ALIAS = "f"


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def list_metrics(workspace_id: str) -> list[dict]:
    rows = store.rows(
        "SELECT * FROM metrics WHERE workspace_id = ? ORDER BY name", (workspace_id,)
    )
    for r in rows:
        r["dimensions"] = _loads(r.get("dimensions"), [])
        r["joins"] = _loads(r.get("joins"), [])
    return rows


def find_metric(workspace_id: str, name: str) -> dict | None:
    rows = list_metrics(workspace_id)
    target = (name or "").strip().lower()
    for r in rows:
        if r["name"].lower() == target or (r.get("display_name") or "").lower() == target:
            return r
    for r in rows:
        if target and (target in r["name"].lower() or target in (r.get("display_name") or "")):
            return r
    return None


def create_metric(workspace_id: str, payload: dict) -> dict:
    name = re.sub(r"[^\w\u4e00-\u9fa5]", "_", (payload.get("name") or "").strip()).lower()
    if not name:
        raise ValueError("指标名不能为空")
    if store.row("SELECT id FROM metrics WHERE workspace_id = ? AND name = ?", (workspace_id, name)):
        raise ValueError(f"指标 {name} 已存在")
    expr = (payload.get("expr") or "").strip()
    table_ref = (payload.get("table_ref") or "").strip()
    if not expr or not table_ref:
        raise ValueError("expr 与 table_ref 必填")
    found = catalog.resolve(workspace_id, table_ref)
    if not found:
        raise ValueError(f"表 {table_ref} 不存在")

    # 校验表达式可执行（LIMIT 0 不产生扫描开销）
    validation_sql = f"SELECT {expr} AS v FROM {found['ref']} LIMIT 0"
    try:
        catalog.run_workspace_sql(workspace_id, validation_sql, limit=1)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"表达式无法执行：{exc}") from exc

    dimensions = payload.get("dimensions") or []
    if isinstance(dimensions, str):
        dimensions = [d.strip() for d in dimensions.split(",") if d.strip()]
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "name": name,
        "display_name": payload.get("display_name") or name,
        "description": payload.get("description") or "",
        "expr": expr,
        "table_ref": found["ref"],
        "filters": payload.get("filters") or "",
        "unit": payload.get("unit") or "",
        "dimensions": json.dumps(dimensions, ensure_ascii=False),
        "joins": json.dumps(payload.get("joins") or [], ensure_ascii=False),
        "owner": payload.get("owner") or "",
        "status": payload.get("status") or "published",
        "created_at": store.now(),
    }
    store.insert("metrics", row)
    return row


def _join_clause(metric: dict) -> tuple[str, dict[str, str]]:
    """返回 JOIN 子句与「维度名 -> 限定列」映射。"""
    joins = metric.get("joins") or []
    clauses: list[str] = []
    alias_map: dict[str, str] = {}
    base_ref = metric["table_ref"]
    for j in joins:
        table = j.get("table")
        alias = j.get("alias") or f"j{len(clauses)}"
        on = j.get("on")
        if not table or not on:
            continue
        clauses.append(f"LEFT JOIN {table} {alias} ON {on}")
        for col in j.get("columns") or []:
            alias_map[col.lower()] = f"{alias}.{col}"
    return " ".join(clauses), alias_map


def resolve_dimension(metric: dict, name: str, alias_map: dict[str, str]) -> str:
    key = name.lower()
    if key in alias_map:
        return alias_map[key]
    return f"{BASE_ALIAS}.{name}"


def build_metric_sql(
    metric: dict,
    dimensions: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    granularity: str | None = None,
    limit: int = 500,
    order: str = "auto",
) -> dict:
    dims = dimensions or []
    join_sql, alias_map = _join_clause(metric)
    select_parts: list[str] = []
    group_parts: list[str] = []
    label_map: dict[str, str] = {}

    for d in dims:
        if d == "__date__" and granularity:
            unit = {"day": "day", "week": "week", "month": "month", "quarter": "quarter"}.get(
                granularity, "day"
            )
            col = resolve_dimension(metric, "order_date", alias_map)
            expr = f"date_trunc('{unit}', {col})"
            alias = {"day": "日期", "week": "周", "month": "月份", "quarter": "季度"}[unit]
            select_parts.append(f"CAST({expr} AS DATE) AS \"{alias}\"")
            group_parts.append(expr)
            label_map["__date__"] = alias
            continue
        col = resolve_dimension(metric, d, alias_map)
        select_parts.append(f'{col} AS "{d}"')
        group_parts.append(col)
        label_map[d] = d

    select_parts.append(f'{metric["expr"]} AS "{metric.get("display_name") or metric["name"]}"')
    date_col = resolve_dimension(metric, "order_date", alias_map)
    where: list[str] = []
    if metric.get("filters"):
        where.append(f"({metric['filters']})")
    if start:
        where.append(f"{date_col} >= DATE '{start}'")
    if end:
        where.append(f"{date_col} <= DATE '{end}'")

    sql = f"SELECT {', '.join(select_parts)} FROM {metric['table_ref']} {BASE_ALIAS} {join_sql}".strip()
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group_parts:
        sql += " GROUP BY " + ", ".join(group_parts)
    if order == "time":
        sql += " ORDER BY 1 ASC"
    elif dims:
        sql += f' ORDER BY "{metric.get("display_name") or metric["name"]}" DESC'
    sql += f" LIMIT {int(limit)}"
    return {"sql": sql, "labels": label_map}


def parse_time_range(text: str) -> tuple[str | None, str | None, str | None]:
    """从中文问句里粗略识别时间范围与粒度。返回 (start, end, granularity)。"""
    today = date.today()
    t = (text or "").lower()
    if re.search(r"近\s*7\s*天|最近\s*7\s*天|过去\s*7\s*天|一周", t):
        return (today - timedelta(days=6)).isoformat(), today.isoformat(), "day"
    if re.search(r"近\s*30\s*天|最近\s*30\s*天|过去\s*30\s*天|一个月", t):
        return (today - timedelta(days=29)).isoformat(), today.isoformat(), "day"
    if re.search(r"近\s*90\s*天|最近\s*90\s*天|三个月|一季度", t):
        return (today - timedelta(days=89)).isoformat(), today.isoformat(), "day"
    if re.search(r"昨天", t):
        d = (today - timedelta(days=1)).isoformat()
        return d, d, "day"
    if re.search(r"今天|当日", t):
        d = today.isoformat()
        return d, d, "day"
    if re.search(r"本月|这个月", t):
        return today.replace(day=1).isoformat(), today.isoformat(), "day"
    if re.search(r"上月|上个月", t):
        first = today.replace(day=1)
        last_month_end = first - timedelta(days=1)
        return last_month_end.replace(day=1).isoformat(), last_month_end.isoformat(), "day"
    if re.search(r"今年", t):
        return today.replace(month=1, day=1).isoformat(), today.isoformat(), "month"
    return None, None, None


def metric_prompt_block(metrics: list[dict]) -> str:
    lines = []
    for m in metrics:
        dims = "、".join(m.get("dimensions") or []) or "无"
        lines.append(
            f'- {m["name"]}（{m.get("display_name")}）: {m.get("description") or ""} '
            f'| 单位 {m.get("unit") or "-"} | 可用维度: {dims} | 表: {m["table_ref"]}'
        )
    return "\n".join(lines)
