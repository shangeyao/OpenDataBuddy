"""Studio 开发工作台接口：Notebook CRUD、cell 执行、魔法命令帮助。

路由约定与其它域一致：``prefix="/api"``，鉴权用 ``from .auth import ctx``。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import notebook, store
from .auth import ctx

router = APIRouter(prefix="/api", tags=["notebook"])


# --------------------------------------------------------------------- 入参模型


class NotebookIn(BaseModel):
    name: str = "未命名 Notebook"
    description: str = ""
    params: dict = {}
    workdir: str = ""
    cells: list[dict] | None = None


class NotebookPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    params: dict | None = None
    workdir: str | None = None
    cells: list[dict] | None = None


class CellIn(BaseModel):
    type: str = "sql"
    source: str = ""
    index: int | None = None


class CellPatch(BaseModel):
    type: str | None = None
    source: str | None = None


class RunCellIn(BaseModel):
    params: dict = {}
    source: str | None = None
    type: str | None = None


class RunAllIn(BaseModel):
    params: dict = {}


class ExplainIn(BaseModel):
    sql: str
    params: dict = {}


# ------------------------------------------------------------------- 辅助函数


def _require(notebook_id: str, ctxd: dict) -> dict:
    found = notebook.get_notebook(notebook_id, ctxd["workspace_id"])
    if not found:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    return found


def _find_cell(found: dict, cell_id: str) -> dict:
    cell = next((c for c in found["cells"] if c.get("id") == cell_id), None)
    if not cell:
        raise HTTPException(status_code=404, detail="Cell 不存在")
    return cell


def _shape(result: dict) -> dict:
    """裁剪返回体：行数可能很大，列表页只回前 100 行。"""
    payload = dict(result)
    payload["rows"] = (payload.get("rows") or [])[:100]
    payload.pop("rendered_sql", None)
    return payload


# ------------------------------------------------------------------ Notebook CRUD


@router.get("/notebooks")
def list_notebooks(ctxd: dict = Depends(ctx)) -> dict:
    return {
        "notebooks": notebook.list_notebooks(ctxd["workspace_id"]),
        "summary": notebook.notebook_summary(ctxd["workspace_id"]),
    }


@router.post("/notebooks")
def create_notebook(payload: NotebookIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        row = notebook.create_notebook(
            ctxd["workspace_id"],
            payload.name,
            description=payload.description,
            cells=payload.cells,
            params=payload.params,
            workdir=payload.workdir,
        )
    except notebook.NotebookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_notebook", row["name"])
    return {"notebook": row}


@router.get("/notebooks/magic-help")
def magic_help() -> dict:
    """魔法命令清单 —— 注意：必须声明在 /notebooks/{id} 之前。"""
    items = notebook.magic_help()
    categories: list[str] = []
    for item in items:
        if item["category"] not in categories:
            categories.append(item["category"])
    return {
        "magics": items,
        "categories": categories,
        "count": len(items),
        "syntax": {
            "sql": "直接写 SQL，支持 {{param}} 占位；也可用 %sql 前缀",
            "python": "直接写 Python，变量跨 cell 共享；也可用 %python 前缀",
            "markdown": "写文档，%md 开头或用 markdown 类型 cell",
        },
        "shortcuts": [
            {"keys": "Ctrl / Cmd + Enter", "desc": "运行当前 cell"},
            {"keys": "Shift + Enter", "desc": "运行并跳到下一个 cell"},
            {"keys": "Ctrl / Cmd + S", "desc": "保存 notebook"},
        ],
    }


@router.get("/notebooks/{notebook_id}")
def get_notebook(notebook_id: str, ctxd: dict = Depends(ctx)) -> dict:
    found = _require(notebook_id, ctxd)
    return {
        "notebook": found,
        "params": notebook.params_of(found),
        "workdir": str(notebook.workdir_of(found)),
        "magics": notebook.magic_help(),
    }


@router.patch("/notebooks/{notebook_id}")
def patch_notebook(notebook_id: str, payload: NotebookPatch, ctxd: dict = Depends(ctx)) -> dict:
    _require(notebook_id, ctxd)
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        row = notebook.update_notebook(notebook_id, data)
    except notebook.NotebookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"notebook": row}


@router.delete("/notebooks/{notebook_id}")
def delete_notebook(notebook_id: str, ctxd: dict = Depends(ctx)) -> dict:
    _require(notebook_id, ctxd)
    notebook.delete_notebook(notebook_id)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "delete_notebook", notebook_id)
    return {"ok": True}


# ---------------------------------------------------------------------- Cell


@router.post("/notebooks/{notebook_id}/cells")
def create_cell(notebook_id: str, payload: CellIn, ctxd: dict = Depends(ctx)) -> dict:
    _require(notebook_id, ctxd)
    try:
        cell = notebook.add_cell(
            notebook_id, cell_type=payload.type, source=payload.source, index=payload.index
        )
    except notebook.NotebookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"cell": cell, "notebook": notebook.get_notebook(notebook_id)}


@router.patch("/notebooks/{notebook_id}/cells/{cell_id}")
def patch_cell(
    notebook_id: str, cell_id: str, payload: CellPatch, ctxd: dict = Depends(ctx)
) -> dict:
    _require(notebook_id, ctxd)
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    data["reset_output"] = True
    try:
        cell = notebook.update_cell(notebook_id, cell_id, data)
    except notebook.NotebookError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"cell": cell}


@router.delete("/notebooks/{notebook_id}/cells/{cell_id}")
def delete_cell(notebook_id: str, cell_id: str, ctxd: dict = Depends(ctx)) -> dict:
    _require(notebook_id, ctxd)
    try:
        removed = notebook.delete_cell(notebook_id, cell_id)
    except notebook.NotebookError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Cell 不存在")
    return {"ok": True, "notebook": notebook.get_notebook(notebook_id)}


# ---------------------------------------------------------------------- 执行


@router.post("/notebooks/{notebook_id}/cells/{cell_id}/run")
def run_cell(
    notebook_id: str, cell_id: str, payload: RunCellIn | None = None, ctxd: dict = Depends(ctx)
) -> dict:
    found = _require(notebook_id, ctxd)
    cell = _find_cell(found, cell_id)
    body = payload or RunCellIn()
    if body.source is not None:
        cell = {**cell, "source": body.source}
    if body.type:
        cell = {**cell, "type": body.type}
    try:
        result = notebook.execute_cell(notebook_id, cell, params=body.params)
    except notebook.NotebookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(
        ctxd["workspace_id"],
        ctxd["user"]["id"],
        "run_notebook_cell",
        found["name"],
        {"cell": cell_id, "ok": not result.get("error"), "ms": result.get("elapsed_ms")},
    )
    return {"result": _shape(result), "notebook": notebook.get_notebook(notebook_id)}


@router.post("/notebooks/{notebook_id}/run-all")
def run_all(
    notebook_id: str, payload: RunAllIn | None = None, ctxd: dict = Depends(ctx)
) -> dict:
    _require(notebook_id, ctxd)
    body = payload or RunAllIn()
    try:
        out = notebook.run_all(notebook_id, params=body.params)
    except notebook.NotebookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(
        ctxd["workspace_id"],
        ctxd["user"]["id"],
        "run_notebook",
        notebook_id,
        {"cells": len(out["results"]), "failed": out["failed"], "ms": out["elapsed_ms"]},
    )
    return {
        "ok": out["ok"],
        "failed": out["failed"],
        "elapsed_ms": out["elapsed_ms"],
        "results": [_shape(r) for r in out["results"]],
        "notebook": out["notebook"],
    }


@router.post("/notebooks/{notebook_id}/reset")
def reset_notebook(notebook_id: str, ctxd: dict = Depends(ctx)) -> dict:
    """清空 Python 命名空间与运行时参数（等价 %clear）。"""
    _require(notebook_id, ctxd)
    notebook.reset_namespace(notebook_id)
    return {"ok": True, "message": "已重置运行时变量与参数"}


# ------------------------------------------------------------------ SQL 解释


@router.post("/sql/explain")
def explain_sql(payload: ExplainIn, ctxd: dict = Depends(ctx)) -> dict:
    """参数化 SQL 解释：渲染 {{param}}、过安全护栏、解析引用表，不执行。"""
    return notebook.explain_sql(payload.sql, payload.params)
