"""OPS 运维 + Agent 安全防护接口。

路由分组
--------
* `/alerts/*`   告警渠道 / 规则 / 投递记录
* `/ops/*`      四层视图 + AI 诊断（`/ops/traces`、`/ops/audit` 在 auth.py）
* `/security/*` 双向 Guardrails 在线检测 / 高危 SQL 评估 / OWASP 清单
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import alerts, guardrails_ext, opsview, store
from .auth import ctx

router = APIRouter(prefix="/api", tags=["ops-ext"])


# ================================================================ 请求模型


class ChannelIn(BaseModel):
    type: str
    name: str = ""
    config: dict = Field(default_factory=dict)
    enabled: bool = True


class ChannelPatch(BaseModel):
    name: str | None = None
    config: dict | None = None
    enabled: bool | None = None


class RuleIn(BaseModel):
    name: str
    event: str
    channels: list[str] = Field(default_factory=list)
    condition: dict = Field(default_factory=dict)
    severity: str = "warning"
    enabled: bool = True


class RulePatch(BaseModel):
    name: str | None = None
    event: str | None = None
    channels: list[str] | None = None
    condition: dict | None = None
    severity: str | None = None
    enabled: bool | None = None


class EmitIn(BaseModel):
    """手动触发一次事件（用于验证规则与渠道是否配通）。"""
    event: str = "flow_run_failed"
    subject: str = "手动测试告警"
    body: str = ""
    severity: str = ""
    detail: dict = Field(default_factory=dict)
    force: bool = False


class CheckTextIn(BaseModel):
    text: str = ""
    context: dict = Field(default_factory=dict)


class AssessSqlIn(BaseModel):
    sql: str = ""


class AssessToolIn(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)


# ================================================================ 告警：渠道


@router.get("/alerts/meta")
def alerts_meta(ctxd: dict = Depends(ctx)) -> dict:
    """渠道类型（5 种）+ 事件类型（4 类）+ 严重级别 + 抑制窗口，供前端渲染。"""
    return {
        "channels": alerts.describe_channels(),
        "events": alerts.describe_events(),
        "severities": [{"value": k, "label": v} for k, v in alerts.SEVERITIES.items()],
        "suppress_window_seconds": alerts.SUPPRESS_WINDOW_SECONDS,
        "stats": alerts.stats(ctxd["workspace_id"]),
    }


@router.get("/alerts/channels")
def list_channels(ctxd: dict = Depends(ctx)) -> dict:
    channels = alerts.list_channels(ctxd["workspace_id"])
    return {"channels": channels, "types": alerts.describe_channels(), "total": len(channels)}


@router.post("/alerts/channels")
def create_channel(payload: ChannelIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        channel = alerts.create_channel(
            ctxd["workspace_id"], payload.type, payload.name, payload.config, payload.enabled
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_alert_channel", payload.type,
                {"name": channel.get("name")})
    return {"channel": channel, "issues": channel.get("issues", [])}


@router.patch("/alerts/channels/{channel_id}")
def update_channel(channel_id: str, payload: ChannelPatch, ctxd: dict = Depends(ctx)) -> dict:
    patch: dict[str, Any] = {}
    if payload.name is not None:
        patch["name"] = payload.name
    if payload.config is not None:
        patch["config"] = payload.config
    if payload.enabled is not None:
        patch["enabled"] = payload.enabled
    try:
        channel = alerts.update_channel(ctxd["workspace_id"], channel_id, patch)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"channel": channel}


@router.delete("/alerts/channels/{channel_id}")
def delete_channel(channel_id: str, ctxd: dict = Depends(ctx)) -> dict:
    alerts.delete_channel(ctxd["workspace_id"], channel_id)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "delete_alert_channel", channel_id)
    return {"ok": True}


@router.post("/alerts/channels/{channel_id}/test")
def test_channel(channel_id: str, ctxd: dict = Depends(ctx)) -> dict:
    """发一条模拟测试告警（不发起真实外部请求）。"""
    try:
        return alerts.test_channel(ctxd["workspace_id"], channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ================================================================ 告警：规则


@router.get("/alerts/rules")
def list_rules(ctxd: dict = Depends(ctx)) -> dict:
    rules = alerts.list_rules(ctxd["workspace_id"])
    return {"rules": rules, "events": alerts.describe_events(), "total": len(rules)}


@router.post("/alerts/rules")
def create_rule(payload: RuleIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        rule = alerts.create_rule(
            ctxd["workspace_id"], payload.name, payload.event, payload.channels,
            payload.condition, payload.severity, payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_alert_rule", payload.name,
                {"event": payload.event})
    return {"rule": rule}


@router.patch("/alerts/rules/{rule_id}")
def update_rule(rule_id: str, payload: RulePatch, ctxd: dict = Depends(ctx)) -> dict:
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        return {"rule": alerts.update_rule(ctxd["workspace_id"], rule_id, patch)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/alerts/rules/{rule_id}")
def delete_rule(rule_id: str, ctxd: dict = Depends(ctx)) -> dict:
    alerts.delete_rule(ctxd["workspace_id"], rule_id)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "delete_alert_rule", rule_id)
    return {"ok": True}


@router.get("/alerts/deliveries")
def list_deliveries(
    limit: int = 100,
    status: str | None = None,
    event: str | None = None,
    ctxd: dict = Depends(ctx),
) -> dict:
    deliveries = alerts.list_deliveries(ctxd["workspace_id"], limit, status, event)
    return {
        "deliveries": deliveries,
        "total": len(deliveries),
        "stats": alerts.stats(ctxd["workspace_id"]),
    }


@router.post("/alerts/emit")
def emit_event(payload: EmitIn, ctxd: dict = Depends(ctx)) -> dict:
    """手动触发一次告警事件，验证「规则 → 渠道 → 投递」整条链路。"""
    try:
        result = alerts.emit(
            ctxd["workspace_id"],
            payload.event,
            payload.subject,
            payload.body,
            payload.detail,
            severity=payload.severity,
            force=payload.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


# ================================================================ OPS 四层视图


@router.get("/ops/levels")
def ops_levels(
    depth: str = Query("all", description="platform / workspace / resource / task / all"),
    workspace_id: str | None = None,
    ctxd: dict = Depends(ctx),
) -> dict:
    """四层视图树：平台 → 工作空间 → 资源 → 任务。"""
    ws = workspace_id or ctxd["workspace_id"]
    if workspace_id:
        # 指定了别的工作空间：确认该用户确实属于它，避免越权读别人的运行数据
        member = store.row(
            "SELECT 1 AS ok FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, ctxd["user"]["id"]),
        )
        if not member and ctxd["user"].get("role") != "admin":
            raise HTTPException(status_code=403, detail="无权访问该工作空间")
    return opsview.levels_tree(ws, depth)


@router.get("/ops/diagnose")
def ops_diagnose(
    scope: str = Query("platform", description="platform / workspace / flow:<id> / task:<id>"),
    ctxd: dict = Depends(ctx),
) -> dict:
    """AI 诊断：基于规则推理给出根因与建议（不调用外部 LLM）。"""
    return opsview.diagnose(ctxd["workspace_id"], scope)


@router.get("/ops/overview")
def ops_overview(ctxd: dict = Depends(ctx)) -> dict:
    """OPS 首屏聚合：健康度 + 关键计数 + 最近任务 + 告警统计。"""
    data = opsview.overview(ctxd["workspace_id"])
    data["alerts"] = alerts.stats(ctxd["workspace_id"])
    return data


@router.get("/ops/tree-flat")
def ops_tree_flat(
    depth: str = Query("all"),
    ctxd: dict = Depends(ctx),
) -> dict:
    """拍平的四层视图，便于前端做表格视图或断言。"""
    tree = opsview.levels_tree(ctxd["workspace_id"], depth)["tree"]
    return {"nodes": opsview.flatten(tree), "levels": opsview.LEVELS}


# ================================================================ Agent 安全


@router.get("/security/policy")
def security_policy(ctxd: dict = Depends(ctx)) -> dict:
    """安全策略总览：双向检查项 / 三要素 / 工具分级 / OWASP 覆盖。"""
    return guardrails_ext.policy_overview(ctxd["workspace_id"])


@router.post("/security/check-input")
def check_input(payload: CheckTextIn, ctxd: dict = Depends(ctx)) -> dict:
    """输入侧 Guardrails 在线检测：提示注入 / 越权 / 高危 SQL 意图。"""
    context = dict(payload.context or {})
    context.setdefault("user", ctxd["user"])
    result = guardrails_ext.check_input(payload.text, context)
    if result["action"] == "block":
        store.audit(
            ctxd["workspace_id"], ctxd["user"]["id"], "guardrail_block_input",
            "security/check-input",
            {"findings": [f["category"] for f in result["findings"]][:5]},
        )
    return result


@router.post("/security/check-output")
def check_output(payload: CheckTextIn, ctxd: dict = Depends(ctx)) -> dict:
    """输出侧 Guardrails 在线检测：PII 泄漏 / 幻觉 / 有害内容，返回脱敏后文本。"""
    context = dict(payload.context or {})
    context.setdefault("user", ctxd["user"])
    result = guardrails_ext.check_output(payload.text, context)
    if result["action"] in ("block", "mask"):
        store.audit(
            ctxd["workspace_id"], ctxd["user"]["id"], "guardrail_mask_output",
            "security/check-output",
            {"action": result["action"], "kinds": [p["kind"] for p in result["pii"]]},
        )
    return result


@router.post("/security/assess-sql")
def assess_sql(payload: AssessSqlIn, ctxd: dict = Depends(ctx)) -> dict:
    """高危 SQL 评估器：返回 risk（low/medium/high）+ 理由列表。"""
    result = guardrails_ext.assess_sql(payload.sql)
    if result["risk"] == "high":
        store.audit(
            ctxd["workspace_id"], ctxd["user"]["id"], "assess_high_risk_sql",
            result["statement"] or "sql", {"reasons": result["reasons"][:5]},
        )
    return result


@router.post("/security/assess-tool")
def assess_tool(payload: AssessToolIn, ctxd: dict = Depends(ctx)) -> dict:
    """工具调用评估：风险等级 / 三要素 / OBO / 是否需要人工审批。"""
    context = dict(payload.context or {})
    context.setdefault("user", ctxd["user"])
    return guardrails_ext.assess_tool_call(payload.tool, payload.args, context)


@router.get("/security/owasp")
def owasp(ctxd: dict = Depends(ctx)) -> dict:
    """OWASP LLM Top10 检查清单（10 项，含覆盖情况与本平台机制）。"""
    return guardrails_ext.owasp_summary()
