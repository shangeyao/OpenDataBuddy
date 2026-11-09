"""运维与治理接口：工作流、仪表盘、治理、总览。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import catalog, charts, flows, governance, store
from ..harness import providers
from ..harness import trace as trace_mod
from .auth import ctx

router = APIRouter(prefix="/api", tags=["ops"])


# ------------------------------------------------------------------- 工作流


@router.get("/flows")
def list_flows(ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        "SELECT * FROM flows WHERE workspace_id = ? ORDER BY created_at DESC", (ctxd["workspace_id"],)
    )
    out = []
    for r in rows:
        last = store.row(
            "SELECT status, started_at FROM flow_runs WHERE flow_id = ? ORDER BY started_at DESC LIMIT 1",
            (r["id"],),
        )
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "description": r["description"],
                "cron": r["cron"],
                "enabled": bool(r["enabled"]),
                "schedule_text": flows.describe_cron(r.get("cron") or ""),
                "nodes": len(json.loads(r.get("nodes") or "[]")),
                "last_run": last,
            }
        )
    return {"flows": out}


@router.get("/flows/{flow_id}")
def get_flow(flow_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM flows WHERE id = ? AND workspace_id = ?", (flow_id, ctxd["workspace_id"])
    )
    if not row:
        raise HTTPException(status_code=404, detail="工作流不存在")
    return {"flow": flows.flow_detail(row)}


class NodeIn(BaseModel):
    id: str | None = None
    type: str = "sql"
    name: str
    sql: str = ""
    url: str = ""


class FlowIn(BaseModel):
    name: str
    description: str = ""
    cron: str = ""
    enabled: bool = False
    nodes: list[NodeIn] = []
    edges: list[list[str]] = []


def _nodes_payload(nodes: list[NodeIn]) -> tuple[list[dict], list[list[str]]]:
    out = []
    for i, n in enumerate(nodes):
        nid = n.id or f"n{i + 1}"
        cfg = {"sql": n.sql} if n.type in ("sql", "quality") else {"url": n.url}
        out.append({"id": nid, "type": n.type, "name": n.name, "config": cfg})
    return out, [[out[i]["id"], out[i + 1]["id"]] for i in range(len(out) - 1)]


@router.post("/flows")
def create_flow(payload: FlowIn, ctxd: dict = Depends(ctx)) -> dict:
    nodes, chain = _nodes_payload(payload.nodes)
    if not nodes:
        raise HTTPException(status_code=400, detail="至少需要一个节点")
    edges = payload.edges or chain
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "name": payload.name,
        "description": payload.description,
        "nodes": json.dumps(nodes, ensure_ascii=False),
        "edges": json.dumps(edges, ensure_ascii=False),
        "cron": payload.cron or None,
        "enabled": 1 if payload.enabled and payload.cron else 0,
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("flows", row)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_flow", payload.name)
    return {"flow": row}


@router.patch("/flows/{flow_id}")
def update_flow(flow_id: str, payload: FlowIn, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM flows WHERE id = ? AND workspace_id = ?", (flow_id, ctxd["workspace_id"])
    )
    if not row:
        raise HTTPException(status_code=404, detail="工作流不存在")
    nodes, chain = _nodes_payload(payload.nodes) if payload.nodes else (
        json.loads(row["nodes"]), json.loads(row["edges"])
    )
    store.update(
        "flows",
        flow_id,
        {
            "name": payload.name,
            "description": payload.description,
            "nodes": json.dumps(nodes, ensure_ascii=False),
            "edges": json.dumps(payload.edges or chain, ensure_ascii=False),
            "cron": payload.cron or None,
            "enabled": 1 if payload.enabled and payload.cron else 0,
            "updated_at": store.now(),
        },
    )
    return {"ok": True}


@router.delete("/flows/{flow_id}")
def delete_flow(flow_id: str, ctxd: dict = Depends(ctx)) -> dict:
    store.execute(
        "DELETE FROM flows WHERE id = ? AND workspace_id = ?", (flow_id, ctxd["workspace_id"])
    )
    store.execute("DELETE FROM flow_runs WHERE flow_id = ?", (flow_id,))
    return {"ok": True}


@router.post("/flows/{flow_id}/run")
def run_flow(flow_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM flows WHERE id = ? AND workspace_id = ?", (flow_id, ctxd["workspace_id"])
    )
    if not row:
        raise HTTPException(status_code=404, detail="工作流不存在")
    try:
        result = flows.run_flow_by_id(flow_id, trigger="manual")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "run_flow", row["name"], {"status": result["status"]})
    return {"run": result}


@router.get("/flows/{flow_id}/runs")
def flow_runs(flow_id: str, ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        "SELECT * FROM flow_runs WHERE flow_id = ? ORDER BY started_at DESC LIMIT 20", (flow_id,)
    )
    for r in rows:
        r["log"] = json.loads(r.get("log") or "[]")
    return {"runs": rows}


# --------------------------------------------------------------------- 看板


class DashboardIn(BaseModel):
    name: str
    description: str = ""
    cards: list[dict]


@router.get("/dashboards")
def list_dashboards(ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        "SELECT * FROM dashboards WHERE workspace_id = ? ORDER BY created_at DESC", (ctxd["workspace_id"],)
    )
    for r in rows:
        spec = json.loads(r.get("spec") or "{}")
        r["card_count"] = len(spec.get("cards", []))
        r.pop("spec", None)
    return {"dashboards": rows}


@router.post("/dashboards")
def create_dashboard(payload: DashboardIn, ctxd: dict = Depends(ctx)) -> dict:
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "name": payload.name,
        "description": payload.description,
        "spec": json.dumps({"cards": payload.cards}, ensure_ascii=False),
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("dashboards", row)
    return {"dashboard": row}


@router.get("/dashboards/{board_id}")
def render_dashboard(board_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM dashboards WHERE id = ? AND workspace_id = ?", (board_id, ctxd["workspace_id"])
    )
    if not row:
        raise HTTPException(status_code=404, detail="看板不存在")
    spec = json.loads(row.get("spec") or "{}")
    cards = charts.render_dashboard(ctxd["workspace_id"], spec)
    return {"dashboard": {**row, "spec": spec}, "cards": cards}


@router.delete("/dashboards/{board_id}")
def delete_dashboard(board_id: str, ctxd: dict = Depends(ctx)) -> dict:
    store.execute(
        "DELETE FROM dashboards WHERE id = ? AND workspace_id = ?", (board_id, ctxd["workspace_id"])
    )
    return {"ok": True}


# --------------------------------------------------------------------- 治理


class ScanIn(BaseModel):
    table: str


@router.post("/governance/scan")
def scan(payload: ScanIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        return governance.scan_table(ctxd["workspace_id"], payload.table)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class TagsIn(BaseModel):
    table: str
    findings: list[dict] = []


@router.post("/governance/tags")
def tag_columns(payload: TagsIn, ctxd: dict = Depends(ctx)) -> dict:
    findings = payload.findings
    if not findings:
        findings = governance.scan_table(ctxd["workspace_id"], payload.table)["findings"]
    applied = governance.apply_tags(ctxd["workspace_id"], payload.table, findings)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "apply_tags", payload.table, {"applied": applied})
    return {"applied": applied}


@router.get("/governance/policies")
def policies(ctxd: dict = Depends(ctx)) -> dict:
    return {"policies": governance.policy_suggestions(ctxd["workspace_id"])}


@router.get("/governance/quality")
def list_quality(table: str | None = None, ctxd: dict = Depends(ctx)) -> dict:
    if table:
        detail = catalog.resolve(ctxd["workspace_id"], table)
        rows = store.rows(
            "SELECT * FROM quality_rules WHERE workspace_id = ? AND table_ref = ?",
            (ctxd["workspace_id"], detail["ref"] if detail else table),
        )
    else:
        rows = store.rows(
            "SELECT * FROM quality_rules WHERE workspace_id = ? ORDER BY created_at DESC",
            (ctxd["workspace_id"],),
        )
    return {"rules": rows}


class RuleIn(BaseModel):
    table: str
    column: str = ""
    rule_type: str
    config: dict = {}


@router.post("/governance/quality")
def create_rule(payload: RuleIn, ctxd: dict = Depends(ctx)) -> dict:
    detail = catalog.resolve(ctxd["workspace_id"], payload.table)
    if not detail:
        raise HTTPException(status_code=404, detail="表不存在")
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "table_ref": detail["ref"],
        "column_name": payload.column or None,
        "rule_type": payload.rule_type,
        "config": json.dumps(payload.config, ensure_ascii=False),
        "enabled": 1,
        "created_at": store.now(),
    }
    store.insert("quality_rules", row)
    return {"rule": row}


@router.post("/governance/quality/{rule_id}/run")
def run_rule(rule_id: str, ctxd: dict = Depends(ctx)) -> dict:
    rule = store.row(
        "SELECT * FROM quality_rules WHERE id = ? AND workspace_id = ?", (rule_id, ctxd["workspace_id"])
    )
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    try:
        return governance.run_rule(ctxd["workspace_id"], rule)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/governance/quality/run-all")
def run_all(ctxd: dict = Depends(ctx)) -> dict:
    rules = store.rows(
        "SELECT * FROM quality_rules WHERE workspace_id = ? AND enabled = 1", (ctxd["workspace_id"],)
    )
    results = []
    for rule in rules:
        try:
            res = governance.run_rule(ctxd["workspace_id"], rule)
        except Exception as exc:  # noqa: BLE001
            res = {"status": "error", "value": "", "detail": str(exc)}
        results.append({"table": rule["table_ref"], "column": rule["column_name"],
                        "rule_type": rule["rule_type"], **res})
    passed = sum(1 for r in results if r["status"] == "pass")
    return {"total": len(results), "passed": passed, "results": results}


@router.delete("/governance/quality/{rule_id}")
def delete_rule(rule_id: str, ctxd: dict = Depends(ctx)) -> dict:
    store.execute(
        "DELETE FROM quality_rules WHERE id = ? AND workspace_id = ?", (rule_id, ctxd["workspace_id"])
    )
    return {"ok": True}


@router.post("/governance/profile")
def profile(payload: ScanIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        return governance.profile(ctxd["workspace_id"], payload.table)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/governance/tags")
def all_tags(ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        """
        SELECT ca.name || '.' || s.name || '.' || t.name AS table_ref, c.name AS column_name,
               ct.tag, ct.level, ct.confidence, ct.source
        FROM column_tags ct
        JOIN columns c ON c.id = ct.column_id
        JOIN tables t ON t.id = c.table_id
        JOIN schemas s ON s.id = t.schema_id
        JOIN catalogs ca ON ca.id = s.catalog_id
        WHERE ca.workspace_id = ?
        ORDER BY ct.level DESC, table_ref
        """,
        (ctxd["workspace_id"],),
    )
    return {"tags": rows}


# --------------------------------------------------------------------- 总览


@router.get("/overview")
def overview(ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    tables = catalog.all_tables(ws)
    metrics = store.rows("SELECT count(*) AS c FROM metrics WHERE workspace_id = ?", (ws,))[0]["c"]
    flows_rows = store.rows("SELECT id, name, enabled, cron FROM flows WHERE workspace_id = ?", (ws,))
    recent_runs = store.rows(
        """
        SELECT r.id, r.status, r.trigger, r.started_at, f.name AS flow_name
        FROM flow_runs r JOIN flows f ON f.id = r.flow_id
        WHERE f.workspace_id = ? ORDER BY r.started_at DESC LIMIT 6
        """,
        (ws,),
    )
    tag_count = store.scalar(
        """
        SELECT count(*) FROM column_tags ct
        JOIN columns c ON c.id = ct.column_id
        JOIN tables t ON t.id = c.table_id
        JOIN schemas s ON s.id = t.schema_id
        JOIN catalogs ca ON ca.id = s.catalog_id
        WHERE ca.workspace_id = ?
        """,
        (ws,),
        0,
    )
    rules = store.rows("SELECT last_status FROM quality_rules WHERE workspace_id = ?", (ws,))
    evaluated = [r for r in rules if r["last_status"]]
    conv_count = store.scalar(
        "SELECT count(*) FROM conversations WHERE workspace_id = ?", (ws,), 0
    )
    boards = store.scalar("SELECT count(*) FROM dashboards WHERE workspace_id = ?", (ws,), 0)

    return {
        "stats": {
            "tables": len(tables),
            "rows": sum(t["row_count"] or 0 for t in tables),
            "metrics": metrics,
            "flows": len(flows_rows),
            "enabled_flows": sum(1 for f in flows_rows if f["enabled"]),
            "sensitive_columns": tag_count,
            "quality_pass_rate": (
                round(sum(1 for r in evaluated if r["last_status"] == "pass") / len(evaluated), 3)
                if evaluated
                else None
            ),
            "conversations": conv_count,
            "dashboards": boards,
        },
        "recent_runs": recent_runs,
        "trace": trace_mod.stats(ws),
        "tables": tables[:6],
        "llm_ready": providers.is_configured(),
    }
