"""W8 数据分析接口：智能问数 / 归因 / AI 报告 / AI 看板 / 报告定时推送 / 分析空间嵌出。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import analysis, reports, semantic, store
from .auth import ctx

router = APIRouter(prefix="/api", tags=["analysis"])


# ------------------------------------------------------------------ 元信息


@router.get("/analysis/meta")
def analysis_meta(ctxd: dict = Depends(ctx)) -> dict:
    """前端渲染下拉所需：指标、维度、周期。"""
    metrics = semantic.list_metrics(ctxd["workspace_id"])
    dims: list[str] = []
    for m in metrics:
        for d in m.get("dimensions") or []:
            if d not in dims:
                dims.append(d)
    return {
        "metrics": [
            {
                "name": m["name"],
                "display_name": m.get("display_name") or m["name"],
                "unit": m.get("unit") or "",
                "expr": m.get("expr") or "",
                "table_ref": m.get("table_ref") or "",
                "dimensions": m.get("dimensions") or [],
            }
            for m in metrics
        ],
        "dimensions": [
            {"name": d, "label": analysis.DIM_LABELS.get(d, d)} for d in dims
        ],
        "periods": analysis.period_options(),
        "summary": reports.summary(ctxd["workspace_id"]),
    }


# ------------------------------------------------------------------ 智能问数


class AskIn(BaseModel):
    question: str
    period: str | None = None
    limit: int = 200


@router.post("/analysis/ask")
def ask(payload: AskIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        out = analysis.ask(
            ctxd["workspace_id"], payload.question, payload.period, limit=payload.limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"问数失败：{exc}") from exc
    store.audit(
        ctxd["workspace_id"], ctxd["user"]["id"], "analysis_ask", payload.question[:120],
        {"engine": out.get("engine"), "rows": out.get("row_count")},
    )
    return out


class AttributeIn(BaseModel):
    metric: str
    dimension: str | None = None
    period: str = "last_month"
    top_n: int = 8


@router.post("/analysis/attribute")
def attribute(payload: AttributeIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        out = analysis.attribute(
            ctxd["workspace_id"],
            payload.metric,
            payload.dimension,
            payload.period,
            top_n=payload.top_n,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"归因失败：{exc}") from exc
    store.audit(
        ctxd["workspace_id"], ctxd["user"]["id"], "analysis_attribute", payload.metric,
        {"dimension": out.get("dimension"), "driver": out.get("driver")},
    )
    return out


# ------------------------------------------------------------------ AI 报告


class ReportGenerateIn(BaseModel):
    topic: str = "经营分析"
    metrics: list[str] = []
    period: str = "last_month"


@router.post("/analysis/report/generate")
def generate_report(payload: ReportGenerateIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        out = analysis.generate_report(
            ctxd["workspace_id"], payload.topic, payload.metrics, payload.period
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"报告生成失败：{exc}") from exc
    store.audit(
        ctxd["workspace_id"], ctxd["user"]["id"], "generate_report", payload.topic,
        {"sections": len(out.get("sections") or [])},
    )
    return out


# ------------------------------------------------------------------ AI 看板


class DashboardGenerateIn(BaseModel):
    prompt: str
    period: str | None = None


@router.post("/analysis/dashboard/generate")
def generate_dashboard(payload: DashboardGenerateIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        out = analysis.generate_dashboard(ctxd["workspace_id"], payload.prompt, payload.period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"看板生成失败：{exc}") from exc
    store.audit(
        ctxd["workspace_id"], ctxd["user"]["id"], "generate_dashboard", payload.prompt[:120],
        {"cards": len(out.get("cards") or [])},
    )
    return out


# ------------------------------------------------------------------ 报告 CRUD


class ReportIn(BaseModel):
    name: str
    topic: str = ""
    description: str = ""
    metrics: list[str] = []
    period: str = "last_month"
    sections: list[dict] | None = None
    summary: str = ""


class ReportPatchIn(BaseModel):
    name: str | None = None
    topic: str | None = None
    description: str | None = None
    metrics: list[str] | None = None
    period: str | None = None
    sections: list[dict] | None = None
    summary: str | None = None
    status: str | None = None


@router.get("/reports")
def list_reports(ctxd: dict = Depends(ctx)) -> dict:
    return {
        "reports": reports.list_reports(ctxd["workspace_id"]),
        "summary": reports.summary(ctxd["workspace_id"]),
    }


@router.post("/reports")
def create_report(payload: ReportIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        row = reports.create_report(
            ctxd["workspace_id"], payload.model_dump(), owner=ctxd["user"]["name"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_report", row["name"])
    return {"report": row}


@router.get("/reports/{report_id}")
def get_report(report_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = reports.get_report(ctxd["workspace_id"], report_id)
    if not row:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"report": row}


@router.patch("/reports/{report_id}")
def update_report(report_id: str, payload: ReportPatchIn, ctxd: dict = Depends(ctx)) -> dict:
    row = reports.update_report(
        ctxd["workspace_id"], report_id, payload.model_dump(exclude_none=True)
    )
    if not row:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"report": row}


@router.delete("/reports/{report_id}")
def delete_report(report_id: str, ctxd: dict = Depends(ctx)) -> dict:
    if not reports.delete_report(ctxd["workspace_id"], report_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "delete_report", report_id)
    return {"ok": True}


class RunIn(BaseModel):
    channels: list[dict] | None = None


@router.post("/reports/{report_id}/run")
def run_report(report_id: str, payload: RunIn | None = None, ctxd: dict = Depends(ctx)) -> dict:
    try:
        out = reports.run_report(
            ctxd["workspace_id"],
            report_id,
            trigger="manual",
            channels=(payload.channels if payload else None),
            user_id=ctxd["user"]["id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if out.get("status") == "failed":
        raise HTTPException(status_code=400, detail=f"报告生成失败：{out.get('error')}")
    return {"run": out}


@router.get("/reports/{report_id}/runs")
def list_report_runs(report_id: str, ctxd: dict = Depends(ctx)) -> dict:
    return {"runs": reports.list_runs(ctxd["workspace_id"], report_id)}


# ------------------------------------------------------------------ 定时推送


class ScheduleIn(BaseModel):
    report_id: str
    cron: str = "0 9 * * *"
    channels: list[dict] = []
    enabled: bool = True


class SchedulePatchIn(BaseModel):
    cron: str | None = None
    channels: list[dict] | None = None
    enabled: bool | None = None


@router.get("/report-schedules")
def list_schedules(ctxd: dict = Depends(ctx)) -> dict:
    return {"schedules": reports.list_schedules(ctxd["workspace_id"])}


@router.post("/report-schedules")
def create_schedule(payload: ScheduleIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        row = reports.create_schedule(ctxd["workspace_id"], payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"schedule": row}


@router.patch("/report-schedules/{schedule_id}")
def update_schedule(schedule_id: str, payload: SchedulePatchIn, ctxd: dict = Depends(ctx)) -> dict:
    row = reports.update_schedule(
        ctxd["workspace_id"], schedule_id, payload.model_dump(exclude_none=True)
    )
    if not row:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return {"schedule": row}


@router.delete("/report-schedules/{schedule_id}")
def delete_schedule(schedule_id: str, ctxd: dict = Depends(ctx)) -> dict:
    if not reports.delete_schedule(ctxd["workspace_id"], schedule_id):
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return {"ok": True}


# ------------------------------------------------------------------ 分析空间嵌出


@router.get("/analysis-spaces/{space_id}/embed")
def get_embed(space_id: str, ctxd: dict = Depends(ctx)) -> dict:
    try:
        out = reports.get_embed(ctxd["workspace_id"], space_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "embed_space", space_id)
    return {"embed": out}


@router.post("/analysis-spaces/{space_id}/embed")
def rotate_embed(
    space_id: str,
    rotate: bool = Query(True),
    ctxd: dict = Depends(ctx),
) -> dict:
    """轮换嵌出 token（旧 token 不自动撤销，需要显式 revoke）。"""
    space = store.row(
        "SELECT id FROM analysis_spaces WHERE id = ? AND workspace_id = ?",
        (space_id, ctxd["workspace_id"]),
    )
    if not space:
        raise HTTPException(status_code=404, detail="分析空间不存在")
    return {"embed": reports.create_embed(ctxd["workspace_id"], space_id, {"rotate": rotate})}


@router.delete("/analysis-spaces/{space_id}/embed")
def revoke_embed(space_id: str, ctxd: dict = Depends(ctx)) -> dict:
    if not reports.revoke_embed(ctxd["workspace_id"], space_id):
        raise HTTPException(status_code=404, detail="没有可撤销的嵌出配置")
    return {"ok": True}


@router.get("/embed/{token}")
def resolve_embed(token: str) -> dict:
    """只读嵌出渲染入口：凭 token 取空间配置（iframe 内使用）。"""
    out = reports.resolve_embed_token(token)
    if not out:
        raise HTTPException(status_code=404, detail="嵌出链接无效或已撤销")
    return {"embed": out}
