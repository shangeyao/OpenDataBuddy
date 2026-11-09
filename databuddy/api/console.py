"""平台级接口：计算资源、模型、智能体与应用、Buddy 定时任务、本地用量。

这些模块在原产品里是云上资源（集群、MLOps、Agent 席位），
开源版全部落成本地实体：能建能改能启停，但不产生任何费用。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .. import catalog, config, store
from ..harness import providers
from .auth import ctx

router = APIRouter(prefix="/api", tags=["console"])

CLUSTER_TYPES = {
    "job": {"label": "作业集群", "engine": "Spark", "use": "Notebook / Python / SQL 任务"},
    "interactive": {"label": "交互式集群", "engine": "Spark", "use": "SQL 探索 / 仪表盘 / 问数"},
    "platform": {"label": "平台集群", "engine": "平台服务", "use": "离线接入 / 连通性测试"},
    "realtime": {"label": "实时集群", "engine": "Flink", "use": "实时接入"},
}


# ---------------------------------------------------------------- 工作空间总览


@router.get("/console/workspaces")
def console_workspaces(ctxd: dict = Depends(ctx)) -> dict:
    user = ctxd["user"]
    rows = store.rows(
        """
        SELECT w.*, m.role FROM workspaces w
        JOIN workspace_members m ON m.workspace_id = w.id AND m.user_id = ?
        ORDER BY w.created_at
        """,
        (user["id"],),
    )
    if not rows and user.get("role") == "admin":
        rows = store.rows("SELECT w.*, 'admin' AS role FROM workspaces w ORDER BY w.created_at")
    out = []
    for w in rows:
        tables = store.scalar(
            """
            SELECT count(*) FROM tables t
            JOIN schemas s ON s.id = t.schema_id
            JOIN catalogs c ON c.id = s.catalog_id
            WHERE c.workspace_id = ?
            """,
            (w["id"],),
            0,
        )
        rows_count = store.scalar(
            """
            SELECT coalesce(sum(t.row_count), 0) FROM tables t
            JOIN schemas s ON s.id = t.schema_id
            JOIN catalogs c ON c.id = s.catalog_id
            WHERE c.workspace_id = ?
            """,
            (w["id"],),
            0,
        )
        members = store.scalar(
            "SELECT count(*) FROM workspace_members WHERE workspace_id = ?", (w["id"],), 0
        )
        out.append(
            {
                "id": w["id"],
                "name": w["name"],
                "description": w["description"],
                "role": w.get("role") or "member",
                "tables": tables,
                "rows": rows_count,
                "members": members,
                "status": "运行中",
                "created_at": w["created_at"],
            }
        )
    return {"workspaces": out}


# ---------------------------------------------------------------- 计算资源


class ResourceIn(BaseModel):
    name: str
    description: str = ""
    type: str = "job"
    engine: str = ""
    compute_type: str = "CPU"
    image: str = "Standard-S 1.1"
    cu: int = 64
    network: str = ""


@router.get("/compute-resources")
def list_resources(ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        "SELECT * FROM compute_resources WHERE workspace_id = ? ORDER BY created_at",
        (ctxd["workspace_id"],),
    )
    for r in rows:
        meta = CLUSTER_TYPES.get(r["type"], {})
        r["type_label"] = meta.get("label", r["type"])
        r["usage"] = meta.get("use", "")
    summary = {
        "count": len(rows),
        "running": sum(1 for r in rows if r["status"] == "running"),
        "total_cu": sum(int(r["cu"] or 0) for r in rows),
    }
    return {"resources": rows, "summary": summary, "types": CLUSTER_TYPES}


@router.post("/compute-resources")
def create_resource(payload: ResourceIn, ctxd: dict = Depends(ctx)) -> dict:
    if payload.type not in CLUSTER_TYPES:
        raise HTTPException(status_code=400, detail="资源类型不合法")
    if store.row(
        "SELECT id FROM compute_resources WHERE workspace_id = ? AND name = ?",
        (ctxd["workspace_id"], payload.name),
    ):
        raise HTTPException(status_code=400, detail="资源名称已存在")
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "name": payload.name,
        "description": payload.description,
        "type": payload.type,
        "engine": payload.engine or CLUSTER_TYPES[payload.type]["engine"],
        "compute_type": payload.compute_type,
        "image": payload.image,
        "cu": max(1, min(int(payload.cu or 64), 1024)),
        "status": "running",
        "network": payload.network,
        "created_at": store.now(),
    }
    store.insert("compute_resources", row)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_compute", payload.name)
    return {"resource": row}


@router.patch("/compute-resources/{resource_id}")
def update_resource(resource_id: str, payload: ResourceIn, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM compute_resources WHERE id = ? AND workspace_id = ?",
        (resource_id, ctxd["workspace_id"]),
    )
    if not row:
        raise HTTPException(status_code=404, detail="资源不存在")
    store.update(
        "compute_resources",
        resource_id,
        {
            "name": payload.name,
            "description": payload.description,
            "cu": max(1, min(int(payload.cu or 64), 1024)),
            "image": payload.image,
            "network": payload.network,
        },
    )
    return {"ok": True}


@router.post("/compute-resources/{resource_id}/action")
def resource_action(resource_id: str, body: dict, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM compute_resources WHERE id = ? AND workspace_id = ?",
        (resource_id, ctxd["workspace_id"]),
    )
    if not row:
        raise HTTPException(status_code=404, detail="资源不存在")
    action = (body or {}).get("action")
    if action not in ("start", "stop"):
        raise HTTPException(status_code=400, detail="action 只支持 start / stop")
    if action == "stop" and row["type"] != "interactive":
        raise HTTPException(status_code=400, detail="仅交互式集群支持手动停止，其余类型常驻运行")
    store.update("compute_resources", resource_id, {"status": "running" if action == "start" else "stopped"})
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], f"{action}_compute", row["name"])
    return {"ok": True, "status": "running" if action == "start" else "stopped"}


@router.delete("/compute-resources/{resource_id}")
def delete_resource(resource_id: str, ctxd: dict = Depends(ctx)) -> dict:
    store.execute(
        "DELETE FROM compute_resources WHERE id = ? AND workspace_id = ?",
        (resource_id, ctxd["workspace_id"]),
    )
    return {"ok": True}


# -------------------------------------------------------------------- 模型


class ExperimentIn(BaseModel):
    name: str
    framework: str = "PyTorch"
    metrics: dict = {}


class ServiceIn(BaseModel):
    name: str
    model_ref: str
    version: str = "v1"
    replicas: int = 1


@router.get("/models")
def list_models(ctxd: dict = Depends(ctx)) -> dict:
    experiments = store.rows(
        "SELECT * FROM model_experiments WHERE workspace_id = ? ORDER BY created_at DESC",
        (ctxd["workspace_id"],),
    )
    for e in experiments:
        try:
            e["metrics"] = json.loads(e.get("metrics") or "{}")
        except json.JSONDecodeError:
            e["metrics"] = {}
    services = store.rows(
        "SELECT * FROM model_services WHERE workspace_id = ? ORDER BY created_at DESC",
        (ctxd["workspace_id"],),
    )
    return {"experiments": experiments, "services": services}


@router.post("/models/experiments")
def create_experiment(payload: ExperimentIn, ctxd: dict = Depends(ctx)) -> dict:
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "name": payload.name,
        "framework": payload.framework,
        "status": "running",
        "metrics": json.dumps(payload.metrics or {}, ensure_ascii=False),
        "owner": ctxd["user"]["name"],
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("model_experiments", row)
    return {"experiment": row}


@router.post("/models/services")
def create_service(payload: ServiceIn, ctxd: dict = Depends(ctx)) -> dict:
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "name": payload.name,
        "model_ref": payload.model_ref,
        "version": payload.version,
        "replicas": payload.replicas,
        "status": "running",
        "endpoint": f"/svc/{payload.name}",
        "created_at": store.now(),
    }
    store.insert("model_services", row)
    return {"service": row}


@router.post("/models/services/{service_id}/action")
def service_action(service_id: str, body: dict, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM model_services WHERE id = ? AND workspace_id = ?",
        (service_id, ctxd["workspace_id"]),
    )
    if not row:
        raise HTTPException(status_code=404, detail="服务不存在")
    action = (body or {}).get("action")
    store.update("model_services", service_id, {"status": "running" if action == "start" else "stopped"})
    return {"ok": True}


# ------------------------------------------------------- 智能体 / 应用


class AgentIn(BaseModel):
    name: str
    mode: str = "engineering"
    description: str = ""
    prompt: str = ""
    tools: list[str] = []


class AppIn(BaseModel):
    name: str
    type: str = "chat"
    description: str = ""
    entry: str = "/buddy"


@router.get("/agents")
def list_agents(ctxd: dict = Depends(ctx)) -> dict:
    agents = store.rows(
        "SELECT * FROM agents WHERE workspace_id = ? ORDER BY created_at", (ctxd["workspace_id"],)
    )
    for a in agents:
        try:
            a["tools"] = json.loads(a.get("tools") or "[]")
        except json.JSONDecodeError:
            a["tools"] = []
    apps = store.rows(
        "SELECT * FROM apps WHERE workspace_id = ? ORDER BY created_at", (ctxd["workspace_id"],)
    )
    return {"agents": agents, "apps": apps}


@router.post("/agents")
def create_agent(payload: AgentIn, ctxd: dict = Depends(ctx)) -> dict:
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "name": payload.name,
        "mode": payload.mode,
        "description": payload.description,
        "prompt": payload.prompt,
        "tools": json.dumps(payload.tools or [], ensure_ascii=False),
        "status": "published",
        "created_at": store.now(),
    }
    store.insert("agents", row)
    return {"agent": row}


@router.delete("/agents/{agent_id}")
def delete_agent(agent_id: str, ctxd: dict = Depends(ctx)) -> dict:
    store.execute(
        "DELETE FROM agents WHERE id = ? AND workspace_id = ?", (agent_id, ctxd["workspace_id"])
    )
    return {"ok": True}


@router.post("/apps")
def create_app(payload: AppIn, ctxd: dict = Depends(ctx)) -> dict:
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "name": payload.name,
        "type": payload.type,
        "description": payload.description,
        "entry": payload.entry,
        "status": "online",
        "created_at": store.now(),
    }
    store.insert("apps", row)
    return {"app": row}


# ------------------------------------------------------- Buddy 定时任务


class TaskIn(BaseModel):
    name: str
    prompt: str
    cron: str
    enabled: bool = True


@router.get("/buddy-tasks")
def list_tasks(ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        "SELECT * FROM buddy_tasks WHERE workspace_id = ? ORDER BY created_at DESC",
        (ctxd["workspace_id"],),
    )
    return {"tasks": rows}


@router.post("/buddy-tasks")
def create_task(payload: TaskIn, ctxd: dict = Depends(ctx)) -> dict:
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "user_id": ctxd["user"]["id"],
        "name": payload.name,
        "prompt": payload.prompt,
        "cron": payload.cron,
        "enabled": 1 if payload.enabled else 0,
        "last_run": None,
        "created_at": store.now(),
    }
    store.insert("buddy_tasks", row)
    return {"task": row}


@router.patch("/buddy-tasks/{task_id}")
def toggle_task(task_id: str, body: dict, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM buddy_tasks WHERE id = ? AND workspace_id = ?", (task_id, ctxd["workspace_id"])
    )
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    store.update("buddy_tasks", task_id, {"enabled": 1 if body.get("enabled") else 0})
    return {"ok": True}


@router.delete("/buddy-tasks/{task_id}")
def delete_task(task_id: str, ctxd: dict = Depends(ctx)) -> dict:
    store.execute(
        "DELETE FROM buddy_tasks WHERE id = ? AND workspace_id = ?", (task_id, ctxd["workspace_id"])
    )
    store.execute("DELETE FROM buddy_task_runs WHERE task_id = ?", (task_id,))
    return {"ok": True}


@router.post("/buddy-tasks/{task_id}/run")
def run_task_now(task_id: str, ctxd: dict = Depends(ctx)) -> dict:
    from .. import buddy_tasks

    row = store.row(
        "SELECT * FROM buddy_tasks WHERE id = ? AND workspace_id = ?", (task_id, ctxd["workspace_id"])
    )
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        return {"run": buddy_tasks.run_task(task_id)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"执行失败：{exc}") from exc


@router.get("/buddy-tasks/{task_id}/runs")
def task_runs(task_id: str, ctxd: dict = Depends(ctx)) -> dict:
    from .. import buddy_tasks

    return {"runs": buddy_tasks.runs_of(task_id)}


@router.get("/buddy-runs")
def recent_task_runs(ctxd: dict = Depends(ctx)) -> dict:
    from .. import buddy_tasks

    return {"runs": buddy_tasks.recent_runs(ctxd["workspace_id"])}


# ---------------------------------------------------------------- 分析空间


class SpaceIn(BaseModel):
    name: str
    description: str = ""
    tables: list[str] = []
    metrics: list[str] = []


@router.get("/analysis-spaces")
def list_spaces(ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        "SELECT * FROM analysis_spaces WHERE workspace_id = ? ORDER BY created_at",
        (ctxd["workspace_id"],),
    )
    for r in rows:
        try:
            r["tables"] = json.loads(r.get("tables") or "[]")
        except json.JSONDecodeError:
            r["tables"] = []
        try:
            r["metrics"] = json.loads(r.get("metrics") or "[]")
        except json.JSONDecodeError:
            r["metrics"] = []
    return {"spaces": rows}


@router.post("/analysis-spaces")
def create_space(payload: SpaceIn, ctxd: dict = Depends(ctx)) -> dict:
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "name": payload.name,
        "description": payload.description,
        "tables": json.dumps(payload.tables or [], ensure_ascii=False),
        "metrics": json.dumps(payload.metrics or [], ensure_ascii=False),
        "owner": ctxd["user"]["name"],
        "created_at": store.now(),
    }
    store.insert("analysis_spaces", row)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_space", payload.name)
    return {"space": row}


@router.delete("/analysis-spaces/{space_id}")
def delete_space(space_id: str, ctxd: dict = Depends(ctx)) -> dict:
    store.execute(
        "DELETE FROM analysis_spaces WHERE id = ? AND workspace_id = ?",
        (space_id, ctxd["workspace_id"]),
    )
    return {"ok": True}


# -------------------------------------------------------------------- 卷管理


@router.get("/volumes")
def list_volumes(ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        "SELECT * FROM volumes WHERE workspace_id = ? ORDER BY created_at DESC", (ctxd["workspace_id"],)
    )
    return {"volumes": rows}


@router.post("/volumes/upload")
async def upload_volume(
    file: UploadFile = File(...),
    target: str = Form("demo.retail"),
    volume: str = Form("raw_files"),
    ctxd: dict = Depends(ctx),
) -> dict:
    import re

    parts = [p for p in target.replace('"', "").split(".") if p]
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="目标格式应为 catalog.schema")
    safe_volume = re.sub(r"[^\w\u4e00-\u9fa5-]", "_", volume)[:48] or "raw_files"
    dest_dir = config.VOLUME_DIR / parts[0] / parts[1] / safe_volume
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = file.filename or "file.bin"
    safe_name = re.sub(r"[^\w\u4e00-\u9fa5.\-]", "_", name)
    dest = dest_dir / f"{store.new_id()[:8]}_{safe_name}"
    payload = await file.read()
    dest.write_bytes(payload)

    existing = store.row(
        "SELECT * FROM volumes WHERE workspace_id = ? AND catalog = ? AND schema = ? AND name = ?",
        (ctxd["workspace_id"], parts[0], parts[1], safe_volume),
    )
    files = [f for f in dest_dir.rglob("*") if f.is_file()]
    total = sum(f.stat().st_size for f in files)
    if existing:
        store.update("volumes", existing["id"], {"file_count": len(files), "size_bytes": total})
        volume_id = existing["id"]
    else:
        volume_id = store.new_id()
        store.insert(
            "volumes",
            {
                "id": volume_id,
                "workspace_id": ctxd["workspace_id"],
                "catalog": parts[0],
                "schema": parts[1],
                "name": safe_volume,
                "path": str(dest_dir),
                "file_count": len(files),
                "size_bytes": total,
                "created_at": store.now(),
            },
        )
    store.audit(
        ctxd["workspace_id"], ctxd["user"]["id"], "upload_volume",
        f"{parts[0]}.{parts[1]}.{safe_volume}", {"file": name, "bytes": len(payload)},
    )
    return {"ok": True, "volume_id": volume_id, "file": dest.name, "files": len(files), "size": total}


@router.get("/volumes/{volume_id}/files")
def volume_files(volume_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM volumes WHERE id = ? AND workspace_id = ?", (volume_id, ctxd["workspace_id"])
    )
    if not row:
        raise HTTPException(status_code=404, detail="卷不存在")
    files = []
    base = Path(row["path"])
    if base.exists():
        for f in sorted(base.rglob("*")):
            if f.is_file():
                stat = f.stat()
                files.append(
                    {
                        "name": f.name,
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    }
                )
    return {"volume": row, "files": files}


@router.delete("/volumes/{volume_id}")
def delete_volume(volume_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM volumes WHERE id = ? AND workspace_id = ?", (volume_id, ctxd["workspace_id"])
    )
    if row:
        shutil.rmtree(row["path"], ignore_errors=True)
        store.execute("DELETE FROM volumes WHERE id = ?", (volume_id,))
    return {"ok": True}


# ------------------------------------------------------- 通知渠道与 Git 连接


class NotifyIn(BaseModel):
    wecom: str = ""
    email: str = ""
    webhook: str = ""
    enabled: bool = True


class GitIn(BaseModel):
    url: str = ""
    branch: str = "main"
    username: str = ""


@router.get("/settings/integrations")
def get_integrations(ctxd: dict = Depends(ctx)) -> dict:
    return {
        "notify": store.get_json_setting("notify", {"wecom": "", "email": "", "webhook": "", "enabled": True}),
        "git": store.get_json_setting("git", {"url": "", "branch": "main", "username": ""}),
        "workspace_rule": store.get_setting("workspace_rule", ""),
    }


@router.put("/settings/notify")
def save_notify(payload: NotifyIn, ctxd: dict = Depends(ctx)) -> dict:
    store.set_setting("notify", json.dumps(payload.model_dump(), ensure_ascii=False))
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "update_notify")
    return {"ok": True}


@router.post("/settings/notify/test")
def test_notify(payload: NotifyIn) -> dict:
    import httpx

    targets = []
    if payload.wecom:
        targets.append(("企业微信", payload.wecom))
    if payload.webhook:
        targets.append(("Webhook", payload.webhook))
    if not targets:
        return {"ok": False, "message": "先填写企业微信机器人地址或 Webhook；邮件渠道需要 SMTP 配置（v0.3）"}
    results = []
    for label, url in targets:
        try:
            body = {"msgtype": "text", "text": {"content": "OpenDataBuddy 测试消息"}} if "qyapi" in url else {"text": "OpenDataBuddy 测试消息"}
            with httpx.Client(timeout=6) as client:
                resp = client.post(url, json=body)
            results.append(f"{label} → HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"{label} → {type(exc).__name__}: {exc}")
    ok = all(("200" in r or "204" in r) for r in results)
    return {"ok": ok, "message": "；".join(results)}


@router.put("/settings/git")
def save_git(payload: GitIn, ctxd: dict = Depends(ctx)) -> dict:
    store.set_setting("git", json.dumps(payload.model_dump(), ensure_ascii=False))
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "update_git")
    return {"ok": True}


@router.post("/settings/git/test")
def test_git(payload: GitIn) -> dict:
    import subprocess

    if not payload.url:
        return {"ok": False, "message": "请填写仓库地址"}
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--heads", payload.url],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return {"ok": False, "message": "本机未安装 git"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "连接超时（10s），检查网络或仓库地址"}
    if out.returncode == 0:
        heads = [line.split("refs/heads/")[-1] for line in out.stdout.splitlines() if "refs/heads/" in line]
        return {"ok": True, "message": f"连接成功，{len(heads)} 个分支", "branches": heads[:10]}
    return {"ok": False, "message": (out.stderr or out.stdout).strip()[:200]}


# ---------------------------------------------------------------- 用量统计


@router.get("/usage")
def usage(ctxd: dict = Depends(ctx)) -> dict:
    ws = ctxd["workspace_id"]
    tables = catalog.all_tables(ws)
    rows_count = sum(t["row_count"] or 0 for t in tables)
    members = store.scalar("SELECT count(*) FROM workspace_members WHERE workspace_id = ?", (ws,), 0)
    calls = store.scalar("SELECT count(*) FROM traces WHERE workspace_id = ?", (ws,), 0)
    conversations = store.scalar("SELECT count(*) FROM conversations WHERE workspace_id = ?", (ws,), 0)

    size = 0
    for path in (config.DB_PATH, config.WAREHOUSE_PATH):
        try:
            size += Path(path).stat().st_size
        except OSError:
            pass
    uploads = config.UPLOAD_DIR
    if uploads.exists():
        for f in uploads.rglob("*"):
            if f.is_file():
                try:
                    size += f.stat().st_size
                except OSError:
                    pass
    try:
        free = shutil.disk_usage(str(config.DATA_DIR)).free
    except OSError:
        free = 0

    llm = providers.resolve_config()
    return {
        "seats": {"used": members, "limit": None},
        "compute": {
            "clusters": store.scalar(
                "SELECT count(*) FROM compute_resources WHERE workspace_id = ?", (ws,), 0
            ),
            "cu_total": store.scalar(
                "SELECT coalesce(sum(cu),0) FROM compute_resources WHERE workspace_id = ?", (ws,), 0
            ),
        },
        "storage": {"bytes": size, "free_bytes": free},
        "data": {"tables": len(tables), "rows": rows_count},
        "agent": {"calls": calls, "conversations": conversations},
        "billing": {"enabled": False, "note": "开源版本地运行，不产生任何费用"},
        "llm": {
            "provider": llm.provider,
            "model": llm.model,
            "configured": providers.is_configured(llm),
            "note": "模型调用费用由模型服务商计费，与本平台无关",
        },
    }
