"""数据域接口：Catalog、数据源、数据接入、SQL 工作台、语义层、知识库。"""
from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .. import catalog, charts, ingest, semantic, store, warehouse
from ..harness import knowledge, memory
from .auth import ctx

router = APIRouter(prefix="/api", tags=["data"])


# ------------------------------------------------------------------- Catalog


@router.get("/catalog/tree")
def catalog_tree(ctxd: dict = Depends(ctx)) -> dict:
    return {"catalogs": catalog.tree(ctxd["workspace_id"])}


@router.get("/catalog/tables")
def catalog_tables(ctxd: dict = Depends(ctx)) -> dict:
    return {"tables": catalog.all_tables(ctxd["workspace_id"])}


@router.get("/catalog/table")
def catalog_table(ref: str, ctxd: dict = Depends(ctx)) -> dict:
    detail = catalog.table_detail(ctxd["workspace_id"], ref)
    if not detail:
        raise HTTPException(status_code=404, detail="表不存在")
    return {"table": detail}


@router.get("/catalog/preview")
def catalog_preview(ref: str, limit: int = 20, ctxd: dict = Depends(ctx)) -> dict:
    detail = catalog.resolve(ctxd["workspace_id"], ref)
    if not detail:
        raise HTTPException(status_code=404, detail="表不存在")
    return warehouse.preview(detail["catalog_name"], detail["schema_name"], detail["name"], limit)


class CatalogIn(BaseModel):
    name: str
    description: str = ""


class SchemaIn(BaseModel):
    catalog: str
    name: str
    description: str = ""


@router.post("/catalog/catalogs")
def create_catalog(payload: CatalogIn, ctxd: dict = Depends(ctx)) -> dict:
    name = ingest.sanitize_identifier(payload.name)
    if store.row("SELECT id FROM catalogs WHERE workspace_id = ? AND name = ?", (ctxd["workspace_id"], name)):
        raise HTTPException(status_code=400, detail="Catalog 已存在")
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "name": name,
        "description": payload.description,
        "created_at": store.now(),
    }
    store.insert("catalogs", row)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_catalog", name)
    return {"catalog": row}


@router.post("/catalog/schemas")
def create_schema(payload: SchemaIn, ctxd: dict = Depends(ctx)) -> dict:
    cat = store.row(
        "SELECT * FROM catalogs WHERE workspace_id = ? AND name = ?",
        (ctxd["workspace_id"], payload.catalog),
    )
    if not cat:
        raise HTTPException(status_code=404, detail="Catalog 不存在")
    name = ingest.sanitize_identifier(payload.name)
    if store.row("SELECT id FROM schemas WHERE catalog_id = ? AND name = ?", (cat["id"], name)):
        raise HTTPException(status_code=400, detail="Schema 已存在")
    row = {
        "id": store.new_id(),
        "catalog_id": cat["id"],
        "name": name,
        "description": payload.description,
        "created_at": store.now(),
    }
    store.insert("schemas", row)
    warehouse.ensure_namespace(cat["name"], name)
    return {"schema": row}


@router.delete("/catalog/tables/{table_id}")
def drop_table(table_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        """
        SELECT t.*, s.name AS schema_name, c.name AS catalog_name
        FROM tables t JOIN schemas s ON s.id = t.schema_id JOIN catalogs c ON c.id = s.catalog_id
        WHERE t.id = ? AND c.workspace_id = ?
        """,
        (table_id, ctxd["workspace_id"]),
    )
    if not row:
        raise HTTPException(status_code=404, detail="表不存在")
    warehouse.drop_table(row["catalog_name"], row["schema_name"], row["name"])
    store.execute("DELETE FROM columns WHERE table_id = ?", (table_id,))
    store.execute("DELETE FROM tables WHERE id = ?", (table_id,))
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "drop_table", row["name"])
    return {"ok": True}


# -------------------------------------------------------------------- 数据源


class SourceIn(BaseModel):
    name: str
    type: str = "file"
    description: str = ""
    config: dict = {}


@router.get("/sources")
def list_sources(ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        "SELECT * FROM sources WHERE workspace_id = ? ORDER BY created_at DESC", (ctxd["workspace_id"],)
    )
    for r in rows:
        try:
            r["config"] = json.loads(r.get("config") or "{}")
        except json.JSONDecodeError:
            r["config"] = {}
    return {"sources": rows}


@router.post("/sources")
def create_source(payload: SourceIn, ctxd: dict = Depends(ctx)) -> dict:
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "name": payload.name,
        "type": payload.type,
        "config": json.dumps(payload.config, ensure_ascii=False),
        "description": payload.description,
        "status": "unknown",
        "created_at": store.now(),
    }
    store.insert("sources", row)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_source", payload.name)
    return {"source": row}


@router.post("/sources/{source_id}/test")
def test_source(source_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM sources WHERE id = ? AND workspace_id = ?", (source_id, ctxd["workspace_id"])
    )
    if not row:
        raise HTTPException(status_code=404, detail="数据源不存在")
    cfg = json.loads(row.get("config") or "{}")
    ok, message = _probe(row["type"], cfg)
    store.update(
        "sources", source_id, {"status": "ok" if ok else "failed", "last_test_at": store.now()}
    )
    return {"ok": ok, "message": message}


def _probe(kind: str, cfg: dict) -> tuple[bool, str]:
    try:
        if kind == "file":
            path = Path(cfg.get("path") or "")
            if path.exists():
                files = len(list(path.glob("*"))) if path.is_dir() else 1
                return True, f"路径可访问，发现 {files} 个文件"
            return False, f"路径不存在：{path}"
        if kind == "sqlite":
            path = Path(cfg.get("path") or "")
            if not path.exists():
                return False, f"SQLite 文件不存在：{path}"
            with sqlite3.connect(path) as conn:
                tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            return True, f"连接成功，{tables} 张表"
        host = cfg.get("host") or "127.0.0.1"
        port = int(cfg.get("port") or (5432 if kind == "postgres" else 3306))
        with socket.create_connection((host, port), timeout=4):
            pass
        return True, f"{host}:{port} TCP 可达（实际取数请用「文件接入」或直连 SQL）"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


@router.delete("/sources/{source_id}")
def delete_source(source_id: str, ctxd: dict = Depends(ctx)) -> dict:
    store.execute(
        "DELETE FROM sources WHERE id = ? AND workspace_id = ?", (source_id, ctxd["workspace_id"])
    )
    return {"ok": True}


# -------------------------------------------------------------------- 数据接入


@router.post("/ingest/upload")
async def upload(
    file: UploadFile = File(...),
    target: str = Form("demo.retail"),
    table: str = Form(""),
    description: str = Form(""),
    ctxd: dict = Depends(ctx),
) -> dict:
    parts = [p for p in target.replace('"', "").split(".") if p]
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="目标格式应为 catalog.schema")
    cat_name, sch_name = ingest.sanitize_identifier(parts[0]), ingest.sanitize_identifier(parts[1])

    cat = store.row(
        "SELECT * FROM catalogs WHERE workspace_id = ? AND name = ?", (ctxd["workspace_id"], cat_name)
    )
    if not cat:
        cat = {
            "id": store.new_id(),
            "workspace_id": ctxd["workspace_id"],
            "name": cat_name,
            "description": "上传文件时自动创建",
            "created_at": store.now(),
        }
        store.insert("catalogs", cat)
    sch = store.row("SELECT * FROM schemas WHERE catalog_id = ? AND name = ?", (cat["id"], sch_name))
    if not sch:
        sch = {
            "id": store.new_id(),
            "catalog_id": cat["id"],
            "name": sch_name,
            "description": "",
            "created_at": store.now(),
        }
        store.insert("schemas", sch)

    payload = await file.read()
    if len(payload) > 200 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="单文件上限 200MB")
    try:
        path = ingest.save_upload(file.filename or "upload.csv", payload)
        table_name = table or Path(file.filename or "upload").stem
        stats = ingest.ingest_file(
            ctxd["workspace_id"], cat_name, sch_name, table_name, path, description
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"解析失败：{exc}") from exc

    store.audit(
        ctxd["workspace_id"], ctxd["user"]["id"], "ingest_file", f"{cat_name}.{sch_name}.{stats['table']}",
        {"rows": stats["row_count"], "file": file.filename},
    )
    return {"ok": True, **stats, "ref": f"{cat_name}.{sch_name}.{stats['table']}"}


class SqlTableIn(BaseModel):
    target: str
    table: str
    sql: str
    description: str = ""


@router.post("/ingest/from-sql")
def create_table_from_sql(payload: SqlTableIn, ctxd: dict = Depends(ctx)) -> dict:
    parts = [p for p in payload.target.replace('"', "").split(".") if p]
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="目标格式应为 catalog.schema")
    cat_name, sch_name = parts
    try:
        prepared = warehouse.guard(payload.sql)
        prepared = warehouse.qualify(
            prepared, catalog.mapping(ctxd["workspace_id"]), catalog.bare_tables(ctxd["workspace_id"])
        )
    except warehouse.SqlRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    table_name = ingest.sanitize_identifier(payload.table)
    warehouse.ensure_namespace(cat_name, sch_name)
    phys = warehouse.physical_name(cat_name, sch_name)
    try:
        warehouse.execute_ddl(f'CREATE OR REPLACE TABLE "{phys}"."{table_name}" AS {prepared}')
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"建表失败：{exc}") from exc

    # 登记元数据
    sch = store.row(
        "SELECT s.id FROM schemas s JOIN catalogs c ON c.id = s.catalog_id "
        "WHERE c.workspace_id = ? AND c.name = ? AND s.name = ?",
        (ctxd["workspace_id"], cat_name, sch_name),
    )
    if not sch:
        raise HTTPException(status_code=400, detail="目标 schema 不存在，请先在目录里创建")
    old = store.row("SELECT id FROM tables WHERE schema_id = ? AND name = ?", (sch["id"], table_name))
    if old:
        store.execute("DELETE FROM columns WHERE table_id = ?", (old["id"],))
        store.delete("tables", old["id"])
    tid = store.new_id()
    store.insert(
        "tables",
        {
            "id": tid,
            "schema_id": sch["id"],
            "name": table_name,
            "kind": "table",
            "description": payload.description or "由 SQL 生成",
            "row_count": warehouse.count_rows(cat_name, sch_name, table_name),
            "created_at": store.now(),
            "updated_at": store.now(),
        },
    )
    for i, col in enumerate(warehouse.describe(cat_name, sch_name, table_name)):
        store.insert(
            "columns",
            {
                "id": store.new_id(),
                "table_id": tid,
                "name": col["name"],
                "data_type": col["data_type"],
                "ordinal": i,
                "is_nullable": 1 if col["is_nullable"] else 0,
                "created_at": store.now(),
            },
        )
    return {"ok": True, "ref": f"{cat_name}.{sch_name}.{table_name}"}


# ---------------------------------------------------------------- SQL 工作台


class SqlIn(BaseModel):
    sql: str
    limit: int = 200


@router.post("/sql/run")
def run_sql(payload: SqlIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        result = catalog.run_workspace_sql(ctxd["workspace_id"], payload.sql, limit=payload.limit)
    except warehouse.SqlRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"执行失败：{exc}") from exc
    result["chart"] = charts.infer(result)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "run_sql", "", {"rows": result["row_count"]})
    return result


# ------------------------------------------------------------------- 语义层


class MetricIn(BaseModel):
    name: str
    display_name: str = ""
    expr: str
    table_ref: str
    unit: str = ""
    description: str = ""
    dimensions: list[str] = []
    joins: list[dict] | None = None


@router.get("/metrics")
def list_metrics(ctxd: dict = Depends(ctx)) -> dict:
    return {"metrics": semantic.list_metrics(ctxd["workspace_id"])}


@router.post("/metrics")
def create_metric(payload: MetricIn, ctxd: dict = Depends(ctx)) -> dict:
    data = payload.model_dump()
    if not data.get("joins"):
        sample = store.row(
            "SELECT joins FROM metrics WHERE workspace_id = ? AND joins != '[]' LIMIT 1",
            (ctxd["workspace_id"],),
        )
        if sample:
            try:
                data["joins"] = json.loads(sample["joins"])
            except json.JSONDecodeError:
                data["joins"] = []
    data["owner"] = ctxd["user"]["name"]
    try:
        row = semantic.create_metric(ctxd["workspace_id"], data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_metric", row["name"])
    return {"metric": row}


@router.delete("/metrics/{metric_id}")
def delete_metric(metric_id: str, ctxd: dict = Depends(ctx)) -> dict:
    store.execute(
        "DELETE FROM metrics WHERE id = ? AND workspace_id = ?", (metric_id, ctxd["workspace_id"])
    )
    return {"ok": True}


class MetricQueryIn(BaseModel):
    metric: str
    dimensions: list[str] = []
    start: str | None = None
    end: str | None = None
    granularity: str | None = None


@router.post("/metrics/query")
def query_metric(payload: MetricQueryIn, ctxd: dict = Depends(ctx)) -> dict:
    m = semantic.find_metric(ctxd["workspace_id"], payload.metric)
    if not m:
        raise HTTPException(status_code=404, detail="指标不存在")
    dims = payload.dimensions or []
    built = semantic.build_metric_sql(
        m, dims, payload.start, payload.end, payload.granularity,
        order="time" if "__date__" in dims else "auto",
    )
    result = catalog.run_workspace_sql(ctxd["workspace_id"], built["sql"], limit=500)
    result["chart"] = charts.infer(result)
    result["metric"] = {"name": m["name"], "display_name": m.get("display_name"), "unit": m.get("unit")}
    return result


# ------------------------------------------------------------------- 知识库


class DocIn(BaseModel):
    title: str
    content: str
    tags: str = ""
    scope: str = "workspace"


@router.get("/knowledge")
def list_docs(ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        "SELECT id, title, tags, source_type, scope, created_at, updated_at, length(content) AS size "
        "FROM knowledge_docs WHERE workspace_id = ? ORDER BY created_at DESC",
        (ctxd["workspace_id"],),
    )
    return {"docs": rows}


@router.get("/knowledge/{doc_id}")
def get_doc(doc_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM knowledge_docs WHERE id = ? AND workspace_id = ?", (doc_id, ctxd["workspace_id"])
    )
    if not row:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"doc": row}


@router.post("/knowledge")
def create_doc(payload: DocIn, ctxd: dict = Depends(ctx)) -> dict:
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "title": payload.title,
        "content": payload.content,
        "tags": payload.tags,
        "scope": payload.scope,
        "source_type": "manual",
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("knowledge_docs", row)
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_doc", payload.title)
    return {"doc": row}


@router.delete("/knowledge/{doc_id}")
def delete_doc(doc_id: str, ctxd: dict = Depends(ctx)) -> dict:
    store.execute(
        "DELETE FROM knowledge_docs WHERE id = ? AND workspace_id = ?", (doc_id, ctxd["workspace_id"])
    )
    return {"ok": True}


@router.get("/knowledge-search")
def search_knowledge(q: str, ctxd: dict = Depends(ctx)) -> dict:
    return {"hits": knowledge.search(ctxd["workspace_id"], q, 8)}


@router.post("/knowledge/rebuild-catalog")
def rebuild_catalog_docs(ctxd: dict = Depends(ctx)) -> dict:
    count = knowledge.rebuild_catalog_docs(ctxd["workspace_id"])
    return {"ok": True, "docs": count}


# --------------------------------------------------------------------- 记忆


class MemoryIn(BaseModel):
    key: str
    value: str


@router.get("/memory")
def list_memory(ctxd: dict = Depends(ctx)) -> dict:
    return {"items": memory.recall(ctxd["workspace_id"], ctxd["user"]["id"])}


@router.post("/memory")
def add_memory(payload: MemoryIn, ctxd: dict = Depends(ctx)) -> dict:
    return memory.remember(ctxd["workspace_id"], ctxd["user"]["id"], payload.key, payload.value)


@router.delete("/memory/{key}")
def del_memory(key: str, ctxd: dict = Depends(ctx)) -> dict:
    memory.forget(ctxd["workspace_id"], ctxd["user"]["id"], key)
    return {"ok": True}
