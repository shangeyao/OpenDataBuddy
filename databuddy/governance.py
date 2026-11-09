"""数据治理：敏感字段识别、分级打标、质量规则与资产盘点。

识别策略是「列名先验 + 数据抽样正则」双证据，输出置信度与依据，
不做不可解释的黑盒判定 —— 治理结论要能被人复核。
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import catalog, store, warehouse

LEVELS = {"L1": "公开", "L2": "内部", "L3": "敏感", "L4": "机密"}

NAME_RULES: list[tuple[str, str, str, float]] = [
    (r"(^|_)(phone|mobile|tel)(_|$)|手机|电话", "手机号", "L3", 0.9),
    (r"(^|_)(email|mail)(_|$)|邮箱", "邮箱", "L3", 0.9),
    (r"id_?card|identity|身份证", "身份证号", "L3", 0.95),
    (r"bank_?card|card_?no|银行卡", "银行卡号", "L4", 0.9),
    (r"(^|_)(name|full_name)(_|$)|姓名", "姓名", "L2", 0.7),
    (r"address|addr|地址", "地址", "L2", 0.7),
    (r"(^|_)(salary|income|wage)(_|$)|薪资|收入", "薪资", "L4", 0.8),
    (r"password|passwd|secret|token", "凭据", "L4", 0.9),
    (r"ip_?addr|ip$", "IP 地址", "L2", 0.6),
]

VALUE_PATTERNS: list[tuple[str, str, str, float]] = [
    (r"^1[3-9]\d{9}$", "手机号", "L3", 1.0),
    (r"^[\w.+-]+@[\w-]+\.[\w.]+$", "邮箱", "L3", 1.0),
    (r"^\d{17}[\dXx]$", "身份证号", "L3", 1.0),
    (r"^\d{16,19}$", "银行卡号", "L4", 0.85),
]


def mask_value(value: Any, tag: str) -> Any:
    text = "" if value is None else str(value)
    if tag in ("手机号", "phone"):
        return re.sub(r"^(\d{3})\d{4}(\d{4})$", r"\1****\2", text) or text
    if tag in ("身份证号", "id_card"):
        return re.sub(r"^(.{6}).*(.{4})$", r"\1********\2", text) or text
    if tag in ("邮箱", "email"):
        name, _, domain = text.partition("@")
        return (name[:2] + "***@" + domain) if domain else text
    if tag in ("银行卡号", "bank_card"):
        return re.sub(r"^(\d{4})\d+(\d{4})$", r"\1********\2", text) or text
    if tag in ("姓名", "name"):
        return text[:1] + "*" * max(len(text) - 1, 1)
    return "***"


def scan_table(workspace_id: str, table_ref: str, sample_size: int = 200) -> dict:
    detail = catalog.table_detail(workspace_id, table_ref)
    if not detail:
        raise ValueError(f"表 {table_ref} 不存在")
    findings: list[dict] = []
    for col in detail["columns"]:
        name = col["name"]
        hit_tag, hit_level, confidence, evidence = "", "", 0.0, ""
        for pattern, tag, level, conf in NAME_RULES:
            if re.search(pattern, name, re.IGNORECASE):
                hit_tag, hit_level, confidence, evidence = tag, level, conf, f"列名匹配 /{pattern}/"
                break

        values = [
            v for v in warehouse.sample_column(
                detail["catalog_name"], detail["schema_name"], detail["name"], name, sample_size
            )
            if v not in (None, "")
        ]
        if values:
            for pattern, tag, level, conf in VALUE_PATTERNS:
                matches = sum(1 for v in values[:100] if re.match(pattern, str(v).strip()))
                ratio = matches / max(len(values[:100]), 1)
                if ratio >= 0.6:
                    if conf >= confidence:
                        hit_tag, hit_level, confidence = tag, level, round(conf * ratio, 2)
                        evidence = f"{matches}/{len(values[:100])} 抽样值命中 {tag} 模式"
                    break

        if hit_tag:
            findings.append(
                {
                    "column": name,
                    "data_type": col["data_type"],
                    "tag": hit_tag,
                    "level": hit_level,
                    "level_name": LEVELS.get(hit_level, ""),
                    "confidence": confidence,
                    "evidence": evidence,
                    "sample": mask_value(values[0], hit_tag) if values else "",
                    "already_tagged": any(t["tag"] == hit_tag for t in col.get("tags", [])),
                }
            )
    return {
        "table": detail["ref"],
        "row_count": detail["row_count"],
        "columns": len(detail["columns"]),
        "findings": sorted(findings, key=lambda f: -f["confidence"]),
    }


def apply_tags(workspace_id: str, table_ref: str, findings: list[dict]) -> int:
    detail = catalog.table_detail(workspace_id, table_ref)
    if not detail:
        raise ValueError(f"表 {table_ref} 不存在")
    col_ids = {c["name"]: c["id"] for c in detail["columns"]}
    applied = 0
    for f in findings:
        cid = col_ids.get(f.get("column"))
        if not cid:
            continue
        exists = store.row(
            "SELECT id FROM column_tags WHERE column_id = ? AND tag = ?", (cid, f["tag"])
        )
        if exists:
            continue
        store.insert(
            "column_tags",
            {
                "id": store.new_id(),
                "column_id": cid,
                "tag": f["tag"],
                "level": f.get("level") or "L2",
                "source": f.get("source") or "ai",
                "confidence": f.get("confidence"),
                "evidence": f.get("evidence") or "",
                "created_at": store.now(),
            },
        )
        applied += 1
    return applied


def policy_suggestions(workspace_id: str) -> list[dict]:
    """基于已打标字段，给出脱敏与访问策略建议。"""
    rows = store.rows(
        """
        SELECT c.name AS column_name, t.name AS table_name, ct.tag, ct.level, ct.confidence,
               s.name AS schema_name, ca.name AS catalog_name
        FROM column_tags ct
        JOIN columns c ON c.id = ct.column_id
        JOIN tables t ON t.id = c.table_id
        JOIN schemas s ON s.id = t.schema_id
        JOIN catalogs ca ON ca.id = s.catalog_id
        WHERE ca.workspace_id = ?
        ORDER BY ct.level DESC
        """,
        (workspace_id,),
    )
    out = []
    for r in rows:
        action = "查询时脱敏展示"
        if r["level"] == "L4":
            action = "禁止明文导出，仅授权角色可见"
        elif r["level"] == "L3":
            action = "默认脱敏，导出需审批"
        out.append(
            {
                **r,
                "level_name": LEVELS.get(r["level"], ""),
                "suggested_action": action,
                "subject": f"{r['catalog_name']}.{r['schema_name']}.{r['table_name']}.{r['column_name']}",
            }
        )
    return out


# ------------------------------------------------------------------ 质量规则

RULE_LABELS = {
    "null_rate": "空值率",
    "uniqueness": "唯一性",
    "range": "值域",
    "freshness": "时效性",
    "custom_sql": "自定义 SQL",
}


def _col(cfg: dict, column: str | None, table_ref: str) -> str:
    return f"{table_ref}.{column}" if column else table_ref


def run_rule(workspace_id: str, rule: dict) -> dict:
    cfg = rule.get("config") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError:
            cfg = {}
    table_ref = rule["table_ref"]
    column = rule.get("column_name")
    rtype = rule["rule_type"]

    if rtype == "null_rate":
        sql = (
            f"SELECT count(*) AS total, sum(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS bad "
            f"FROM {table_ref}"
        )
        r = catalog.run_workspace_sql(workspace_id, sql)
        total, bad = (r["rows"][0] if r["rows"] else (0, 0))
        rate = (bad or 0) / max(total or 1, 1)
        limit = float(cfg.get("max_rate", 0))
        ok = rate <= limit
        value = f"{rate:.2%}"
        detail = f"空值 {(bad or 0)} / 总行 {total}，阈值 {limit:.0%}"
    elif rtype == "uniqueness":
        sql = f"SELECT count(*) - count(DISTINCT {column}) AS dup FROM {table_ref}"
        r = catalog.run_workspace_sql(workspace_id, sql)
        dup = (r["rows"][0][0] if r["rows"] else 0) or 0
        limit = float(cfg.get("max_dup", 0))
        ok = dup <= limit
        value = f"重复 {dup}"
        detail = f"重复行数 {dup}，阈值 {int(limit)}"
    elif rtype == "range":
        lo = cfg.get("min")
        hi = cfg.get("max")
        conds = []
        if lo is not None:
            conds.append(f"{column} < {lo}")
        if hi is not None:
            conds.append(f"{column} > {hi}")
        where = " OR ".join(conds) or "1=0"
        sql = f"SELECT count(*) AS bad FROM {table_ref} WHERE {column} IS NOT NULL AND ({where})"
        r = catalog.run_workspace_sql(workspace_id, sql)
        bad = (r["rows"][0][0] if r["rows"] else 0) or 0
        ok = bad == 0
        value = f"越界 {bad}"
        detail = f"越界行数 {bad}，区间 [{lo}, {hi}]"
    elif rtype == "freshness":
        date_col = column or cfg.get("column") or "order_date"
        max_days = int(cfg.get("max_days", 1))
        sql = f"SELECT max({date_col}) AS latest FROM {table_ref}"
        r = catalog.run_workspace_sql(workspace_id, sql)
        latest = r["rows"][0][0] if r["rows"] else None
        if not latest:
            ok, value, detail = False, "无数据", "取不到最大日期"
        else:
            sql2 = f"SELECT date_diff('day', max({date_col}), current_date) AS lag FROM {table_ref}"
            r2 = catalog.run_workspace_sql(workspace_id, sql2)
            lag_days = (r2["rows"][0][0] if r2["rows"] else 0) or 0
            ok = lag_days <= max_days
            value = f"滞后 {lag_days} 天"
            detail = f"最新数据 {latest}，距今天 {lag_days} 天，阈值 {max_days} 天"
    elif rtype == "custom_sql":
        sql = cfg.get("sql") or ""
        if not sql:
            raise ValueError("自定义规则缺少 sql")
        r = catalog.run_workspace_sql(workspace_id, sql)
        first = r["rows"][0][0] if r["rows"] and r["rows"][0] else 0
        threshold = float(cfg.get("max_value", 0))
        ok = float(first or 0) <= threshold
        value = str(first)
        detail = f"返回值 {first}，阈值 ≤ {threshold}"
    else:
        raise ValueError(f"不支持的规则类型：{rtype}")

    result = {
        "status": "pass" if ok else "fail",
        "value": value,
        "detail": detail,
    }
    store.update(
        "quality_rules",
        rule["id"],
        {
            "last_status": result["status"],
            "last_value": value,
            "last_run_at": store.now(),
        },
    )
    return result


def suggest_rules(workspace_id: str, table_ref: str) -> list[dict]:
    detail = catalog.table_detail(workspace_id, table_ref)
    if not detail:
        raise ValueError(f"表 {table_ref} 不存在")
    suggestions: list[dict] = []
    for col in detail["columns"]:
        name, dtype = col["name"], (col["data_type"] or "").upper()
        tags = [t["tag"] for t in col.get("tags", [])]
        if re.search(r"(^|_)id$", name):
            suggestions.append(
                {
                    "column_name": name,
                    "rule_type": "uniqueness",
                    "config": {"max_dup": 0},
                    "reason": f"{name} 形如主键/外键，应唯一",
                }
            )
        if any(k in dtype for k in ("DOUBLE", "DECIMAL", "BIGINT", "INTEGER")) and re.search(
            r"amount|price|qty|quantity|discount|salary|cost", name
        ):
            suggestions.append(
                {
                    "column_name": name,
                    "rule_type": "range",
                    "config": {"min": 0, "max": 10000000},
                    "reason": f"{name} 为金额/数量类指标，不应为负或异常放大",
                }
            )
        if "DATE" in dtype or "TIMESTAMP" in dtype:
            suggestions.append(
                {
                    "column_name": name,
                    "rule_type": "freshness",
                    "config": {"max_days": 1},
                    "reason": f"{name} 为时间字段，可用于时效性监控",
                }
            )
        if tags:
            suggestions.append(
                {
                    "column_name": name,
                    "rule_type": "null_rate",
                    "config": {"max_rate": 0.02},
                    "reason": f"{name} 已标记为 {tags[0]}，敏感字段空值率需严格控制",
                }
            )
        else:
            suggestions.append(
                {
                    "column_name": name,
                    "rule_type": "null_rate",
                    "config": {"max_rate": 0.05},
                    "reason": f"{name} 基础非空约束",
                }
            )
    return suggestions[:20]


def profile(workspace_id: str, table_ref: str) -> dict:
    detail = catalog.table_detail(workspace_id, table_ref)
    if not detail:
        raise ValueError(f"表 {table_ref} 不存在")
    columns = []
    for col in detail["columns"][:40]:
        sql = (
            f"SELECT count(*) AS total, sum(CASE WHEN {col['name']} IS NULL THEN 1 ELSE 0 END) AS nulls, "
            f"count(DISTINCT {col['name']}) AS distinct_cnt FROM {table_ref}"
        )
        try:
            r = catalog.run_workspace_sql(workspace_id, sql)
            total, nulls, distinct_cnt = r["rows"][0]
        except Exception:  # noqa: BLE001
            total = nulls = distinct_cnt = None
        columns.append(
            {
                "name": col["name"],
                "data_type": col["data_type"],
                "total": total,
                "nulls": nulls,
                "null_rate": round((nulls or 0) / max(total or 1, 1), 4),
                "distinct_cnt": distinct_cnt,
                "tags": [t["tag"] for t in col.get("tags", [])],
            }
        )
    pii_cols = [c["name"] for c in columns if c["tags"]]
    rules = store.rows(
        "SELECT * FROM quality_rules WHERE workspace_id = ? AND table_ref = ?",
        (workspace_id, detail["ref"]),
    )
    passed = sum(1 for r in rules if r["last_status"] == "pass")
    return {
        "table": detail["ref"],
        "row_count": detail["row_count"],
        "column_count": len(detail["columns"]),
        "columns": columns,
        "pii_columns": pii_cols,
        "rules": rules,
        "quality_score": round(passed / len(rules), 2) if rules else None,
    }
