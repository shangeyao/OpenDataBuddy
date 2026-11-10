"""数据质量与剖析接口：规则 CRUD / 执行 / YAML 导入导出 / 剖析 / 漂移 / 异常。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import catalog, drift, profiling, quality, store
from .auth import ctx

router = APIRouter(prefix="/api", tags=["quality"])


# ------------------------------------------------------------------- 入参模型


class RuleIn(BaseModel):
    table_ref: str = ""
    table: str = ""
    column_name: str = ""
    column: str = ""
    rule_type: str
    config: dict[str, Any] = {}
    enabled: bool = True


class RulePatchIn(BaseModel):
    table_ref: str | None = None
    column_name: str | None = None
    rule_type: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class YamlIn(BaseModel):
    yaml: str = ""
    text: str = ""
    content: str = ""
    replace: bool = False


class ProfileIn(BaseModel):
    table_ref: str = ""
    table: str = ""
    mode: str = "snapshot"
    time_column: str | None = None
    granularity: str | None = None
    sample_rows: int | None = None
    max_columns: int = profiling.MAX_COLUMNS
    save_baseline: bool = False


class BaselineIn(BaseModel):
    table_ref: str = ""
    table: str = ""
    column_name: str = ""
    mode: str = "snapshot"
    stats: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None


class DriftIn(BaseModel):
    table_ref: str = ""
    table: str = ""
    column: str = ""
    baseline_id: str | None = None
    metric: str | None = None
    thresholds: dict[str, float] | None = None


class DriftBatchIn(BaseModel):
    table_ref: str = ""
    table: str = ""
    columns: list[str] | None = None


def _ref(payload: Any) -> str:
    """兼容前端传 table_ref 或 table。"""
    return (
        (getattr(payload, "table_ref", "") or "").strip()
        or (getattr(payload, "table", "") or "").strip()
    )


def _column(payload: Any) -> str:
    return (
        (getattr(payload, "column_name", "") or "").strip()
        or (getattr(payload, "column", "") or "").strip()
    )


def _canonical(ws: str, table_ref: str) -> str:
    found = catalog.resolve(ws, table_ref)
    if not found:
        raise HTTPException(status_code=404, detail=f"表 {table_ref} 不存在")
    return found["ref"]


def _rule_out(row: dict) -> dict:
    cfg = row.get("config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError:
            cfg = {}
    rtype = quality._normalize_type(row.get("rule_type"))  # noqa: SLF001
    meta = quality.RULE_TYPES.get(rtype, {})
    return {
        **row,
        "config": cfg if isinstance(cfg, dict) else {},
        "rule_type_label": meta.get("label", row.get("rule_type")),
        "severity": (cfg or {}).get("severity", quality.DEFAULT_SEVERITY)
        if isinstance(cfg, dict)
        else quality.DEFAULT_SEVERITY,
        "blocking": bool((cfg or {}).get("blocking")) if isinstance(cfg, dict) else False,
        "enabled": bool(row.get("enabled")),
    }


# ------------------------------------------------------------------- 规则列表


@router.get("/quality/rules")
def list_rules(table: str = "", ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    rows = quality.list_rules(ws, table_ref=table)
    return {
        "rules": [_rule_out(r) for r in rows],
        "types": quality.describe_types(),
        "severities": list(quality.SEVERITIES),
    }


@router.post("/quality/rules")
def create_rule(payload: RuleIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    table_ref = _ref(payload)
    if not table_ref:
        raise HTTPException(status_code=400, detail="请指定目标表")
    try:
        rule = quality.create_rule(
            ws,
            table_ref,
            payload.rule_type,
            _column(payload),
            payload.config,
            payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(ws, ctxd["user"]["id"], "quality_rule_create", f"{rule['table_ref']}.{rule['column_name'] or ''}", rule["id"])
    return {"rule": _rule_out(rule)}


@router.patch("/quality/rules/{rule_id}")
def update_rule(rule_id: str, payload: RulePatchIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    patch = payload.model_dump(exclude_none=True)
    try:
        rule = quality.update_rule(rule_id, ws, patch)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    store.audit(ws, ctxd["user"]["id"], "quality_rule_update", rule_id, patch)
    return {"rule": _rule_out(rule)}


@router.delete("/quality/rules/{rule_id}")
def delete_rule(rule_id: str, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    if not quality.delete_rule(rule_id, ws):
        raise HTTPException(status_code=404, detail="规则不存在")
    store.audit(ws, ctxd["user"]["id"], "quality_rule_delete", rule_id)
    return {"ok": True}


# 注意：固定路径（/export、/run-all、/validate、/import）必须声明在
# /{rule_id} 之前，否则 FastAPI 会把 "export" 当成 rule_id 匹配掉。


@router.post("/quality/rules/run-all")
def run_all(table: str = "", ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    out = quality.run_rules(ws, table_ref=table, user_id=ctxd["user"]["id"])
    store.audit(ws, ctxd["user"]["id"], "quality_run_all", table or "全部", out["total"])
    return out


# ------------------------------------------------------------------- YAML


@router.post("/quality/rules/import")
def import_rules(payload: YamlIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    text = payload.yaml or payload.text or payload.content
    if not text.strip():
        raise HTTPException(status_code=400, detail="YAML 内容为空")
    try:
        out = quality.import_rules(ws, text, payload.replace, ctxd["user"]["id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"created": out["created"], "skipped": out["skipped"], "rules": [_rule_out(r) for r in out["rules"]]}


@router.post("/quality/rules/validate")
def validate_yaml(payload: YamlIn, ctxd: dict = Depends(ctx)) -> dict:
    """只校验不落库，前端导入前预检用。"""
    text = payload.yaml or payload.text or payload.content
    try:
        parsed = quality.yaml_to_rules(text)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "rules": []}
    ws = ctxd["workspace_id"]
    issues = []
    for idx, item in enumerate(parsed):
        if not catalog.resolve(ws, item["table_ref"]):
            issues.append(
                {"level": "warning", "index": idx, "message": f"表 {item['table_ref']} 未在目录中登记，导入时会跳过"}
            )
    return {"ok": True, "rules": parsed, "issues": issues, "count": len(parsed)}


@router.get("/quality/rules/export")
def export_rules(table: str = "", ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    text = quality.export_rules(ws, table_ref=table)
    return {"yaml": text, "text": text}


@router.get("/quality/rules/{rule_id}")
def get_rule(rule_id: str, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    try:
        detail = quality.rule_detail(rule_id, ws)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "rule": _rule_out(detail["rule"]),
        "runs": detail["runs"],
        "stats": detail["stats"],
    }


@router.post("/quality/rules/{rule_id}/run")
def run_rule(rule_id: str, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    try:
        return quality.run_rule_by_id(rule_id, ws, ctxd["user"]["id"])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/quality/rules/{rule_id}/runs")
def rule_runs(rule_id: str, limit: int = 50, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    rule = store.row(
        "SELECT * FROM quality_rules WHERE id = ? AND workspace_id = ?", (rule_id, ws)
    )
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"runs": quality.history(rule_id, limit)}


# ------------------------------------------------------------------- 汇总 / 阻断


@router.get("/quality/summary")
def summary(days: int = 7, ctxd: dict = Depends(ctx)) -> dict:
    out = quality.summary(ctxd["workspace_id"], days)
    return {
        **out,
        "failing_rules": [_rule_out(r) for r in out["failing_rules"]],
    }


@router.post("/quality/precheck")
def precheck(payload: DriftBatchIn, ctxd: dict = Depends(ctx)) -> dict:
    """前置阻断预检：供工作流节点与前端试跑。"""
    table_ref = _ref(payload)
    if not table_ref:
        raise HTTPException(status_code=400, detail="请指定目标表")
    ws = ctxd["workspace_id"]
    try:
        return quality.precheck(_canonical(ws, table_ref), ws, ctxd["user"]["id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------------------- 剖析


@router.get("/profiling/modes")
def profile_modes() -> dict:
    return {"modes": profiling.describe_modes()}


@router.post("/profiling/profile")
def profile(payload: ProfileIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    table_ref = _ref(payload)
    if not table_ref:
        raise HTTPException(status_code=400, detail="请指定目标表")
    try:
        out = profiling.profile_table(
            _canonical(ws, table_ref),
            payload.mode,
            workspace_id=ws,
            time_column=payload.time_column,
            granularity=payload.granularity,
            sample_rows=payload.sample_rows,
            max_columns=payload.max_columns,
            save=payload.save_baseline,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(ws, ctxd["user"]["id"], "profile_table", out["table_ref"], {"mode": payload.mode})
    return out


@router.get("/profiling/baselines")
def list_baselines(table: str = "", limit: int = 50, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    ref = _canonical(ws, table) if table else ""
    rows = profiling.list_baselines(table_ref=ref, workspace_id=ws, limit=limit)
    for row in rows:
        stats = row.get("stats") or {}
        row["row_count"] = stats.get("row_count")
        row["column_count"] = stats.get("column_count")
        row["column_names"] = sorted((stats.get("columns") or {}).keys())[:12]
    return {"baselines": rows}


@router.post("/profiling/baselines")
def create_baseline(payload: BaselineIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    table_ref = _ref(payload)
    if not table_ref:
        raise HTTPException(status_code=400, detail="请指定目标表")
    ref = _canonical(ws, table_ref)
    stats = payload.stats or payload.profile
    if not stats:
        try:
            stats = profiling.profile_table(ref, payload.mode, workspace_id=ws)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = profiling.save_baseline(
        ref, _column(payload), payload.mode, stats, workspace_id=ws
    )
    store.audit(ws, ctxd["user"]["id"], "save_baseline", ref, {"baseline_id": saved["id"]})
    return {"baseline": saved}


@router.delete("/profiling/baselines/{baseline_id}")
def delete_baseline(baseline_id: str, ctxd: dict = Depends(ctx)) -> dict:
    if not profiling.delete_baseline(baseline_id, ctxd["workspace_id"]):
        raise HTTPException(status_code=404, detail="基线不存在")
    return {"ok": True}


# ------------------------------------------------------------------- 漂移


@router.get("/drift/metrics")
def drift_metrics() -> dict:
    return {"metrics": drift.describe_metrics()}


@router.post("/drift/detect")
def detect_drift(payload: DriftIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    table_ref = _ref(payload)
    column = _column(payload)
    if not table_ref or not column:
        raise HTTPException(status_code=400, detail="请指定目标表与字段")
    try:
        return drift.detect_drift(
            _canonical(ws, table_ref),
            column,
            baseline_id=payload.baseline_id,
            metric=payload.metric,
            workspace_id=ws,
            thresholds=payload.thresholds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/drift/batch")
def detect_drift_batch(payload: DriftBatchIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    table_ref = _ref(payload)
    if not table_ref:
        raise HTTPException(status_code=400, detail="请指定目标表")
    try:
        return drift.detect_drift_batch(_canonical(ws, table_ref), payload.columns, workspace_id=ws)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/drift/anomalies")
def detect_anomalies(payload: DriftBatchIn, ctxd: dict = Depends(ctx)) -> dict:
    """零配置异常检测：schema / 行数 / 分布 / 新鲜度。"""
    ws = ctxd["workspace_id"]
    table_ref = _ref(payload)
    if not table_ref:
        raise HTTPException(status_code=400, detail="请指定目标表")
    try:
        return drift.detect_anomalies(_canonical(ws, table_ref), workspace_id=ws)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------------------- 异常


@router.get("/anomalies")
def list_anomalies(
    table: str = "",
    acknowledged: str = "",
    limit: int = 100,
    ctxd: dict = Depends(ctx),
) -> dict:
    ws = ctxd["workspace_id"]
    acked: bool | None = None
    if acknowledged in ("1", "true", "yes"):
        acked = True
    elif acknowledged in ("0", "false", "no"):
        acked = False
    rows = drift.list_anomalies(ws, table_ref=table, acknowledged=acked, limit=limit)
    unacked = store.scalar(
        "SELECT count(*) FROM anomaly_findings WHERE workspace_id = ? AND acknowledged = 0", (ws,), 0
    )
    return {"anomalies": rows, "unacked": int(unacked or 0)}


@router.post("/anomalies/{anomaly_id}/ack")
def ack_anomaly(anomaly_id: str, ctxd: dict = Depends(ctx)) -> dict:
    if not drift.acknowledge(anomaly_id, ctxd["workspace_id"], ctxd["user"]["id"]):
        raise HTTPException(status_code=404, detail="异常不存在")
    return {"ok": True}


# ------------------------------------------------------------------- 元信息


@router.get("/quality/meta")
def quality_meta(ctxd: dict = Depends(ctx)) -> dict:
    """前端一次拿全：规则类型 / 严重级别 / 剖析模式 / 漂移指标。"""
    return {
        "rule_types": quality.describe_types(),
        "severities": [
            {"value": "info", "label": "提示"},
            {"value": "warning", "label": "告警"},
            {"value": "critical", "label": "严重"},
        ],
        "profile_modes": profiling.describe_modes(),
        "drift_metrics": drift.describe_metrics(),
        "histogram_bins": profiling.HISTOGRAM_BINS,
    }
