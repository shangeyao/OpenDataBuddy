"""Catalog 目录服务：逻辑名（catalog.schema.table）与物理位置的解析。"""
from __future__ import annotations

from . import store, warehouse


def tree(workspace_id: str) -> list[dict]:
    catalogs = store.rows(
        "SELECT * FROM catalogs WHERE workspace_id = ? ORDER BY name", (workspace_id,)
    )
    for cat in catalogs:
        schemas = store.rows(
            "SELECT * FROM schemas WHERE catalog_id = ? ORDER BY name", (cat["id"],)
        )
        for sch in schemas:
            tables = store.rows(
                "SELECT id, name, kind, description, row_count, updated_at FROM tables "
                "WHERE schema_id = ? ORDER BY name",
                (sch["id"],),
            )
            sch["tables"] = tables
        cat["schemas"] = schemas
    return catalogs


def all_tables(workspace_id: str) -> list[dict]:
    rows = store.rows(
        """
        SELECT t.id, t.name, t.kind, t.description, t.row_count, s.name AS schema_name,
               c.name AS catalog_name, t.schema_id
        FROM tables t
        JOIN schemas s ON s.id = t.schema_id
        JOIN catalogs c ON c.id = s.catalog_id
        WHERE c.workspace_id = ?
        ORDER BY c.name, s.name, t.name
        """,
        (workspace_id,),
    )
    for r in rows:
        r["ref"] = f"{r['catalog_name']}.{r['schema_name']}.{r['name']}"
    return rows


def mapping(workspace_id: str) -> list[tuple[str, str]]:
    rows = store.rows(
        "SELECT c.name AS catalog_name, s.name AS schema_name FROM schemas s "
        "JOIN catalogs c ON c.id = s.catalog_id WHERE c.workspace_id = ?",
        (workspace_id,),
    )
    return [(r["catalog_name"], r["schema_name"]) for r in rows]


def bare_tables(workspace_id: str) -> dict[str, str]:
    """表名唯一时才允许裸名引用，避免歧义。"""
    rows = all_tables(workspace_id)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["name"]] = counts.get(r["name"], 0) + 1
    out: dict[str, str] = {}
    for r in rows:
        if counts[r["name"]] == 1:
            phys = warehouse.physical_name(r["catalog_name"], r["schema_name"])
            out[r["name"]] = f'"{phys}"."{r["name"]}"'
    return out


def resolve(workspace_id: str, ref: str) -> dict | None:
    """解析 'cat.sch.tbl' / 'sch.tbl' / 'tbl'，返回目录记录。"""
    parts = [p for p in (ref or "").replace('"', "").split(".") if p]
    rows = all_tables(workspace_id)
    if len(parts) == 3:
        for r in rows:
            if (
                r["catalog_name"].lower() == parts[0].lower()
                and r["schema_name"].lower() == parts[1].lower()
                and r["name"].lower() == parts[2].lower()
            ):
                return r
    elif len(parts) == 2:
        matched = [
            r for r in rows
            if r["schema_name"].lower() == parts[0].lower() and r["name"].lower() == parts[1].lower()
        ]
        if len(matched) == 1:
            return matched[0]
    elif len(parts) == 1:
        matched = [r for r in rows if r["name"].lower() == parts[0].lower()]
        if len(matched) == 1:
            return matched[0]
    return None


def columns_of(table_id: str) -> list[dict]:
    return store.rows(
        "SELECT * FROM columns WHERE table_id = ? ORDER BY ordinal", (table_id,)
    )


def table_detail(workspace_id: str, ref: str) -> dict | None:
    found = resolve(workspace_id, ref)
    if not found:
        return None
    cols = columns_of(found["id"])
    tags = {}
    for t in store.rows(
        "SELECT ct.*, c.name AS column_name FROM column_tags ct "
        "JOIN columns c ON c.id = ct.column_id WHERE c.table_id = ?",
        (found["id"],),
    ):
        tags.setdefault(t["column_name"], []).append(t)
    for c in cols:
        c["tags"] = tags.get(c["name"], [])
    found["columns"] = cols
    found["lineage"] = store.rows(
        "SELECT * FROM lineage WHERE upstream = ? OR downstream = ?",
        (found["ref"], found["ref"]),
    )
    return found


def run_workspace_sql(workspace_id: str, sql: str, limit: int = 500) -> dict:
    return warehouse.query(
        sql,
        mapping=mapping(workspace_id),
        tables=bare_tables(workspace_id),
        limit=limit,
    )
