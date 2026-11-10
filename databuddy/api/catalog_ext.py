"""目录扩展接口：Volume / 函数视图 / 外部目录 / 模型目录 / 端到端血缘。

与 ``api/data.py`` 的既有 catalog 接口并存（prefix 都是 ``/api``），
本模块只补充新能力，不覆盖原有路由：

* 卷 Volume —— 建卷、浏览文件、删除
* 函数 / 视图 —— UDF / VIEW / MACRO 三类，挂在 Schema 下
* 外部目录 External Catalog —— postgres/mysql/sqlite/iceberg/hive，
  sqlite 真连读表结构，其余类型只做配置校验 + 模拟元数据抓取（零外部依赖）
* 模型目录 —— 从 registered_models + model_versions 汇总；
  W4 的 mlops 模块可能尚未就绪，用 try/except ImportError 降级为空列表
* 血缘 —— record / graph / impact / discover 四个接口
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import catalog, config, lineage, store
from .auth import ctx

router = APIRouter(prefix="/api", tags=["catalog-ext"])

#: 支持的外部目录类型 -> 默认端口与展示名
EXTERNAL_TYPES: dict[str, dict[str, Any]] = {
    "postgres": {"label": "PostgreSQL", "port": 5432, "schemes": ["postgres", "postgresql"]},
    "mysql": {"label": "MySQL", "port": 3306, "schemes": ["mysql"]},
    "sqlite": {"label": "SQLite", "port": 0, "schemes": ["sqlite", "file"]},
    "iceberg": {"label": "Apache Iceberg", "port": 8181, "schemes": ["iceberg", "http", "https"]},
    "hive": {"label": "Hive Metastore", "port": 9083, "schemes": ["hive", "thrift"]},
}

#: 每种类型必填的配置项（诊断与校验都用它）
REQUIRED_FIELDS: dict[str, list[str]] = {
    "postgres": ["host", "database", "user"],
    "mysql": ["host", "database", "user"],
    "sqlite": ["path"],
    "iceberg": ["catalog_uri", "warehouse"],
    "hive": ["host", "port"],
}

FUNCTION_KINDS: dict[str, str] = {
    "udf": "自定义函数",
    "view": "逻辑视图",
    "macro": "宏",
}


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _ws(ctxd: dict) -> str:
    ws = ctxd.get("workspace_id")
    if not ws:
        raise HTTPException(status_code=400, detail="请先选择工作空间")
    return ws


def _audit(ctxd: dict, action: str, target: str = "", detail: Any = None) -> None:
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], action, target, detail)


# ============================================================== 卷 Volume


class VolumeIn(BaseModel):
    name: str
    catalog: str = "demo"
    # 字段名不能用 schema：会遮蔽 BaseModel.schema 方法，Pydantic 会告警。
    # 对外仍是 JSON 里的 "schema"，内部用 schema_name。
    schema_name: str = Field(default="retail", alias="schema")
    description: str = ""

    model_config = {"populate_by_name": True}


def _volume_root(row: dict) -> Path:
    """卷的物理目录。优先用表里记录的 path，缺失时按约定重建。"""
    if row.get("path"):
        return Path(row["path"])
    return config.VOLUME_DIR / row["catalog"] / row["schema"] / row["name"]


def _scan_volume(row: dict) -> list[dict]:
    base = _volume_root(row)
    files: list[dict] = []
    if not base.exists():
        return files
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "relative_path": str(path.relative_to(base)).replace("\\", "/"),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "ext": path.suffix.lower(),
            }
        )
    return files


def _refresh_volume(row: dict) -> dict:
    """重新统计文件数与占用，并回写元数据。"""
    files = _scan_volume(row)
    total = sum(f["size"] for f in files)
    if row.get("file_count") != len(files) or row.get("size_bytes") != total:
        store.update("volumes", row["id"], {"file_count": len(files), "size_bytes": total})
        row["file_count"], row["size_bytes"] = len(files), total
    return row


@router.get("/volumes")
def list_volumes(ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    rows = store.rows(
        "SELECT * FROM volumes WHERE workspace_id = ? ORDER BY created_at DESC", (ws,)
    )
    for row in rows:
        _refresh_volume(row)
        row["ref"] = f"{row['catalog']}.{row['schema']}.{row['name']}"
        row["exists"] = _volume_root(row).exists()
    total_files = sum(r["file_count"] or 0 for r in rows)
    total_bytes = sum(r["size_bytes"] or 0 for r in rows)
    return {
        "volumes": rows,
        "summary": {"count": len(rows), "files": total_files, "size_bytes": total_bytes},
    }


@router.post("/volumes")
def create_volume(payload: VolumeIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    name = re.sub(r"[^\w\u4e00-\u9fa5-]", "_", (payload.name or "").strip())[:48]
    if not name:
        raise HTTPException(status_code=400, detail="卷名不能为空")
    catalog_name = (payload.catalog or "demo").strip()
    schema_name = (payload.schema_name or "retail").strip()
    existing = store.row(
        "SELECT * FROM volumes WHERE workspace_id = ? AND catalog = ? AND schema = ? AND name = ?",
        (ws, catalog_name, schema_name, name),
    )
    if existing:
        raise HTTPException(status_code=400, detail="该位置下同名卷已存在")
    path = config.VOLUME_DIR / catalog_name / schema_name / name
    path.mkdir(parents=True, exist_ok=True)
    row = {
        "id": store.new_id(),
        "workspace_id": ws,
        "catalog": catalog_name,
        "schema": schema_name,
        "name": name,
        "path": str(path),
        "file_count": 0,
        "size_bytes": 0,
        "created_at": store.now(),
    }
    store.insert("volumes", row)
    _audit(ctxd, "create_volume", f"{catalog_name}.{schema_name}.{name}")
    row["ref"] = f"{catalog_name}.{schema_name}.{name}"
    return {"volume": row}


@router.get("/volumes/{volume_id}/files")
def volume_files(volume_id: str, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    row = store.row("SELECT * FROM volumes WHERE id = ? AND workspace_id = ?", (volume_id, ws))
    if not row:
        raise HTTPException(status_code=404, detail="卷不存在")
    _refresh_volume(row)
    files = _scan_volume(row)
    by_ext: dict[str, int] = {}
    for f in files:
        by_ext[f["ext"] or "(无扩展名)"] = by_ext.get(f["ext"] or "(无扩展名)", 0) + 1
    row["ref"] = f"{row['catalog']}.{row['schema']}.{row['name']}"
    return {
        "volume": row,
        "files": files,
        "summary": {
            "count": len(files),
            "size_bytes": sum(f["size"] for f in files),
            "by_ext": by_ext,
        },
    }


@router.delete("/volumes/{volume_id}")
def delete_volume(volume_id: str, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    row = store.row("SELECT * FROM volumes WHERE id = ? AND workspace_id = ?", (volume_id, ws))
    if not row:
        raise HTTPException(status_code=404, detail="卷不存在")
    base = _volume_root(row)
    # 只删自己登记过的目录，且必须落在 VOLUME_DIR 下，避免误删任意路径
    try:
        if base.exists() and config.VOLUME_DIR.resolve() in base.resolve().parents:
            shutil.rmtree(base, ignore_errors=True)
    except OSError:
        pass
    store.delete("volumes", volume_id)
    _audit(ctxd, "delete_volume", row.get("name") or volume_id)
    return {"ok": True, "deleted_files": True}


# ====================================================== 函数 / 视图 / 宏


class FunctionIn(BaseModel):
    schema_id: str = ""
    schema_ref: str = ""
    name: str
    kind: str = "udf"
    body: str = ""
    description: str = ""


def _resolve_schema(ws: str, schema_id: str = "", schema_ref: str = "") -> dict:
    if schema_id:
        row = store.row(
            "SELECT s.* FROM schemas s JOIN catalogs c ON c.id = s.catalog_id "
            "WHERE s.id = ? AND c.workspace_id = ?",
            (schema_id, ws),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Schema 不存在")
        return row
    parts = [p for p in (schema_ref or "").replace('"', "").split(".") if p]
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="schema_ref 格式应为 catalog.schema")
    row = store.row(
        "SELECT s.* FROM schemas s JOIN catalogs c ON c.id = s.catalog_id "
        "WHERE c.workspace_id = ? AND c.name = ? AND s.name = ?",
        (ws, parts[0], parts[1]),
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Schema 不存在：{schema_ref}")
    return row


def _function_rows(ws: str, schema_id: str | None = None) -> list[dict]:
    sql = (
        "SELECT f.*, s.name AS schema_name, c.name AS catalog_name "
        "FROM functions f JOIN schemas s ON s.id = f.schema_id "
        "JOIN catalogs c ON c.id = s.catalog_id WHERE c.workspace_id = ?"
    )
    params: list[Any] = [ws]
    if schema_id:
        sql += " AND f.schema_id = ?"
        params.append(schema_id)
    sql += " ORDER BY c.name, s.name, f.name"
    rows = store.rows(sql, params)
    for row in rows:
        row["ref"] = f"{row['catalog_name']}.{row['schema_name']}.{row['name']}"
        row["kind_label"] = FUNCTION_KINDS.get(row["kind"], row["kind"])
    return rows


@router.get("/functions")
def list_functions(schema_id: str = "", ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    rows = _function_rows(ws, schema_id or None)
    return {
        "functions": rows,
        "kinds": [{"value": k, "label": v} for k, v in FUNCTION_KINDS.items()],
        "summary": {"count": len(rows)},
    }


@router.post("/functions")
def create_function(payload: FunctionIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    schema = _resolve_schema(ws, payload.schema_id, payload.schema_ref)
    name = re.sub(r"[^\w\u4e00-\u9fa5]", "_", (payload.name or "").strip())[:64].lower()
    if not name:
        raise HTTPException(status_code=400, detail="函数名不能为空")
    kind = (payload.kind or "udf").lower()
    if kind not in FUNCTION_KINDS:
        raise HTTPException(status_code=400, detail=f"不支持的函数类型：{kind}")
    if store.row("SELECT id FROM functions WHERE schema_id = ? AND name = ?", (schema["id"], name)):
        raise HTTPException(status_code=400, detail="该 Schema 下同名函数已存在")
    row = {
        "id": store.new_id(),
        "schema_id": schema["id"],
        "name": name,
        "kind": kind,
        "body": payload.body or "",
        "description": payload.description or "",
        "created_at": store.now(),
    }
    store.insert("functions", row)
    _audit(ctxd, "create_function", name, {"kind": kind})
    row["schema_name"] = schema["name"]
    row["kind_label"] = FUNCTION_KINDS[kind]
    return {"function": row}


@router.delete("/functions/{function_id}")
def delete_function(function_id: str, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    row = store.row(
        "SELECT f.* FROM functions f JOIN schemas s ON s.id = f.schema_id "
        "JOIN catalogs c ON c.id = s.catalog_id WHERE f.id = ? AND c.workspace_id = ?",
        (function_id, ws),
    )
    if not row:
        raise HTTPException(status_code=404, detail="函数不存在")
    store.delete("functions", function_id)
    _audit(ctxd, "delete_function", row["name"])
    return {"ok": True}


# ============================================== 外部目录 External Catalog


class ExternalCatalogIn(BaseModel):
    name: str
    type: str = "postgres"
    config: dict = {}
    description: str = ""


class ExternalCatalogPatch(BaseModel):
    name: str | None = None
    type: str | None = None
    config: dict | None = None
    description: str | None = None


def _validate_external_config(kind: str, cfg: dict) -> list[str]:
    """配置校验：返回问题列表（空表示通过）。"""
    issues: list[str] = []
    if kind not in EXTERNAL_TYPES:
        return [f"不支持的类型 {kind}，可选：{', '.join(EXTERNAL_TYPES)}"]
    for field in REQUIRED_FIELDS.get(kind, []):
        value = cfg.get(field)
        if value in (None, "", 0) and field != "port":
            issues.append(f"缺少必填配置项：{field}")
    if kind in ("postgres", "mysql"):
        port = cfg.get("port")
        if port not in (None, ""):
            try:
                port_int = int(port)
                if not (0 < port_int < 65536):
                    issues.append(f"端口超出范围：{port}")
            except (TypeError, ValueError):
                issues.append(f"端口必须是数字：{port}")
        user = cfg.get("user") or ""
        if user and re.search(r"\s", str(user)):
            issues.append("用户名不能包含空格")
    if kind == "sqlite":
        path = str(cfg.get("path") or "")
        if path and not path.lower().endswith((".db", ".sqlite", ".sqlite3", ".duckdb")):
            issues.append("SQLite 路径建议以 .db / .sqlite / .duckdb 结尾")
    if kind == "iceberg":
        uri = str(cfg.get("catalog_uri") or "")
        if uri and not re.match(r"^(https?|iceberg|thrift)://", uri):
            issues.append("catalog_uri 需以 http(s):// 或 iceberg:// 开头")
    return issues


def _probe_external(kind: str, cfg: dict) -> tuple[bool, str, dict]:
    """连通性测试。sqlite 真连读表结构，其余类型只做配置校验 + 端口探测。"""
    issues = _validate_external_config(kind, cfg)
    if issues:
        return False, "配置不完整：" + "；".join(issues), {"issues": issues, "tables": []}

    if kind == "sqlite":
        path = Path(str(cfg.get("path") or ""))
        if not path.exists():
            return False, f"SQLite 文件不存在：{path}", {"issues": ["文件不存在"], "tables": []}
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                names = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                ]
                tables = []
                for name in names[:200]:
                    try:
                        cols = [
                            {"name": c[1], "data_type": c[2] or ""}
                            for c in conn.execute(f'PRAGMA table_info("{name}")').fetchall()
                        ]
                        count = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                    except sqlite3.Error:
                        cols, count = [], 0
                    tables.append({"name": name, "columns": cols, "row_count": count})
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return False, f"SQLite 连接失败：{exc}", {"issues": [str(exc)], "tables": []}
        return True, f"连接成功，发现 {len(tables)} 张表", {"issues": [], "tables": tables}

    # 其他类型：不引入驱动，只校验配置形状 + 尝试 TCP 可达性（失败不视为错误）
    host = cfg.get("host") or cfg.get("metastore_host") or ""
    port = cfg.get("port") or EXTERNAL_TYPES[kind]["port"]
    reachable = None
    if host:
        import socket

        try:
            with socket.create_connection((str(host), int(port)), timeout=2):
                reachable = True
        except Exception:  # noqa: BLE001 - 探测失败仅作提示
            reachable = False
    tables = _simulate_tables(kind, cfg)
    if reachable is True:
        message = f"{host}:{port} 端口可达，模拟抓取 {len(tables)} 张表"
    elif reachable is False:
        message = f"配置校验通过（{host}:{port} 端口不可达，仅登记配置），模拟抓取 {len(tables)} 张表"
    else:
        message = f"配置校验通过，模拟抓取 {len(tables)} 张表"
    return True, message, {"issues": [], "tables": tables, "reachable": reachable}


def _simulate_tables(kind: str, cfg: dict) -> list[dict]:
    """非 sqlite 类型的模拟元数据抓取（不真连外部库，零依赖）。"""
    database = cfg.get("database") or cfg.get("warehouse") or cfg.get("namespace") or "default"
    schema = cfg.get("schema") or "public"
    seed = re.sub(r"\W", "", f"{kind}{database}{schema}") or "ext"
    names = [f"{seed}_orders", f"{seed}_customers", f"{seed}_products", f"{seed}_events"]
    out = []
    for i, name in enumerate(names):
        out.append(
            {
                "name": name,
                "schema": schema,
                "columns": [
                    {"name": "id", "data_type": "BIGINT"},
                    {"name": "created_at", "data_type": "TIMESTAMP"},
                    {"name": "amount", "data_type": "DOUBLE"},
                ],
                "row_count": (i + 1) * 1200,
                "simulated": True,
            }
        )
    return out


def _external_rows(ws: str) -> list[dict]:
    rows = store.rows(
        "SELECT * FROM external_catalogs WHERE workspace_id = ? ORDER BY created_at DESC", (ws,)
    )
    for row in rows:
        row["config"] = _jsonable(row.get("config"))
        meta = EXTERNAL_TYPES.get(row["type"], {})
        row["type_label"] = meta.get("label", row["type"])
        row["config_ok"] = not _validate_external_config(row["type"], row["config"])
    return rows


@router.get("/external-catalogs")
def list_external_catalogs(ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    rows = _external_rows(ws)
    return {
        "external_catalogs": rows,
        "types": [
            {
                "value": key,
                "label": meta["label"],
                "port": meta["port"],
                "required": REQUIRED_FIELDS.get(key, []),
            }
            for key, meta in EXTERNAL_TYPES.items()
        ],
        "summary": {
            "count": len(rows),
            "ok": sum(1 for r in rows if r["status"] == "ok"),
            "failed": sum(1 for r in rows if r["status"] == "failed"),
        },
    }


@router.post("/external-catalogs")
def create_external_catalog(payload: ExternalCatalogIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    kind = (payload.type or "postgres").lower()
    if kind not in EXTERNAL_TYPES:
        raise HTTPException(
            status_code=400, detail=f"不支持的类型 {kind}，可选：{', '.join(EXTERNAL_TYPES)}"
        )
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    if store.row(
        "SELECT id FROM external_catalogs WHERE workspace_id = ? AND name = ?", (ws, name)
    ):
        raise HTTPException(status_code=400, detail="同名外部目录已存在")
    issues = _validate_external_config(kind, payload.config or {})
    row = {
        "id": store.new_id(),
        "workspace_id": ws,
        "name": name,
        "type": kind,
        "config": json.dumps(payload.config or {}, ensure_ascii=False),
        "status": "failed" if issues else "unknown",
        "last_sync_at": None,
        "description": payload.description or "",
        "created_at": store.now(),
    }
    store.insert("external_catalogs", row)
    _audit(ctxd, "create_external_catalog", name, {"type": kind})
    row["config"] = payload.config or {}
    row["type_label"] = EXTERNAL_TYPES[kind]["label"]
    return {"external_catalog": row, "issues": issues}


@router.patch("/external-catalogs/{catalog_id}")
def update_external_catalog(
    catalog_id: str, payload: ExternalCatalogPatch, ctxd: dict = Depends(ctx)
) -> dict:
    ws = _ws(ctxd)
    row = store.row(
        "SELECT * FROM external_catalogs WHERE id = ? AND workspace_id = ?", (catalog_id, ws)
    )
    if not row:
        raise HTTPException(status_code=404, detail="外部目录不存在")
    patch: dict[str, Any] = {}
    if payload.name is not None and payload.name.strip() != row["name"]:
        if store.row(
            "SELECT id FROM external_catalogs WHERE workspace_id = ? AND name = ? AND id != ?",
            (ws, payload.name.strip(), catalog_id),
        ):
            raise HTTPException(status_code=400, detail="同名外部目录已存在")
        patch["name"] = payload.name.strip()
    if payload.type is not None:
        kind = payload.type.lower()
        if kind not in EXTERNAL_TYPES:
            raise HTTPException(status_code=400, detail=f"不支持的类型：{kind}")
        patch["type"] = kind
    if payload.config is not None:
        patch["config"] = json.dumps(payload.config, ensure_ascii=False)
    if payload.description is not None:
        patch["description"] = payload.description
    if patch:
        store.update("external_catalogs", catalog_id, patch)
        _audit(ctxd, "update_external_catalog", row["name"], {"fields": sorted(patch)})
    merged = {**row, **patch}
    merged["config"] = _jsonable(merged.get("config"))
    merged["type_label"] = EXTERNAL_TYPES.get(merged["type"], {}).get("label", merged["type"])
    return {"external_catalog": merged}


@router.post("/external-catalogs/{catalog_id}/test")
def test_external_catalog(catalog_id: str, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    row = store.row(
        "SELECT * FROM external_catalogs WHERE id = ? AND workspace_id = ?", (catalog_id, ws)
    )
    if not row:
        raise HTTPException(status_code=404, detail="外部目录不存在")
    cfg = _jsonable(row.get("config"))
    ok, message, detail = _probe_external(row["type"], cfg)
    store.update(
        "external_catalogs",
        catalog_id,
        {"status": "ok" if ok else "failed", "last_sync_at": row.get("last_sync_at")},
    )
    _audit(ctxd, "test_external_catalog", row["name"], {"ok": ok})
    return {
        "ok": ok,
        "message": message,
        "type": row["type"],
        "type_label": EXTERNAL_TYPES.get(row["type"], {}).get("label", row["type"]),
        "issues": detail.get("issues", []),
        "tables": detail.get("tables", []),
        "reachable": detail.get("reachable"),
        "tested_at": store.now(),
    }


@router.post("/external-catalogs/{catalog_id}/sync")
def sync_external_catalog(catalog_id: str, ctxd: dict = Depends(ctx)) -> dict:
    """同步外部元数据：sqlite 真读，其他类型模拟抓取后登记为只读表。"""
    ws = _ws(ctxd)
    row = store.row(
        "SELECT * FROM external_catalogs WHERE id = ? AND workspace_id = ?", (catalog_id, ws)
    )
    if not row:
        raise HTTPException(status_code=404, detail="外部目录不存在")
    cfg = _jsonable(row.get("config"))
    ok, message, detail = _probe_external(row["type"], cfg)
    if not ok:
        store.update("external_catalogs", catalog_id, {"status": "failed"})
        raise HTTPException(status_code=400, detail=message)

    tables = detail.get("tables", [])
    cat_name = re.sub(r"[^\w\u4e00-\u9fa5]", "_", row["name"])[:48] or f"ext_{row['type']}"
    # 外部目录在本地目录树里落成一个只读 catalog（不写 DuckDB，仅登记元数据）
    cat = store.row("SELECT * FROM catalogs WHERE workspace_id = ? AND name = ?", (ws, cat_name))
    if not cat:
        cat = {
            "id": store.new_id(),
            "workspace_id": ws,
            "name": cat_name,
            "description": f"外部目录同步（{EXTERNAL_TYPES.get(row['type'], {}).get('label', row['type'])}）",
            "created_at": store.now(),
        }
        store.insert("catalogs", cat)
    sch_name = "external"
    sch = store.row("SELECT * FROM schemas WHERE catalog_id = ? AND name = ?", (cat["id"], sch_name))
    if not sch:
        sch = {
            "id": store.new_id(),
            "catalog_id": cat["id"],
            "name": sch_name,
            "description": "外部目录同步的元数据",
            "created_at": store.now(),
        }
        store.insert("schemas", sch)

    created = updated = 0
    for table in tables:
        name = re.sub(r"[^\w\u4e00-\u9fa5]", "_", table.get("name") or "")[:64].lower()
        if not name:
            continue
        existing = store.row(
            "SELECT id FROM tables WHERE schema_id = ? AND name = ?", (sch["id"], name)
        )
        payload = {
            "schema_id": sch["id"],
            "name": name,
            "kind": "view",
            "description": f"来自外部目录 {row['name']}（{row['type']}）",
            "row_count": int(table.get("row_count") or 0),
            "updated_at": store.now(),
        }
        if existing:
            store.update("tables", existing["id"], payload)
            tid = existing["id"]
            updated += 1
            store.execute("DELETE FROM columns WHERE table_id = ?", (tid,))
        else:
            payload["id"] = store.new_id()
            payload["created_at"] = store.now()
            store.insert("tables", payload)
            tid = payload["id"]
            created += 1
        for i, col in enumerate(table.get("columns") or []):
            store.insert(
                "columns",
                {
                    "id": store.new_id(),
                    "table_id": tid,
                    "name": col.get("name") or f"col_{i}",
                    "data_type": col.get("data_type") or "VARCHAR",
                    "ordinal": i,
                    "is_nullable": 1,
                    "created_at": store.now(),
                },
            )
    store.update(
        "external_catalogs", catalog_id, {"status": "ok", "last_sync_at": store.now()}
    )
    _audit(
        ctxd, "sync_external_catalog", row["name"],
        {"created": created, "updated": updated, "tables": len(tables)},
    )
    return {
        "ok": True,
        "message": message,
        "created": created,
        "updated": updated,
        "tables": len(tables),
        "target": f"{cat_name}.{sch_name}",
        "synced_at": store.now(),
    }


# ===================================================== 模型目录（MLOps 汇总）


def _model_catalog(ws: str) -> tuple[list[dict], str]:
    """汇总模型目录。mlops 模块可能未就绪 -> 降级为空列表。"""
    try:
        from . import mlops  # type: ignore  # noqa: F401
    except ImportError:
        pass
    models: list[dict] = []
    try:
        rows = store.rows(
            "SELECT * FROM registered_models WHERE workspace_id = ? ORDER BY created_at DESC", (ws,)
        )
    except Exception:  # noqa: BLE001 - 表可能还不存在
        return [], "registered_models 表不存在，返回空列表"
    for row in rows:
        versions = []
        try:
            versions = store.rows(
                "SELECT id, version, stage, metrics, artifact_uri, run_id, description, created_at "
                "FROM model_versions WHERE model_id = ? ORDER BY created_at DESC",
                (row["id"],),
            )
        except Exception:  # noqa: BLE001
            versions = []
        production = next((v for v in versions if v["stage"] == "production"), None)
        latest = versions[0] if versions else None
        metrics: dict[str, Any] = {}
        for candidate in (production, latest):
            if candidate and candidate.get("metrics"):
                try:
                    metrics = json.loads(candidate["metrics"] or "{}")
                except json.JSONDecodeError:
                    metrics = {}
                break
        models.append(
            {
                "id": row["id"],
                "name": row["name"],
                "description": row.get("description") or "",
                "framework": row.get("framework") or "",
                "owner": row.get("owner") or "",
                "source_experiment": row.get("source_experiment") or "",
                "created_at": row.get("created_at"),
                "versions": len(versions),
                "stage": (production or latest or {}).get("stage") or "unregistered",
                "version": (production or latest or {}).get("version") or "",
                "metrics": metrics,
                "version_list": [
                    {
                        "id": v["id"],
                        "version": v["version"],
                        "stage": v["stage"],
                        "created_at": v.get("created_at"),
                    }
                    for v in versions
                ],
            }
        )
    return models, ""


@router.get("/catalog/models")
def catalog_models(ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    models, note = _model_catalog(ws)
    by_stage: dict[str, int] = {}
    for m in models:
        by_stage[m["stage"]] = by_stage.get(m["stage"], 0) + 1
    return {
        "models": models,
        "summary": {
            "count": len(models),
            "versions": sum(m["versions"] for m in models),
            "production": by_stage.get("production", 0),
            "by_stage": by_stage,
        },
        "degraded": bool(note),
        "note": note,
    }


@router.get("/catalog/tree-ext")
def catalog_tree_ext(ctxd: dict = Depends(ctx)) -> dict:
    """目录树扩展：在原有 tree 上挂 functions / volumes / models。"""
    ws = _ws(ctxd)
    tree = catalog.tree(ws)
    funcs = _function_rows(ws)
    func_by_schema: dict[str, list[dict]] = {}
    for f in funcs:
        func_by_schema.setdefault(f["schema_id"], []).append(f)
    volumes = store.rows(
        "SELECT id, catalog, schema, name, file_count, size_bytes FROM volumes "
        "WHERE workspace_id = ? ORDER BY name",
        (ws,),
    )
    vol_by_schema: dict[tuple[str, str], list[dict]] = {}
    for v in volumes:
        vol_by_schema.setdefault((v["catalog"], v["schema"]), []).append(v)
    for cat in tree:
        for sch in cat.get("schemas", []):
            sch["functions"] = func_by_schema.get(sch["id"], [])
            sch["volumes"] = vol_by_schema.get((cat["name"], sch["name"]), [])
    models, note = _model_catalog(ws)
    return {
        "catalogs": tree,
        "models": models,
        "external_catalogs": _external_rows(ws),
        "counts": {
            "catalogs": len(tree),
            "schemas": sum(len(c.get("schemas", [])) for c in tree),
            "tables": sum(len(s.get("tables", [])) for c in tree for s in c.get("schemas", [])),
            "functions": len(funcs),
            "volumes": len(volumes),
            "models": len(models),
        },
        "note": note,
    }


# ================================================================== 血缘


class LineageRecordIn(BaseModel):
    upstream: str
    downstream: str
    kind: str = "manual"
    src_column: str | None = None
    dst_column: str | None = None
    transform: str = ""
    detail: str = ""


class LineageBatchIn(BaseModel):
    relations: list[LineageRecordIn] = []


class LineageDiscoverIn(BaseModel):
    sql: str
    downstream: str | None = None
    save: bool = True
    record_columns: bool = True


@router.get("/lineage/summary")
def lineage_summary(ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    return lineage.summary(ws)


@router.get("/lineage/edges")
def lineage_edges(table_ref: str = "", limit: int = 500, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    edges = lineage.all_edges(ws, limit=limit)
    if table_ref:
        key = table_ref.strip().lower()
        edges = [
            e for e in edges
            if e["upstream"].lower() == key or e["downstream"].lower() == key
        ]
    return {"edges": edges, "count": len(edges)}


@router.post("/lineage/record")
def lineage_record(payload: LineageRecordIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    try:
        edge = lineage.record(
            payload.upstream,
            payload.downstream,
            kind=payload.kind or "manual",
            src_column=payload.src_column,
            dst_column=payload.dst_column,
            transform=payload.transform,
            detail=payload.detail,
            workspace_id=ws,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(ctxd, "record_lineage", f"{payload.upstream} -> {payload.downstream}")
    return {"edge": edge, "created": edge.get("created", False)}


@router.post("/lineage/batch")
def lineage_batch(payload: LineageBatchIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    relations = [r.model_dump() for r in payload.relations]
    result = lineage.record_many(ws, relations)
    _audit(ctxd, "record_lineage_batch", "", result)
    return result


@router.get("/lineage/graph")
def lineage_graph(
    table_ref: str,
    depth: int = 3,
    view: str = "table",
    direction: str = "both",
    ctxd: dict = Depends(ctx),
) -> dict:
    ws = _ws(ctxd)
    if not table_ref.strip():
        raise HTTPException(status_code=400, detail="table_ref 不能为空")
    try:
        if (view or "table").lower() == "column":
            return lineage.column_level_view(table_ref, depth=depth, workspace_id=ws, direction=direction)
        return lineage.table_level_view(table_ref, depth=depth, workspace_id=ws, direction=direction)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/lineage/impact")
def lineage_impact(
    table_ref: str, max_depth: int = 5, ctxd: dict = Depends(ctx)
) -> dict:
    ws = _ws(ctxd)
    if not table_ref.strip():
        raise HTTPException(status_code=400, detail="table_ref 不能为空")
    return lineage.impact_analysis(table_ref, workspace_id=ws, max_depth=max_depth)


@router.post("/lineage/discover")
def lineage_discover(payload: LineageDiscoverIn, ctxd: dict = Depends(ctx)) -> dict:
    ws = _ws(ctxd)
    if not (payload.sql or "").strip():
        raise HTTPException(status_code=400, detail="sql 不能为空")
    if payload.save:
        result = lineage.discover_and_record(
            ws,
            payload.sql,
            downstream=payload.downstream,
            record_columns=payload.record_columns,
        )
        _audit(
            ctxd, "discover_lineage", result.get("target") or "",
            {"sources": result.get("sources"), "columns": result.get("recorded_columns")},
        )
        return result
    return lineage.auto_discover(payload.sql)


@router.delete("/lineage/{edge_id}")
def lineage_delete(edge_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row(
        "SELECT * FROM lineage WHERE id = ? AND (workspace_id = ? OR workspace_id IS NULL)",
        (edge_id, ctxd["workspace_id"]),
    )
    if not row:
        raise HTTPException(status_code=404, detail="血缘记录不存在")
    lineage.delete(edge_id)
    _audit(ctxd, "delete_lineage", edge_id)
    return {"ok": True}
