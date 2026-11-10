"""工作流引擎接口：工作流 CRUD / 运行 / 运行明细 / 元数据 / 启停。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import store, workflow
from .auth import ctx

router = APIRouter(prefix="/api", tags=["workflow"])


# ------------------------------------------------------------------ 入参模型


class NodeIn(BaseModel):
    id: str | None = None
    type: str = "sql"
    name: str = ""
    config: dict = {}
    params: dict = {}
    condition: str = "all_success"
    condition_config: dict = {}
    retries: int | None = None


class WorkflowIn(BaseModel):
    name: str
    description: str = ""
    cron: str = ""
    enabled: bool = False
    nodes: list[NodeIn] = []
    edges: list[list[str]] = []
    params: dict = {}
    retries: int = 0
    run_mode: str = "cron"
    interval_seconds: int = 60
    timezone: str = ""


class WorkflowPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    cron: str | None = None
    enabled: bool | None = None
    nodes: list[NodeIn] | None = None
    edges: list[list[str]] | None = None
    params: dict | None = None
    retries: int | None = None
    run_mode: str | None = None
    interval_seconds: int | None = None
    timezone: str | None = None


class RunIn(BaseModel):
    params: dict = {}


# ------------------------------------------------------------------ 元数据


@router.get("/workflow-meta")
def workflow_meta(ctxd: dict = Depends(ctx)) -> dict:
    """前端选择器用：9 种节点类型 + 12 种运行条件 + 运行模式。"""
    data = workflow.meta()
    data["flows"] = [
        {"id": f["id"], "name": f["name"]}
        for f in workflow.list_flows(ctxd["workspace_id"])
    ]
    return data


# ------------------------------------------------------------------ 工作流 CRUD


@router.get("/workflows")
def list_workflows(ctxd: dict = Depends(ctx)) -> dict:
    return {"workflows": workflow.list_flows(ctxd["workspace_id"])}


@router.post("/workflows")
def create_workflow(payload: WorkflowIn, ctxd: dict = Depends(ctx)) -> dict:
    nodes = [n.model_dump() for n in payload.nodes]
    if not nodes:
        raise HTTPException(status_code=400, detail="至少需要一个节点")
    try:
        flow = workflow.create_flow(
            workspace_id=ctxd["workspace_id"],
            name=payload.name,
            description=payload.description,
            cron=payload.cron,
            enabled=payload.enabled,
            nodes=nodes,
            edges=payload.edges,
            params=payload.params,
            retries=payload.retries,
            run_mode=payload.run_mode,
            interval_seconds=payload.interval_seconds,
            timezone=payload.timezone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_workflow", payload.name)
    return {"workflow": flow}


@router.get("/workflows/{flow_id}")
def get_workflow(flow_id: str, ctxd: dict = Depends(ctx)) -> dict:
    flow = workflow.get_flow(flow_id, ctxd["workspace_id"])
    if not flow:
        raise HTTPException(status_code=404, detail="工作流不存在")
    return {"workflow": flow}


@router.patch("/workflows/{flow_id}")
def update_workflow(flow_id: str, payload: WorkflowPatch, ctxd: dict = Depends(ctx)) -> dict:
    data = payload.model_dump(exclude_unset=True)
    if "nodes" in data and data["nodes"] is not None:
        data["nodes"] = [n.model_dump() if hasattr(n, "model_dump") else n for n in data["nodes"]]
    flow = workflow.update_flow(flow_id, ctxd["workspace_id"], data)
    if not flow:
        raise HTTPException(status_code=404, detail="工作流不存在")
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "update_workflow", flow_id)
    return {"workflow": flow}


@router.delete("/workflows/{flow_id}")
def delete_workflow(flow_id: str, ctxd: dict = Depends(ctx)) -> dict:
    if not workflow.delete_flow(flow_id, ctxd["workspace_id"]):
        raise HTTPException(status_code=404, detail="工作流不存在")
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "delete_workflow", flow_id)
    return {"ok": True}


# ------------------------------------------------------------------ 运行


@router.post("/workflows/{flow_id}/run")
def run_workflow(flow_id: str, payload: RunIn | None = None, ctxd: dict = Depends(ctx)) -> dict:
    flow = workflow.get_flow(flow_id, ctxd["workspace_id"])
    if not flow:
        raise HTTPException(status_code=404, detail="工作流不存在")
    params = (payload.params if payload else {}) or {}
    try:
        result = workflow.run_flow_by_id(flow_id, trigger="manual", params=params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(
        ctxd["workspace_id"],
        ctxd["user"]["id"],
        "run_workflow",
        flow.get("name") or flow_id,
        {"status": result.get("status"), "run_id": result.get("id")},
    )
    return {"run": result}


@router.get("/workflows/{flow_id}/runs")
def workflow_runs(flow_id: str, limit: int = 20, ctxd: dict = Depends(ctx)) -> dict:
    flow = store.row(
        "SELECT id FROM flows WHERE id = ? AND workspace_id = ?", (flow_id, ctxd["workspace_id"])
    )
    if not flow:
        raise HTTPException(status_code=404, detail="工作流不存在")
    return {"runs": workflow.list_runs(flow_id, limit=limit)}


@router.get("/workflow-runs/{run_id}")
def workflow_run_detail(run_id: str, ctxd: dict = Depends(ctx)) -> dict:
    detail = workflow.run_detail(run_id)
    if not detail:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    flow = store.row(
        "SELECT id FROM flows WHERE id = ? AND workspace_id = ?",
        (detail.get("flow_id"), ctxd["workspace_id"]),
    )
    if not flow:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return {"run": detail}


# ------------------------------------------------------------------ 启停


@router.post("/workflows/{flow_id}/pause")
def pause_workflow(flow_id: str, ctxd: dict = Depends(ctx)) -> dict:
    flow = workflow.set_enabled(flow_id, ctxd["workspace_id"], False)
    if not flow:
        raise HTTPException(status_code=404, detail="工作流不存在")
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "pause_workflow", flow_id)
    return {"ok": True, "workflow": flow}


@router.post("/workflows/{flow_id}/resume")
def resume_workflow(flow_id: str, ctxd: dict = Depends(ctx)) -> dict:
    flow = workflow.set_enabled(flow_id, ctxd["workspace_id"], True)
    if not flow:
        raise HTTPException(status_code=404, detail="工作流不存在")
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "resume_workflow", flow_id)
    return {"ok": True, "workflow": flow}


# ------------------------------------------------------------------ 条件试算


@router.post("/workflows/{flow_id}/validate")
def validate_workflow(flow_id: str, ctxd: dict = Depends(ctx)) -> dict:
    flow = workflow.get_flow(flow_id, ctxd["workspace_id"])
    if not flow:
        raise HTTPException(status_code=404, detail="工作流不存在")
    issues: list[dict] = []
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    ids = {n["id"] for n in nodes}
    if not nodes:
        issues.append({"level": "error", "message": "工作流没有节点"})
    if flow.get("has_cycle"):
        issues.append({"level": "error", "message": "存在环，无法执行"})
    for edge in edges:
        if edge[0] not in ids or edge[1] not in ids:
            issues.append({"level": "warning", "message": f"边 {edge[0]}→{edge[1]} 引用了不存在的节点"})
    for node in nodes:
        kind = node.get("type")
        if kind not in workflow.NODE_TYPES:
            issues.append({"level": "error", "message": f"节点「{node.get('name')}」类型 {kind} 不受支持"})
        if node.get("condition") not in workflow.CONDITIONS:
            issues.append({"level": "warning", "message": f"节点「{node.get('name')}」条件 {node.get('condition')} 未知"})
        cfg = node.get("config") or {}
        if kind == "sql" and not str(cfg.get("sql") or "").strip():
            issues.append({"level": "error", "message": f"节点「{node.get('name')}」缺少 SQL"})
        if kind == "shell" and not str(cfg.get("command") or "").strip():
            issues.append({"level": "error", "message": f"节点「{node.get('name')}」缺少命令"})
        if kind == "quality" and not str(cfg.get("table_ref") or "").strip():
            issues.append({"level": "warning", "message": f"节点「{node.get('name')}」未配置 table_ref"})
    return {"ok": not any(i["level"] == "error" for i in issues), "issues": issues}


# ------------------------------------------------------------------ 调度预览


@router.get("/workflow-schedule")
def workflow_schedule(ctxd: dict = Depends(ctx)) -> dict:
    """当前调度概览：已启用工作流、下次触发说明、时区解析结果。"""
    rows = workflow.list_flows(ctxd["workspace_id"])
    items = []
    for row in rows:
        tz = row.get("timezone") or ""
        resolved = workflow.resolve_tz(tz)
        items.append(
            {
                "id": row["id"],
                "name": row["name"],
                "enabled": row["enabled"],
                "run_mode": row["run_mode"],
                "cron": row["cron"],
                "timezone": tz,
                "timezone_resolved": str(resolved) if resolved else ("本地时区" if not tz else "无法解析，按本地时区处理"),
                "schedule_text": row["schedule_text"],
                "interval_seconds": row["interval_seconds"],
            }
        )
    return {
        "items": items,
        "enabled_count": sum(1 for i in items if i["enabled"]),
        "min_continuous_seconds": workflow.MIN_CONTINUOUS_SECONDS,
    }
