"""结果集 → 可视化推断 + 看板卡片执行。"""
from __future__ import annotations

import re

from . import catalog

DATE_HINTS = ("date", "日期", "time", "时间", "month", "月份", "day", "dt", "week", "周")


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_date_like(values: list) -> bool:
    if not values:
        return False
    sample = [v for v in values[:20] if v is not None]
    if not sample:
        return False
    return all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}(-\d{2})?([ T].*)?", v) for v in sample)


def infer(result: dict) -> dict:
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    if not cols or not rows:
        return {"type": "empty"}

    numeric_idx = [
        i for i in range(len(cols))
        if any(_is_number(r[i]) for r in rows[:20] if i < len(r))
    ]

    if len(rows) == 1 and len(cols) <= 3 and numeric_idx:
        return {
            "type": "kpi",
            "value_index": numeric_idx[0],
            "label": cols[numeric_idx[0]],
        }

    if len(cols) < 2 or not numeric_idx:
        return {"type": "table"}

    x_idx = 0 if 0 not in numeric_idx else (1 if len(numeric_idx) > 1 else 0)
    y_idx = next((i for i in numeric_idx if i != x_idx), numeric_idx[0])
    x_values = [r[x_idx] for r in rows]

    if _is_date_like(x_values) or any(h in str(cols[x_idx]).lower() for h in DATE_HINTS):
        return {"type": "line", "x": x_idx, "y": y_idx, "x_label": cols[x_idx], "y_label": cols[y_idx]}

    if len(numeric_idx) >= 2 and len(cols) >= 3:
        return {
            "type": "bar",
            "x": x_idx,
            "y": y_idx,
            "series": [i for i in numeric_idx if i not in (x_idx,)][:3],
            "x_label": cols[x_idx],
            "y_label": cols[y_idx],
        }

    if 2 <= len(rows) <= 8:
        return {"type": "pie", "x": x_idx, "y": y_idx, "x_label": cols[x_idx], "y_label": cols[y_idx]}
    return {"type": "bar", "x": x_idx, "y": y_idx, "x_label": cols[x_idx], "y_label": cols[y_idx]}


def build_card(workspace_id: str, title: str, sql: str, chart: dict | None = None) -> dict:
    result = catalog.run_workspace_sql(workspace_id, sql, limit=200)
    spec = chart or infer(result)
    return {
        "title": title,
        "sql": result["sql"],
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "chart": spec,
        "elapsed_ms": result["elapsed_ms"],
    }


def render_dashboard(workspace_id: str, spec: dict) -> list[dict]:
    cards = []
    for card in (spec or {}).get("cards", []):
        try:
            cards.append(
                build_card(workspace_id, card.get("title") or "未命名卡片", card["sql"], card.get("chart"))
            )
        except Exception as exc:  # noqa: BLE001
            cards.append({"title": card.get("title") or "卡片", "error": str(exc), "sql": card.get("sql")})
    return cards
