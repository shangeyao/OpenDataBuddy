"""数据质量规则引擎：6 类规则 + YAML 导入导出 + 前置阻断 + 规则级告警。

设计要点
--------
* 规则只有 6 种类型（null / unique / range / enum / regex / sql），每种都能被
  工作流当作「前置阻断」节点调用 —— 阻断能力比规则数量重要。
* 规则的期望、阈值、严重级别、是否阻断全部收敛在 ``config`` JSON 里，不再加表列，
  这样老库不需要迁移，前端也只需渲染一份 config。
* 每次执行都落 ``quality_rule_runs``，趋势与通过率都从这张表算，而不是看
  ``quality_rules.last_status``（后者只保留最后一次，无法回溯）。
* 规则失败同时写 ``anomaly_findings`` 与 ``audit_logs``：异常面板能看到，
  审计也能追到是谁触发的。相同异常 24 小时内不重复刷屏。
* YAML 解析只用标准库实现一个极简子集（嵌套映射 / 列表 / 标量 / 引号 /
  注释 / 块字符串），不引入 pyyaml —— 配置即代码的场景够用即可。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from . import catalog, store, warehouse

# --------------------------------------------------------------------- 元数据

RULE_TYPES: dict[str, dict] = {
    "null": {
        "label": "空值",
        "desc": "统计字段空值率，超过阈值即判定失败",
        "fields": ["max_rate", "max_null"],
        "example": {"max_rate": 0.02},
    },
    "unique": {
        "label": "唯一",
        "desc": "统计重复值行数，主键/外键约束用",
        "fields": ["max_dup", "max_dup_rate"],
        "example": {"max_dup": 0},
    },
    "range": {
        "label": "范围",
        "desc": "统计落在 [min, max] 区间外的行数",
        "fields": ["min", "max", "max_bad"],
        "example": {"min": 0, "max": 1000000},
    },
    "enum": {
        "label": "枚举",
        "desc": "统计不在允许取值集合内的行数",
        "fields": ["values", "allow_null", "max_bad"],
        "example": {"values": ["paid", "refund", "pending"]},
    },
    "regex": {
        "label": "正则",
        "desc": "统计不匹配正则的行数/占比，格式校验用",
        "fields": ["pattern", "max_mismatch_rate", "max_bad"],
        "example": {"pattern": "^1[3-9]\\d{9}$"},
    },
    "sql": {
        "label": "自定义 SQL",
        "desc": "执行只读 SQL，取首行首列与阈值比较（默认期望 0）",
        "fields": ["sql", "max_value", "expected"],
        "example": {"sql": "SELECT count(*) FROM demo.retail.fact_orders WHERE amount < 0"},
    },
}

SEVERITIES = ("info", "warning", "critical")
DEFAULT_SEVERITY = "warning"

# 历史规则名（governance.py / seed.py 里的老命名）映射到新类型，避免老库报错。
LEGACY_ALIASES = {
    "null_rate": "null",
    "not_null": "null",
    "uniqueness": "unique",
    "duplicate": "unique",
    "value_range": "range",
    "enumeration": "enum",
    "regexp": "regex",
    "custom_sql": "sql",
    "assertion": "sql",
}

# freshness 不在 6 类里，但老数据里有；单独实现一个内部执行器兜底。
_EXTRA_EXECUTORS = ("freshness",)


# --------------------------------------------------------------------- 小工具


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _cfg(rule: dict) -> dict:
    """config 可能是 JSON 字符串，也可能是已解析的 dict。"""
    raw = rule.get("config")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _num(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _j(value: Any) -> Any:
    """把 duckdb 返回的原生类型转成可 JSON 序列化的值。"""
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def _normalize_type(rule_type: Any) -> str:
    text = str(rule_type or "").strip().lower()
    return LEGACY_ALIASES.get(text, text)


def _severity(cfg: dict) -> str:
    value = str(cfg.get("severity") or DEFAULT_SEVERITY).lower()
    return value if value in SEVERITIES else DEFAULT_SEVERITY


def _blocking(cfg: dict) -> bool:
    raw = cfg.get("blocking", False)
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def workspace_of(workspace_id: str | None, table_ref: str = "") -> str:
    """工作空间兜底解析：显式传入 > 规则所在空间 > 第一个空间。"""
    if workspace_id:
        return workspace_id
    if table_ref:
        hit = store.row(
            "SELECT workspace_id FROM quality_rules WHERE table_ref = ? "
            "AND workspace_id IS NOT NULL LIMIT 1",
            (table_ref,),
        )
        if hit and hit.get("workspace_id"):
            return hit["workspace_id"]
    hit = store.row("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
    return hit["id"] if hit else ""


def _canonical_ref(workspace_id: str, table_ref: str) -> str:
    """把 sch.tbl / tbl 之类的简写还原成 catalog.schema.table。"""
    found = catalog.resolve(workspace_id, table_ref)
    return found["ref"] if found else (table_ref or "")


def _run_sql(workspace_id: str, sql: str, limit: int = 50) -> list[list]:
    return catalog.run_workspace_sql(workspace_id, sql, limit=limit)["rows"]


def _one(workspace_id: str, sql: str) -> list:
    rows = _run_sql(workspace_id, sql, limit=1)
    return rows[0] if rows else []


def _sql_str(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def ensure_tables() -> None:
    """防御性建表：老库可能在建表语句落地前就初始化过。"""
    with store.db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS quality_rule_runs (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                rule_id TEXT,
                table_ref TEXT,
                status TEXT,
                value TEXT,
                detail TEXT DEFAULT '',
                ran_at TEXT
            );
            CREATE TABLE IF NOT EXISTS profile_baselines (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                table_ref TEXT NOT NULL,
                column_name TEXT DEFAULT '',
                mode TEXT DEFAULT 'snapshot',
                stats TEXT DEFAULT '{}',
                captured_at TEXT
            );
            CREATE TABLE IF NOT EXISTS anomaly_findings (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                table_ref TEXT,
                column_name TEXT DEFAULT '',
                kind TEXT DEFAULT 'schema',
                severity TEXT DEFAULT 'warning',
                detail TEXT DEFAULT '',
                detected_at TEXT,
                acknowledged INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_rule_runs_rule ON quality_rule_runs(rule_id, ran_at);
            CREATE INDEX IF NOT EXISTS idx_anomaly_ws ON anomaly_findings(workspace_id, detected_at);
            """
        )


# --------------------------------------------------------------------- 执行器
# 每个执行器返回 {ok, value, detail, meta}；value 是写库用的短文本。


def _exec_null(ws: str, ref: str, column: str, cfg: dict) -> dict:
    if not column:
        raise ValueError("空值规则必须指定字段")
    total, nulls = _one(
        ws, f"SELECT count(*) AS total, count(*) - count({_q(column)}) AS nulls FROM {ref}"
    )
    total = int(total or 0)
    nulls = int(nulls or 0)
    rate = nulls / total if total else 0.0
    max_rate = _num(cfg.get("max_rate"), 0.0)
    max_null = _num(cfg.get("max_null"))
    if max_null is not None:
        ok = nulls <= max_null
        detail = f"空值 {nulls} 行 / 总行 {total}，阈值 ≤ {int(max_null)} 行"
        value = f"空值 {nulls}"
    else:
        ok = rate <= max_rate
        detail = f"空值 {nulls} 行 / 总行 {total}，空值率 {rate:.2%}，阈值 ≤ {max_rate:.2%}"
        value = f"{rate:.2%}"
    return {"ok": ok, "value": value, "detail": detail,
            "meta": {"total": total, "nulls": nulls, "rate": round(rate, 6)}}


def _exec_unique(ws: str, ref: str, column: str, cfg: dict) -> dict:
    if not column:
        raise ValueError("唯一性规则必须指定字段")
    total, dups = _one(
        ws,
        f"SELECT count(*) AS total, count(*) - count(DISTINCT {_q(column)}) AS dups FROM {ref}",
    )
    total = int(total or 0)
    dups = int(dups or 0)
    max_dup = _num(cfg.get("max_dup"), 0.0)
    max_rate = _num(cfg.get("max_dup_rate"))
    if max_rate is not None:
        rate = dups / total if total else 0.0
        ok = rate <= max_rate
        detail = f"重复行 {dups} / 总行 {total}，重复率 {rate:.2%}，阈值 ≤ {max_rate:.2%}"
        value = f"重复率 {rate:.2%}"
    else:
        ok = dups <= max_dup
        detail = f"重复行 {dups} / 总行 {total}，阈值 ≤ {int(max_dup)} 行"
        value = f"重复 {dups}"
    return {"ok": ok, "value": value, "detail": detail,
            "meta": {"total": total, "dups": dups}}


def _exec_range(ws: str, ref: str, column: str, cfg: dict) -> dict:
    if not column:
        raise ValueError("范围规则必须指定字段")
    lo, hi = cfg.get("min"), cfg.get("max")
    if lo is None and hi is None:
        raise ValueError("范围规则至少需要 min 或 max")
    conds = []
    if lo is not None:
        conds.append(f"{_q(column)} < {_num(lo, 0)}")
    if hi is not None:
        conds.append(f"{_q(column)} > {_num(hi, 0)}")
    where = " OR ".join(conds)
    bad = _one(
        ws,
        f"SELECT count(*) FROM {ref} WHERE {_q(column)} IS NOT NULL AND ({where})",
    )
    bad = int((bad[0] if bad else 0) or 0)
    max_bad = _num(cfg.get("max_bad"), 0.0)
    ok = bad <= max_bad
    detail = f"越界行 {bad}，允许区间 [{lo}, {hi}]，阈值 ≤ {int(max_bad)} 行"
    return {"ok": ok, "value": f"越界 {bad}", "detail": detail,
            "meta": {"bad": bad, "min": lo, "max": hi}}


def _exec_enum(ws: str, ref: str, column: str, cfg: dict) -> dict:
    if not column:
        raise ValueError("枚举规则必须指定字段")
    values = cfg.get("values") or cfg.get("allowed") or []
    if not values:
        raise ValueError("枚举规则缺少 values 取值集合")
    allow_null = bool(cfg.get("allow_null", True))
    listed = ", ".join(_sql_str(v) for v in values)
    conds = [f"CAST({_q(column)} AS VARCHAR) NOT IN ({listed})"]
    if not allow_null:
        conds.append(f"{_q(column)} IS NULL")
    bad = _one(ws, f"SELECT count(*) FROM {ref} WHERE {' OR '.join(conds)}")
    bad = int((bad[0] if bad else 0) or 0)
    max_bad = _num(cfg.get("max_bad"), 0.0)
    ok = bad <= max_bad
    detail = (
        f"非法取值 {bad} 行，允许集合 {values[:6]}"
        f"{'…' if len(values) > 6 else ''}，阈值 ≤ {int(max_bad)} 行"
    )
    return {"ok": ok, "value": f"非法 {bad}", "detail": detail,
            "meta": {"bad": bad, "values": len(values)}}


def _exec_regex(ws: str, ref: str, column: str, cfg: dict) -> dict:
    if not column:
        raise ValueError("正则规则必须指定字段")
    pattern = cfg.get("pattern") or cfg.get("regex") or ""
    if not pattern:
        raise ValueError("正则规则缺少 pattern")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"正则表达式非法：{exc}") from exc
    total, bad = _one(
        ws,
        f"SELECT count(*) AS total, "
        f"sum(CASE WHEN {_q(column)} IS NOT NULL "
        f"AND NOT regexp_full_match(CAST({_q(column)} AS VARCHAR), {_sql_str(pattern)}) "
        f"THEN 1 ELSE 0 END) AS bad FROM {ref}",
    )
    total = int(total or 0)
    bad = int(bad or 0)
    rate = bad / total if total else 0.0
    max_bad = _num(cfg.get("max_bad"))
    max_rate = _num(cfg.get("max_mismatch_rate"), 0.0 if max_bad is None else None)
    if max_bad is not None:
        ok = bad <= max_bad
        detail = f"不匹配行 {bad} / 总行 {total}，模式 /{pattern}/，阈值 ≤ {int(max_bad)} 行"
        value = f"不匹配 {bad}"
    else:
        ok = rate <= (max_rate or 0.0)
        detail = f"不匹配行 {bad} / 总行 {total}，不匹配率 {rate:.2%}，模式 /{pattern}/"
        value = f"{rate:.2%}"
    return {"ok": ok, "value": value, "detail": detail,
            "meta": {"total": total, "bad": bad, "rate": round(rate, 6)}}


def _exec_sql(ws: str, ref: str, column: str, cfg: dict) -> dict:
    sql = (cfg.get("sql") or "").strip()
    if not sql:
        raise ValueError("自定义 SQL 规则缺少 sql")
    rows = _run_sql(ws, sql, limit=1)
    first = rows[0][0] if rows and rows[0] else None
    numeric = _num(first)
    if "expected" in cfg and cfg["expected"] is not None:
        expected = cfg["expected"]
        ok = str(first) == str(expected) or (numeric is not None and numeric == _num(expected))
        detail = f"返回值 {first}，期望 == {expected}"
        value = str(first)
    else:
        max_value = _num(cfg.get("max_value"), 0.0)
        ok = (numeric if numeric is not None else 0.0) <= max_value
        detail = f"返回值 {first}，阈值 ≤ {max_value}"
        value = str(first)
    return {"ok": ok, "value": value, "detail": detail,
            "meta": {"raw": _j(first), "sql": sql[:400]}}


def _exec_freshness(ws: str, ref: str, column: str, cfg: dict) -> dict:
    """老规则的时效性兜底（不在 6 类对外暴露）。"""
    date_col = column or cfg.get("column") or ""
    if not date_col:
        raise ValueError("时效性规则必须指定时间字段")
    max_days = int(_num(cfg.get("max_days"), 1) or 1)
    row = _one(
        ws,
        f"SELECT max({_q(date_col)}), "
        f"date_diff('day', max({_q(date_col)}), current_date) FROM {ref}",
    )
    latest, lag = (row[0], row[1]) if row else (None, None)
    if latest is None:
        return {"ok": False, "value": "无数据", "detail": "取不到最大时间值", "meta": {}}
    lag_days = int(lag or 0)
    ok = lag_days <= max_days
    return {
        "ok": ok,
        "value": f"滞后 {lag_days} 天",
        "detail": f"最新数据 {_j(latest)}，距今 {lag_days} 天，阈值 ≤ {max_days} 天",
        "meta": {"latest": _j(latest), "lag_days": lag_days},
    }


_EXECUTORS = {
    "null": _exec_null,
    "unique": _exec_unique,
    "range": _exec_range,
    "enum": _exec_enum,
    "regex": _exec_regex,
    "sql": _exec_sql,
    "freshness": _exec_freshness,
}


def describe_types() -> list[dict]:
    """给前端渲染规则类型选择器用。"""
    out = []
    for key, meta in RULE_TYPES.items():
        out.append(
            {
                "type": key,
                "label": meta["label"],
                "desc": meta["desc"],
                "fields": meta["fields"],
                "example": meta["example"],
                "need_column": key != "sql",
            }
        )
    return out


# --------------------------------------------------------------------- 执行


def run_rule(
    rule: dict,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> dict:
    """执行一条规则。

    rule 至少要有 table_ref / rule_type；有 id 时会把结果回写 quality_rules。
    返回 {status: pass|fail|error, value, detail, ...}，永不抛异常 ——
    工作流节点拿到的必须是可判断的结果，而不是堆栈。
    """
    ensure_tables()
    cfg = _cfg(rule)
    rtype = _normalize_type(rule.get("rule_type"))
    table_ref = (rule.get("table_ref") or "").strip()
    column = (rule.get("column_name") or "").strip()
    ws = workspace_id or rule.get("workspace_id") or workspace_of(None, table_ref)
    severity = _severity(cfg)
    blocking = _blocking(cfg)
    ran_at = store.now()

    result: dict = {
        "rule_id": rule.get("id") or "",
        "table_ref": table_ref,
        "column_name": column,
        "rule_type": rtype,
        "severity": severity,
        "blocking": blocking,
        "ran_at": ran_at,
    }

    if rtype not in _EXECUTORS:
        result.update(status="error", value="", detail=f"不支持的规则类型：{rule.get('rule_type')}")
    elif not table_ref:
        result.update(status="error", value="", detail="规则缺少 table_ref")
    else:
        try:
            ref = _canonical_ref(ws, table_ref)
            result["table_ref"] = ref
            out = _EXECUTORS[rtype](ws, ref, column, cfg)
            result.update(
                status="pass" if out["ok"] else "fail",
                value=str(out.get("value", "")),
                detail=str(out.get("detail", "")),
                metrics=out.get("meta", {}),
            )
        except Exception as exc:  # noqa: BLE001 —— 规则执行失败要可解释
            result.update(status="error", value="", detail=f"{type(exc).__name__}: {exc}")

    _persist_run(ws, result)

    if result["status"] == "fail":
        record_anomaly(
            ws,
            result["table_ref"],
            column,
            kind="rule",
            severity=severity,
            detail=f"[{RULE_TYPES.get(rtype, {}).get('label', rtype)}] {result['detail']}",
        )
    if result["status"] in ("fail", "error"):
        store.audit(
            ws,
            user_id,
            "quality_rule_fail" if result["status"] == "fail" else "quality_rule_error",
            f"{result['table_ref']}.{column}" if column else result["table_ref"],
            {
                "rule_id": result["rule_id"],
                "rule_type": rtype,
                "severity": severity,
                "blocking": blocking,
                "status": result["status"],
                "value": result["value"],
                "detail": result["detail"],
            },
        )
    return result


def _persist_run(ws: str, result: dict) -> None:
    store.insert(
        "quality_rule_runs",
        {
            "id": store.new_id(),
            "workspace_id": ws,
            "rule_id": result.get("rule_id") or "",
            "table_ref": result.get("table_ref") or "",
            "status": result["status"],
            "value": str(result.get("value") or ""),
            "detail": str(result.get("detail") or "")[:2000],
            "ran_at": result.get("ran_at") or store.now(),
        },
    )
    rule_id = result.get("rule_id")
    if rule_id:
        store.execute(
            "UPDATE quality_rules SET last_status = ?, last_value = ?, last_run_at = ? WHERE id = ?",
            (result["status"], str(result.get("value") or ""), result.get("ran_at"), rule_id),
        )


def run_rule_by_id(
    rule_id: str, workspace_id: str | None = None, user_id: str | None = None
) -> dict:
    rule = store.row("SELECT * FROM quality_rules WHERE id = ?", (rule_id,))
    if not rule:
        raise ValueError("规则不存在")
    return run_rule(rule, workspace_id or rule.get("workspace_id"), user_id)


def run_rules(
    workspace_id: str,
    table_ref: str = "",
    only_enabled: bool = True,
    user_id: str | None = None,
) -> dict:
    """批量执行规则，返回汇总 + 明细。"""
    ensure_tables()
    sql = "SELECT * FROM quality_rules WHERE workspace_id = ?"
    params: list[Any] = [workspace_id]
    if table_ref:
        sql += " AND table_ref = ?"
        params.append(_canonical_ref(workspace_id, table_ref))
    if only_enabled:
        sql += " AND enabled = 1"
    sql += " ORDER BY created_at"
    rules = store.rows(sql, params)
    results = [run_rule(rule, workspace_id, user_id) for rule in rules]
    passed = sum(1 for r in results if r["status"] == "pass")
    return {
        "total": len(results),
        "passed": passed,
        "failed": sum(1 for r in results if r["status"] == "fail"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "pass_rate": round(passed / len(results), 4) if results else None,
        "results": results,
    }


# --------------------------------------------------------------------- 前置阻断


def precheck(
    table_ref: str,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> dict:
    """工作流前置阻断：跑该表所有启用的 blocking 规则，任一失败即阻断。

    返回 {blocked, reasons, ...}。reasons 里同时带上非阻断失败，方便调用方
    决定是「硬失败」还是「告警继续」。
    """
    ensure_tables()
    ws = workspace_id or workspace_of(None, table_ref)
    ref = _canonical_ref(ws, table_ref) if ws else (table_ref or "")
    rules = store.rows(
        "SELECT * FROM quality_rules WHERE workspace_id = ? AND table_ref = ? AND enabled = 1",
        (ws, ref),
    )
    reasons: list[dict] = []
    warnings: list[dict] = []
    checked = 0
    for rule in rules:
        cfg = _cfg(rule)
        if not _blocking(cfg) and cfg.get("severity") not in ("critical",):
            continue
        checked += 1
        res = run_rule(rule, ws, user_id)
        if res["status"] == "pass":
            continue
        item = {
            "rule_id": res["rule_id"],
            "rule_type": res["rule_type"],
            "column_name": res["column_name"],
            "status": res["status"],
            "severity": res["severity"],
            "blocking": res["blocking"],
            "value": res["value"],
            "detail": res["detail"],
        }
        if res["blocking"]:
            reasons.append(item)
        else:
            warnings.append(item)
    return {
        "table_ref": ref,
        "blocked": bool(reasons),
        "reasons": reasons,
        "warnings": warnings,
        "checked": checked,
        "passed": checked - len(reasons) - len(warnings),
        "ran_at": store.now(),
    }


def precheck_for_workspace(workspace_id: str, table_ref: str, user_id: str | None = None) -> dict:
    """precheck 的显式参数顺序版本，供已有 workspace 上下文的调用方使用。"""
    return precheck(table_ref, workspace_id, user_id)


# --------------------------------------------------------------------- 异常记录


def record_anomaly(
    workspace_id: str,
    table_ref: str,
    column_name: str = "",
    kind: str = "rule",
    severity: str = "warning",
    detail: str = "",
    dedupe_hours: int = 24,
) -> str:
    """写一条异常，24 小时内相同内容不重复刷屏。返回 finding id（去重时为空）。"""
    ensure_tables()
    cutoff = (
        datetime.now().astimezone() - timedelta(hours=max(0, dedupe_hours))
    ).isoformat(timespec="seconds")
    dup = store.row(
        "SELECT id FROM anomaly_findings WHERE workspace_id = ? AND table_ref = ? "
        "AND IFNULL(column_name, '') = ? AND kind = ? AND detail = ? "
        "AND detected_at >= ? AND acknowledged = 0 LIMIT 1",
        (workspace_id, table_ref, column_name or "", kind, detail, cutoff),
    )
    if dup:
        return ""
    finding_id = store.new_id()
    store.insert(
        "anomaly_findings",
        {
            "id": finding_id,
            "workspace_id": workspace_id,
            "table_ref": table_ref,
            "column_name": column_name or "",
            "kind": kind,
            "severity": severity if severity in SEVERITIES else DEFAULT_SEVERITY,
            "detail": detail[:2000],
            "detected_at": store.now(),
            "acknowledged": 0,
        },
    )
    return finding_id


# --------------------------------------------------------------------- 汇总


def summary(workspace_id: str, days: int = 7) -> dict:
    """质量概览：规则数 / 今日通过率 / 失败规则 / 趋势 / 未处理异常。"""
    ensure_tables()
    rules = store.rows(
        "SELECT * FROM quality_rules WHERE workspace_id = ? ORDER BY created_at DESC",
        (workspace_id,),
    )
    today = datetime.now().astimezone().date().isoformat()
    today_runs = store.rows(
        "SELECT * FROM quality_rule_runs WHERE workspace_id = ? AND ran_at >= ?",
        (workspace_id, today),
    )
    total_runs = len(today_runs)
    passed = sum(1 for r in today_runs if r["status"] == "pass")
    failed = sum(1 for r in today_runs if r["status"] == "fail")
    errored = sum(1 for r in today_runs if r["status"] == "error")

    last_status: dict[str, str] = {}
    for run in store.rows(
        "SELECT rule_id, status, ran_at FROM quality_rule_runs WHERE workspace_id = ? "
        "ORDER BY ran_at ASC",
        (workspace_id,),
    ):
        if run["rule_id"]:
            last_status[run["rule_id"]] = run["status"]
    failing_rules = [
        r for r in rules if (last_status.get(r["id"]) or r.get("last_status")) in ("fail", "error")
    ]

    trend: list[dict] = []
    for offset in range(max(1, days) - 1, -1, -1):
        day = (datetime.now().astimezone().date() - timedelta(days=offset)).isoformat()
        day_runs = [r for r in today_runs if str(r["ran_at"] or "").startswith(day)]
        if offset > 0:
            day_runs = store.rows(
                "SELECT * FROM quality_rule_runs WHERE workspace_id = ? AND ran_at >= ? AND ran_at < ?",
                (workspace_id, day, (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()),
            )
        ok = sum(1 for r in day_runs if r["status"] == "pass")
        trend.append(
            {
                "date": day,
                "total": len(day_runs),
                "passed": ok,
                "failed": sum(1 for r in day_runs if r["status"] == "fail"),
                "pass_rate": round(ok / len(day_runs), 4) if day_runs else None,
            }
        )

    unacked = store.scalar(
        "SELECT count(*) FROM anomaly_findings WHERE workspace_id = ? AND acknowledged = 0",
        (workspace_id,),
        0,
    )
    return {
        "rule_count": len(rules),
        "enabled_count": sum(1 for r in rules if r["enabled"]),
        "today_runs": total_runs,
        "today_passed": passed,
        "today_failed": failed,
        "today_errors": errored,
        "today_pass_rate": round(passed / total_runs, 4) if total_runs else None,
        "failing_rules": failing_rules,
        "failing_count": len(failing_rules),
        "unacked_anomalies": int(unacked or 0),
        "trend": trend,
        "by_type": _count_by(rules, "rule_type"),
        "by_severity": _count_by([{**_cfg(r), "_t": _cfg(r).get("severity", DEFAULT_SEVERITY)} for r in rules], "_t"),
    }


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        value = str(r.get(key) or "unknown")
        out[value] = out.get(value, 0) + 1
    return out


def history(rule_id: str, limit: int = 50) -> list[dict]:
    ensure_tables()
    return store.rows(
        "SELECT * FROM quality_rule_runs WHERE rule_id = ? ORDER BY ran_at DESC LIMIT ?",
        (rule_id, limit),
    )


def rule_detail(rule_id: str, workspace_id: str | None = None) -> dict:
    """规则详情 + 最近执行历史，供抽屉展示。"""
    rule = store.row("SELECT * FROM quality_rules WHERE id = ?", (rule_id,))
    if not rule:
        raise ValueError("规则不存在")
    if workspace_id and rule.get("workspace_id") not in (None, "", workspace_id):
        raise ValueError("规则不存在")
    runs = history(rule_id, 30)
    passed = sum(1 for r in runs if r["status"] == "pass")
    return {
        "rule": {**rule, "config": _cfg(rule)},
        "runs": runs,
        "stats": {
            "total": len(runs),
            "passed": passed,
            "pass_rate": round(passed / len(runs), 4) if runs else None,
        },
    }


# --------------------------------------------------------------------- 规则 CRUD


def create_rule(
    workspace_id: str,
    table_ref: str,
    rule_type: str,
    column_name: str = "",
    config: dict | None = None,
    enabled: bool = True,
) -> dict:
    ensure_tables()
    rtype = _normalize_type(rule_type)
    if rtype not in _EXECUTORS:
        raise ValueError(f"不支持的规则类型：{rule_type}（可选：{', '.join(RULE_TYPES)}）")
    found = catalog.resolve(workspace_id, table_ref)
    if not found:
        raise ValueError(f"表 {table_ref} 不存在")
    cfg = dict(config or {})
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "table_ref": found["ref"],
        "column_name": column_name or None,
        "rule_type": rtype,
        "config": json.dumps(cfg, ensure_ascii=False),
        "enabled": 1 if enabled else 0,
        "last_status": None,
        "last_value": None,
        "last_run_at": None,
        "created_at": store.now(),
    }
    store.insert("quality_rules", row)
    return {**row, "config": cfg}


def update_rule(rule_id: str, workspace_id: str, patch: dict) -> dict:
    rule = store.row(
        "SELECT * FROM quality_rules WHERE id = ? AND workspace_id = ?", (rule_id, workspace_id)
    )
    if not rule:
        raise ValueError("规则不存在")
    data: dict[str, Any] = {}
    if "table_ref" in patch and patch["table_ref"]:
        found = catalog.resolve(workspace_id, patch["table_ref"])
        data["table_ref"] = found["ref"] if found else patch["table_ref"]
    if "column_name" in patch:
        data["column_name"] = patch["column_name"] or None
    if "rule_type" in patch and patch["rule_type"]:
        rtype = _normalize_type(patch["rule_type"])
        if rtype not in _EXECUTORS:
            raise ValueError(f"不支持的规则类型：{patch['rule_type']}")
        data["rule_type"] = rtype
    if "config" in patch:
        cfg = patch["config"]
        data["config"] = json.dumps(cfg if isinstance(cfg, dict) else {}, ensure_ascii=False)
    if "enabled" in patch:
        data["enabled"] = 1 if patch["enabled"] else 0
    store.update("quality_rules", rule_id, data)
    return rule_detail(rule_id, workspace_id)["rule"]


def delete_rule(rule_id: str, workspace_id: str) -> bool:
    rule = store.row(
        "SELECT id FROM quality_rules WHERE id = ? AND workspace_id = ?", (rule_id, workspace_id)
    )
    if not rule:
        return False
    store.execute("DELETE FROM quality_rules WHERE id = ?", (rule_id,))
    return True


# --------------------------------------------------------------------- YAML
# 极简 YAML 子集：嵌套映射 / 列表 / 标量 / 引号 / 注释 / 块字符串 / 行内 {} []。
# 不追求覆盖 YAML 规范，只保证 bundle、质量规则这类配置文件能读能写。


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote = None
    prev_space = True
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and prev_space:
            break
        else:
            out.append(ch)
        prev_space = ch in " \t"
    return "".join(out).rstrip()


def _tokenize(text: str) -> list:
    toks: list = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(2).split("\n"):
        line = _strip_comment(raw)
        if not line.strip():
            toks.append(None)
            continue
        indent = len(line) - len(line.lstrip(" "))
        toks.append((indent, line.strip()))
    return toks


class _Stream:
    def __init__(self, toks: list):
        self.toks = toks
        self.i = 0

    def skip_blank(self) -> None:
        while self.i < len(self.toks) and self.toks[self.i] is None:
            self.i += 1

    def peek(self):
        self.skip_blank()
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self):
        self.skip_blank()
        if self.i >= len(self.toks):
            return None
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def at_end(self) -> bool:
        self.skip_blank()
        return self.i >= len(self.toks)


def _split_top(text: str, sep: str) -> list[str]:
    parts, buf = [], []
    depth = 0
    quote = None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[{(":
            depth += 1
            buf.append(ch)
        elif ch in "]})":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _split_kv(text: str):
    """'key: value' -> (key, value)；不是映射则返回 None。"""
    depth = 0
    quote = None
    for idx, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            nxt = text[idx + 1] if idx + 1 < len(text) else ""
            if nxt in ("", " ", "\t"):
                key = text[:idx].strip().strip("\"'")
                return key, text[idx + 1:].strip()
    return None


def _scalar(text: str) -> Any:
    t = text.strip()
    if t == "":
        return ""
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        body = t[1:-1]
        if t[0] == '"':
            return (
                body.replace("\\\\", "\x00")
                .replace('\\"', '"')
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace("\x00", "\\")
            )
        return body.replace("''", "'")
    low = t.lower()
    if low in ("null", "~", "none"):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if t.startswith("{") and t.endswith("}"):
        out: dict = {}
        for chunk in _split_top(t[1:-1], ","):
            if not chunk.strip():
                continue
            kv = _split_kv(chunk.strip())
            if kv:
                out[kv[0]] = _scalar(kv[1])
        return out
    if t.startswith("[") and t.endswith("]"):
        return [_scalar(c) for c in _split_top(t[1:-1], ",") if c.strip()]
    if re.fullmatch(r"[-+]?\d+", t):
        try:
            return int(t)
        except ValueError:
            return t
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?", t) and any(c in t for c in ".eE"):
        try:
            return float(t)
        except ValueError:
            return t
    return t


def _parse_block_scalar(stream: _Stream, parent_indent: int, folded: bool) -> str:
    stream.skip_blank()
    lines: list[str] = []
    base: int | None = None
    while stream.i < len(stream.toks):
        tok = stream.toks[stream.i]
        if tok is None:
            lines.append("")
            stream.i += 1
            continue
        indent, content = tok
        if indent <= parent_indent:
            break
        if base is None:
            base = indent
        lines.append(content)
        stream.i += 1
    while lines and lines[-1] == "":
        lines.pop()
    return (" " if folded else "\n").join(lines)


def _parse_node(stream: _Stream, indent: int) -> Any:
    tok = stream.peek()
    if tok is None or tok[0] < indent:
        return None
    if tok[1].startswith("- ") or tok[1] == "-":
        return _parse_seq(stream, tok[0])
    return _parse_map(stream, tok[0])


def _parse_map(stream: _Stream, indent: int) -> dict:
    out: dict = {}
    while True:
        tok = stream.peek()
        if tok is None or tok[0] != indent:
            break
        if tok[1].startswith("- "):
            break
        kv = _split_kv(tok[1])
        if not kv:
            break
        key, raw_value = kv
        stream.take()
        if raw_value in ("|", "|-", "|+", ">", ">-", ">+"):
            out[key] = _parse_block_scalar(stream, indent, raw_value.startswith(">"))
        elif raw_value == "":
            nxt = stream.peek()
            if nxt is not None and (nxt[0] > indent or (nxt[0] == indent and nxt[1].startswith("- "))):
                out[key] = _parse_node(stream, nxt[0])
            else:
                out[key] = None
        else:
            out[key] = _scalar(raw_value)
    return out


def _parse_seq(stream: _Stream, indent: int) -> list:
    out: list = []
    while True:
        tok = stream.peek()
        if tok is None or tok[0] != indent:
            break
        if not (tok[1].startswith("- ") or tok[1] == "-"):
            break
        content = tok[1][1:].strip()
        stream.take()
        if not content:
            nxt = stream.peek()
            if nxt is not None and nxt[0] > indent:
                out.append(_parse_node(stream, nxt[0]))
            else:
                out.append(None)
            continue
        kv = _split_kv(content)
        if kv is None:
            out.append(_scalar(content))
            continue
        # 列表项本身是映射：第一对 kv 已内联，后续键在更深缩进处
        item: dict = {}
        key, raw_value = kv
        if raw_value in ("|", "|-", "|+", ">", ">-", ">+"):
            item[key] = _parse_block_scalar(stream, indent, raw_value.startswith(">"))
        elif raw_value == "":
            nxt = stream.peek()
            if nxt is not None and nxt[0] > indent:
                item[key] = _parse_node(stream, nxt[0])
            else:
                item[key] = None
        else:
            item[key] = _scalar(raw_value)
        while True:
            tok2 = stream.peek()
            if tok2 is None or tok2[0] <= indent:
                break
            kv2 = _split_kv(tok2[1])
            if kv2 is None:
                break
            k2, v2 = kv2
            child_indent = tok2[0]
            stream.take()
            if v2 in ("|", "|-", "|+", ">", ">-", ">+"):
                item[k2] = _parse_block_scalar(stream, child_indent, v2.startswith(">"))
            elif v2 == "":
                nxt = stream.peek()
                if nxt is not None and nxt[0] > child_indent:
                    item[k2] = _parse_node(stream, nxt[0])
                else:
                    item[k2] = None
            else:
                item[k2] = _scalar(v2)
        out.append(item)
    return out


def parse_yaml(text: str) -> Any:
    """解析极简 YAML，返回 dict / list / 标量。"""
    if not text or not text.strip():
        return None
    stream = _Stream(_tokenize(text))
    # 跳过文档分隔符
    while True:
        tok = stream.peek()
        if tok is not None and tok[1] in ("---", "..."):
            stream.take()
            continue
        break
    return _parse_node(stream, stream.peek()[0] if stream.peek() else 0)


_RULE_KEY_ALIASES = {
    "table": "table_ref",
    "table_ref": "table_ref",
    "column": "column_name",
    "column_name": "column_name",
    "field": "column_name",
    "type": "rule_type",
    "rule_type": "rule_type",
    "name": "name",
    "description": "description",
    "config": "config",
    "severity": "severity",
    "level": "severity",
    "blocking": "blocking",
    "enabled": "enabled",
}


def yaml_to_rules(text: str) -> list[dict]:
    """把 YAML 文本解析成规则 dict 列表（不落库）。

    支持两种形态：
      1) 顶层直接是规则列表（- table: ...）
      2) 顶层是 {version: 1, rules: [...]} / {quality_rules: [...]}
    """
    doc = parse_yaml(text)
    if doc is None:
        return []
    if isinstance(doc, dict):
        items = doc.get("rules") or doc.get("quality_rules") or doc.get("checks") or []
    elif isinstance(doc, list):
        items = doc
    else:
        raise ValueError("YAML 顶层必须是规则列表或包含 rules 的映射")

    rules: list[dict] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"第 {idx + 1} 条规则不是映射结构")
        rule: dict = {}
        for raw_key, value in item.items():
            key = _RULE_KEY_ALIASES.get(str(raw_key).strip().lower())
            if not key:
                continue
            rule[key] = value
        if not rule.get("table_ref"):
            raise ValueError(f"第 {idx + 1} 条规则缺少 table / table_ref")
        # YAML 里 `type: null` 会被解析成 Python None（null 是 YAML 空值字面量），
        # 但 null 恰好也是我们的规则类型名，所以这里要还原回来。
        if rule.get("rule_type") is None and "rule_type" in rule:
            rule["rule_type"] = "null"
        rtype = _normalize_type(rule.get("rule_type"))
        if rtype not in _EXECUTORS:
            raise ValueError(
                f"第 {idx + 1} 条规则类型 {rule.get('rule_type')} 不支持"
                f"（可选：{', '.join(RULE_TYPES)}）"
            )
        cfg = rule.get("config") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        if rule.get("severity"):
            cfg["severity"] = str(rule["severity"]).lower()
        if "blocking" in rule:
            cfg["blocking"] = bool(rule["blocking"])
        rule["rule_type"] = rtype
        rule["config"] = cfg
        rule["column_name"] = rule.get("column_name") or ""
        rule["enabled"] = bool(rule.get("enabled", True))
        rules.append(rule)
    return rules


def import_rules(
    workspace_id: str,
    text: str,
    replace: bool = False,
    user_id: str | None = None,
) -> dict:
    """导入 YAML 规则。replace=True 时先清空该空间下同名表的旧规则。"""
    ensure_tables()
    parsed = yaml_to_rules(text)
    created: list[dict] = []
    skipped: list[dict] = []
    for item in parsed:
        found = catalog.resolve(workspace_id, item["table_ref"])
        if not found:
            skipped.append({"table_ref": item["table_ref"], "reason": "表未在目录中登记"})
            continue
        created.append(
            create_rule(
                workspace_id,
                found["ref"],
                item["rule_type"],
                item.get("column_name") or "",
                item["config"],
                item.get("enabled", True),
            )
        )
    if replace and created:
        refs = sorted({c["table_ref"] for c in created})
        keep = {c["id"] for c in created}
        for ref in refs:
            for old in store.rows(
                "SELECT id FROM quality_rules WHERE workspace_id = ? AND table_ref = ?", (workspace_id, ref)
            ):
                if old["id"] not in keep:
                    store.execute("DELETE FROM quality_rules WHERE id = ?", (old["id"],))
    store.audit(
        workspace_id,
        user_id,
        "quality_rule_import",
        f"{len(created)} rules",
        {"created": len(created), "skipped": skipped},
    )
    return {"created": len(created), "skipped": skipped, "rules": created}


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if text == "":
        return "''"
    if re.search(r"[:#\[\]{},&*!|>'\"%@`]", text) or text != text.strip() or "\n" in text:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if text.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~"):
        return '"' + text + '"'
    return text


def _dump_yaml(value: Any, indent: int, lines: list[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(sub, (dict, list)) and sub:
                lines.append(f"{pad}{key}:")
                _dump_yaml(sub, indent + 2, lines)
            else:
                lines.append(f"{pad}{key}: {_dump_scalar(sub)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item:
                first = True
                for key, sub in item.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    if isinstance(sub, (dict, list)) and sub:
                        lines.append(f"{prefix}{key}:")
                        _dump_yaml(sub, indent + 4, lines)
                    else:
                        lines.append(f"{prefix}{key}: {_dump_scalar(sub)}")
                    first = False
            else:
                lines.append(f"{pad}- {_dump_scalar(item)}")


def rules_to_yaml(rules: list[dict]) -> str:
    """把规则列表导出成 YAML 文本（可再次被 yaml_to_rules 读回）。"""
    items: list[dict] = []
    for rule in rules:
        cfg = _cfg(rule)
        body = {k: v for k, v in cfg.items() if k not in ("severity", "blocking")}
        item = {
            "table": rule.get("table_ref") or "",
            "column": rule.get("column_name") or "",
            "type": _normalize_type(rule.get("rule_type")),
            "severity": cfg.get("severity", DEFAULT_SEVERITY),
            "blocking": bool(_blocking(cfg)),
            "enabled": bool(rule.get("enabled", 1)),
        }
        if rule.get("name"):
            item["name"] = rule["name"]
        if body:
            item["config"] = body
        items.append(item)
    doc = {
        "version": 1,
        "generator": "OpenDataBuddy 数据质量",
        "rules": items,
    }
    lines = ["# OpenDataBuddy 数据质量规则（YAML 导入导出，标准库解析，无需 pyyaml）"]
    _dump_yaml(doc, 0, lines)
    return "\n".join(lines) + "\n"


def export_rules(workspace_id: str, table_ref: str = "") -> str:
    sql = "SELECT * FROM quality_rules WHERE workspace_id = ?"
    params: list[Any] = [workspace_id]
    if table_ref:
        sql += " AND table_ref = ?"
        params.append(_canonical_ref(workspace_id, table_ref))
    sql += " ORDER BY created_at"
    return rules_to_yaml(store.rows(sql, params))


def list_rules(workspace_id: str, table_ref: str = "", enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM quality_rules WHERE workspace_id = ?"
    params: list[Any] = [workspace_id]
    if table_ref:
        sql += " AND table_ref = ?"
        params.append(_canonical_ref(workspace_id, table_ref))
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY created_at DESC"
    rows = store.rows(sql, params)
    for row in rows:
        row["config"] = _cfg(row)
    return rows
