"""Feature Store：特征定义、物化计算、统计、版本与训练集构建。

设计要点
--------
* 特征由 `(table_ref, expr)` 定义 —— expr 是一段**只读 SQL 表达式**，既能是行级
  列表达式（`amount * 0.8`），也能是聚合表达式（`sum(amount)`）。是否聚合由
  正则识别，聚合表达式按主体键 GROUP BY。
* 主体（entity）既可以是主体类型（customer / order），也可以直接是主体键列名。
  解析顺序：同名物理列 → `<entity>_id` → 表里第一个 `*_id` 列 → 首列。
  这样「训练 / 推理一致性」才有共同的对齐键。
* 统计信息（count / 空值率 / distinct / min / max / mean）落 `features.stats`，
  前端表格与漂移对比都读它。
* 版本：`features` 表在 workspace 内 name 唯一，历史版本落 `feature_revisions`
  （本模块内自建，不改 store.py）。改动 expr / table_ref / entity / dtype 时版本自增。
* 训练集：把多个特征按主体键拼成宽表，表达式与在线推理共用同一份定义 —— 这是
  「训练 / 推理一致性」的落点。
* 血缘：建特征时向 `lineage` 写一条 `table_ref -> feature:<name>` 的边，可反查。
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import catalog, store, warehouse

DTYPES = ["float", "int", "string", "bool", "date"]
NUMERIC_DTYPES = {"float", "int", "double", "decimal", "number", "bigint", "long", "real"}

# 聚合函数识别：命中即认为 expr 是「按主体聚合」的定义
AGG_RE = re.compile(
    r"\b(sum|avg|mean|count|min|max|stddev|stddev_pop|stddev_samp|median|"
    r"quantile_cont|quantile_disc|approx_count_distinct|any_value|array_agg|list)\s*\(",
    re.IGNORECASE,
)

_revision_ready = False


# ------------------------------------------------------------------ 基础工具


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _ident(name: str) -> str:
    """把任意名字变成安全的 SQL 别名。"""
    return re.sub(r"[^\w\u4e00-\u9fa5]", "_", (name or "").strip()) or "f"


def _lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ensure_revision_table() -> None:
    """特征历史版本表：features 在 workspace 内 name 唯一，历史只能另存。"""
    global _revision_ready
    if _revision_ready:
        return
    store.execute(
        """
        CREATE TABLE IF NOT EXISTS feature_revisions (
            id TEXT PRIMARY KEY,
            feature_id TEXT,
            workspace_id TEXT,
            name TEXT,
            version INTEGER DEFAULT 1,
            entity TEXT DEFAULT '',
            dtype TEXT DEFAULT 'float',
            expr TEXT DEFAULT '',
            table_ref TEXT DEFAULT '',
            description TEXT DEFAULT '',
            stats TEXT DEFAULT '{}',
            status TEXT DEFAULT 'published',
            created_at TEXT
        )
        """
    )
    _revision_ready = True


# ------------------------------------------------------------------ 主体 / 解析


def _columns_of(workspace_id: str, table_ref: str) -> tuple[dict | None, list[str]]:
    found = catalog.resolve(workspace_id, table_ref)
    if not found:
        return None, []
    return found, [c["name"] for c in catalog.columns_of(found["id"])]


def resolve_entity_key(workspace_id: str, table_ref: str, entity: str = "") -> str:
    """推断主体键列：同名物理列 → <entity>_id → 第一个 *_id 列 → 首列。"""
    _, cols = _columns_of(workspace_id, table_ref)
    if not cols:
        return ""
    lowered = {c.lower(): c for c in cols}
    ent = (entity or "").strip()
    if ent:
        if ent.lower() in lowered:
            return lowered[ent.lower()]
        for cand in (f"{ent}_id", f"{ent}id", f"{ent}_key"):
            if cand.lower() in lowered:
                return lowered[cand.lower()]
    for c in cols:
        if c.lower().endswith("_id"):
            return c
    return cols[0]


def _is_aggregate(expr: str) -> bool:
    return bool(AGG_RE.search(expr or ""))


def _is_numeric(dtype: str) -> bool:
    return (dtype or "").strip().lower() in NUMERIC_DTYPES


# ------------------------------------------------------------------ 特征 CRUD


def _shape(row: dict) -> dict:
    row = dict(row)
    row["stats"] = _loads(row.get("stats"), {})
    row["is_aggregate"] = _is_aggregate(row.get("expr") or "")
    return row


def list_features(workspace_id: str, entity: str = "", keyword: str = "") -> list[dict]:
    sql = "SELECT * FROM features WHERE workspace_id = ?"
    params: list[Any] = [workspace_id]
    if entity:
        sql += " AND entity = ?"
        params.append(entity)
    sql += " ORDER BY created_at DESC"
    rows = [_shape(r) for r in store.rows(sql, params)]
    if keyword:
        kw = keyword.lower()
        rows = [
            r for r in rows
            if kw in (r.get("name") or "").lower()
            or kw in (r.get("description") or "").lower()
            or kw in (r.get("table_ref") or "").lower()
        ]
    return rows


def get_feature(workspace_id: str, ref: str) -> dict | None:
    """按 id 或 name 取特征。"""
    row = store.row(
        "SELECT * FROM features WHERE workspace_id = ? AND id = ?", (workspace_id, ref)
    )
    if not row:
        row = store.row(
            "SELECT * FROM features WHERE workspace_id = ? AND name = ?", (workspace_id, ref)
        )
    if not row:
        return None
    return _shape(row)


def create_feature(workspace_id: str, payload: dict) -> dict:
    name = re.sub(r"[^\w\u4e00-\u9fa5]", "_", (payload.get("name") or "").strip()).lower()
    if not name:
        raise ValueError("特征名不能为空")
    if store.row("SELECT id FROM features WHERE workspace_id = ? AND name = ?", (workspace_id, name)):
        raise ValueError(f"特征 {name} 已存在，请用更新接口（版本会自动自增）")

    expr = (payload.get("expr") or "").strip()
    table_ref = (payload.get("table_ref") or "").strip()
    if not expr or not table_ref:
        raise ValueError("expr 与 table_ref 必填")

    found, cols = _columns_of(workspace_id, table_ref)
    if not found:
        raise ValueError(f"表 {table_ref} 不存在")

    entity = (payload.get("entity") or "").strip()
    dtype = (payload.get("dtype") or "float").strip() or "float"
    if dtype not in DTYPES:
        dtype = "float"

    _validate_expr(workspace_id, found["ref"], expr, entity, cols)

    row = {
        "id": store.new_id("ft"),
        "workspace_id": workspace_id,
        "name": name,
        "entity": entity or resolve_entity_key(workspace_id, found["ref"], ""),
        "dtype": dtype,
        "expr": expr,
        "table_ref": found["ref"],
        "description": payload.get("description") or "",
        "stats": "{}",
        "version": 1,
        "status": payload.get("status") or "published",
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("features", row)
    _ensure_revision_table()
    _snapshot_revision(row)
    record_lineage(workspace_id, row["table_ref"], row["name"])
    return _shape(row)


def _validate_expr(workspace_id: str, ref: str, expr: str, entity: str, cols: list[str]) -> None:
    """用 LIMIT 0 试跑，确保表达式可执行（不产生扫描开销）。"""
    key = resolve_entity_key(workspace_id, ref, entity)
    if _is_aggregate(expr) and key:
        probe = f"SELECT {key} AS k, {expr} AS v FROM {ref} GROUP BY {key} LIMIT 0"
    else:
        probe = f"SELECT {expr} AS v FROM {ref} LIMIT 0"
    try:
        catalog.run_workspace_sql(workspace_id, probe, limit=1)
    except Exception as exc:  # noqa: BLE001 - 把引擎报错翻译成人话
        raise ValueError(f"表达式无法执行：{exc}") from exc


def update_feature(workspace_id: str, feature_id: str, payload: dict) -> dict:
    row = store.row(
        "SELECT * FROM features WHERE id = ? AND workspace_id = ?", (feature_id, workspace_id)
    )
    if not row:
        raise ValueError("特征不存在")

    patch: dict[str, Any] = {}
    for field in ("description", "status", "dtype"):
        if payload.get(field) is not None:
            patch[field] = payload[field]

    version_bump = False
    if payload.get("expr") is not None:
        expr = (payload["expr"] or "").strip()
        if not expr:
            raise ValueError("expr 不能为空")
        if expr != row["expr"]:
            patch["expr"] = expr
            version_bump = True
    if payload.get("table_ref") is not None:
        found, cols = _columns_of(workspace_id, payload["table_ref"])
        if not found:
            raise ValueError(f"表 {payload['table_ref']} 不存在")
        if found["ref"] != row["table_ref"]:
            patch["table_ref"] = found["ref"]
            version_bump = True
    if payload.get("entity") is not None and (payload["entity"] or "") != (row["entity"] or ""):
        patch["entity"] = payload["entity"]
        version_bump = True

    # 表达式或来源变化时先校验新组合可执行
    if "expr" in patch or "table_ref" in patch or "entity" in patch:
        ref = patch.get("table_ref", row["table_ref"])
        expr = patch.get("expr", row["expr"])
        entity = patch.get("entity", row.get("entity") or "")
        _, cols = _columns_of(workspace_id, ref)
        _validate_expr(workspace_id, ref, expr, entity, cols)

    if version_bump:
        patch["version"] = int(row.get("version") or 1) + 1
        patch["stats"] = "{}"  # 定义变了，旧统计失效
    patch["updated_at"] = store.now()

    store.update("features", feature_id, patch)
    merged = {**row, **patch}
    _ensure_revision_table()
    _snapshot_revision(merged)
    if "table_ref" in patch:
        record_lineage(workspace_id, patch["table_ref"], merged["name"])
    return _shape(store.row("SELECT * FROM features WHERE id = ?", (feature_id,)) or merged)


def delete_feature(workspace_id: str, feature_id: str) -> None:
    row = store.row(
        "SELECT * FROM features WHERE id = ? AND workspace_id = ?", (feature_id, workspace_id)
    )
    if not row:
        raise ValueError("特征不存在")
    store.execute("DELETE FROM features WHERE id = ?", (feature_id,))
    _ensure_revision_table()
    store.execute("DELETE FROM feature_revisions WHERE feature_id = ?", (feature_id,))
    store.execute(
        "DELETE FROM lineage WHERE workspace_id = ? AND downstream = ?",
        (workspace_id, f"feature:{row['name']}"),
    )


# ------------------------------------------------------------------ 版本


def _snapshot_revision(row: dict) -> None:
    _ensure_revision_table()
    store.insert(
        "feature_revisions",
        {
            "id": store.new_id("fr"),
            "feature_id": row.get("id"),
            "workspace_id": row.get("workspace_id"),
            "name": row.get("name"),
            "version": int(row.get("version") or 1),
            "entity": row.get("entity") or "",
            "dtype": row.get("dtype") or "float",
            "expr": row.get("expr") or "",
            "table_ref": row.get("table_ref") or "",
            "description": row.get("description") or "",
            "stats": row.get("stats") if isinstance(row.get("stats"), str) else _dumps(row.get("stats") or {}),
            "status": row.get("status") or "published",
            "created_at": store.now(),
        },
    )


def list_versions(workspace_id: str, feature_ref: str) -> dict:
    """返回当前版本 + 历史版本（按版本号倒序）。"""
    feature = get_feature(workspace_id, feature_ref)
    if not feature:
        raise ValueError("特征不存在")
    _ensure_revision_table()
    history = store.rows(
        "SELECT * FROM feature_revisions WHERE feature_id = ? ORDER BY version DESC, created_at DESC",
        (feature["id"],),
    )
    for h in history:
        h["stats"] = _loads(h.get("stats"), {})
        h["current"] = int(h.get("version") or 0) == int(feature.get("version") or 0)
    return {
        "feature": feature,
        "current_version": feature.get("version"),
        "versions": history,
    }


# ------------------------------------------------------------------ 统计 / 计算


def _where(entity_key: str, entity_ids: list[str] | None) -> str:
    if not entity_ids or not entity_key:
        return ""
    values = ", ".join(_lit(v) for v in [str(e) for e in entity_ids][:500])
    return f" WHERE {entity_key} IN ({values})"


def _aggregate_sql(
    ref: str, expr: str, dtype: str, entity_key: str, entity_ids: list[str] | None
) -> str:
    """特征统计 SQL。

    聚合特征（如 sum(amount)）必须先按主体键收敛成子查询，再对特征向量做统计 ——
    直接嵌套聚合函数会被引擎拒绝。行级特征则直接对列做统计。
    """
    if _is_aggregate(expr):
        if entity_key:
            inner = (
                f"SELECT {expr} AS v FROM {ref}{_where(entity_key, entity_ids)} "
                f"GROUP BY {entity_key}"
            )
        else:
            inner = f"SELECT {expr} AS v FROM {ref}{_where(entity_key, entity_ids)}"
        source, value = f"({inner}) sub", "v"
    else:
        source, value = f"{ref}{_where(entity_key, entity_ids)}", expr

    parts = [
        "count(*) AS total",
        f"count({value}) AS non_null",
        f"count(DISTINCT {value}) AS distinct_cnt",
        f"min({value}) AS min_v",
        f"max({value}) AS max_v",
    ]
    if _is_numeric(dtype):
        parts.append(f"avg({value}) AS mean_v")
        parts.append(f"stddev_pop({value}) AS std_v")
    return f"SELECT {', '.join(parts)} FROM {source}"


def _preview_sql(
    ref: str, expr: str, entity_key: str, entity_ids: list[str] | None, limit: int
) -> str:
    where = _where(entity_key, entity_ids)
    if _is_aggregate(expr) and entity_key:
        return (
            f"SELECT {entity_key} AS entity_id, {expr} AS value FROM {ref}{where} "
            f"GROUP BY {entity_key} LIMIT {int(limit)}"
        )
    lead = f"{entity_key} AS entity_id, " if entity_key else ""
    return f"SELECT {lead}{expr} AS value FROM {ref}{where} LIMIT {int(limit)}"


def _stats_from_row(row: list[Any], columns: list[str], dtype: str) -> dict:
    rec = dict(zip([c.lower() for c in columns], row))
    total = rec.get("total") or 0
    non_null = rec.get("non_null") or 0
    try:
        total_i = int(total)
        non_null_i = int(non_null)
    except (TypeError, ValueError):
        total_i, non_null_i = 0, 0
    stats: dict[str, Any] = {
        "count": total_i,
        "non_null": non_null_i,
        "null_count": max(total_i - non_null_i, 0),
        "null_rate": round((total_i - non_null_i) / total_i, 6) if total_i else 0.0,
        "distinct": rec.get("distinct_cnt") or 0,
        "min": rec.get("min_v"),
        "max": rec.get("max_v"),
        "dtype": dtype,
    }
    if _is_numeric(dtype):
        mean = rec.get("mean_v")
        std = rec.get("std_v")
        stats["mean"] = round(float(mean), 6) if mean is not None else None
        stats["std"] = round(float(std), 6) if std is not None else None
    return stats


def compute_features(
    workspace_id: str,
    names: list[str] | None = None,
    entity_ids: list[str] | None = None,
    limit: int = 20,
    materialize: bool = False,
) -> dict:
    """物化计算特征：跑统计 + 取样本，统计落 features.stats。

    names 为空时计算全部已发布特征。
    """
    targets: list[dict] = []
    if names:
        for ref in names:
            feature = get_feature(workspace_id, ref)
            if not feature:
                raise ValueError(f"特征 {ref} 不存在")
            targets.append(feature)
    else:
        targets = list_features(workspace_id)

    computed: list[dict] = []
    for feature in targets:
        found = catalog.resolve(workspace_id, feature["table_ref"])
        if not found:
            computed.append({**feature, "error": f"来源表 {feature['table_ref']} 不存在", "stats": {}})
            continue
        ref = found["ref"]
        entity_key = resolve_entity_key(workspace_id, ref, feature.get("entity") or "")
        dtype = feature.get("dtype") or "float"

        stats: dict[str, Any] = {}
        error = ""
        agg_sql = _aggregate_sql(ref, feature["expr"], dtype, entity_key, entity_ids)
        try:
            out = catalog.run_workspace_sql(workspace_id, agg_sql, limit=2)
            if out.get("rows"):
                stats = _stats_from_row(out["rows"][0], out.get("columns") or [], dtype)
        except Exception as exc:  # noqa: BLE001 - 降级为只统计行数
            try:
                out = catalog.run_workspace_sql(
                    workspace_id,
                    f"SELECT count(*) AS total FROM {ref}{_where(entity_key, entity_ids)}",
                    limit=2,
                )
                stats = {
                    "count": int(out["rows"][0][0] or 0) if out.get("rows") else 0,
                    "dtype": dtype,
                    "note": f"仅统计行数：{exc}",
                }
            except Exception as exc2:  # noqa: BLE001
                error = str(exc2)

        preview = {"columns": [], "rows": [], "row_count": 0}
        preview_sql = _preview_sql(ref, feature["expr"], entity_key, entity_ids, limit)
        try:
            out = catalog.run_workspace_sql(workspace_id, preview_sql, limit=limit)
            preview = {
                "columns": out.get("columns") or [],
                "rows": out.get("rows") or [],
                "row_count": out.get("row_count") or 0,
            }
        except Exception as exc:  # noqa: BLE001
            error = error or str(exc)

        patch: dict[str, Any] = {"updated_at": store.now()}
        if stats:
            patch["stats"] = _dumps(stats)
        store.update("features", feature["id"], patch)

        item = {
            "id": feature["id"],
            "name": feature["name"],
            "entity": feature.get("entity"),
            "entity_key": entity_key,
            "dtype": dtype,
            "expr": feature["expr"],
            "table_ref": feature["table_ref"],
            "version": feature.get("version"),
            "stats": stats,
            "preview": preview,
            "sql": agg_sql,
            "aggregate": _is_aggregate(feature["expr"]),
        }
        if error:
            item["error"] = error
        if materialize:
            item["materialized"] = _materialize(
                workspace_id,
                found,
                f"_feature_{_ident(feature['name'])}",
                preview_sql,
            )
        computed.append(item)

    return {"computed_at": store.now(), "count": len(computed), "features": computed}


def _materialize(workspace_id: str, found: dict, table_name: str, sql: str) -> dict:
    """把查询结果落成本地 DuckDB 快照表（尽力而为，失败不影响主流程）。"""
    try:
        mapping = catalog.mapping(workspace_id)
        tables = catalog.bare_tables(workspace_id)
        qualified = warehouse.qualify(sql, mapping, tables)
        phys = warehouse.physical_name(found["catalog_name"], found["schema_name"])
        target = f'"{phys}"."{_ident(table_name)}"'
        warehouse.execute_ddl(f"CREATE OR REPLACE TABLE {target} AS {qualified}")
        count = warehouse.count_rows(
            found["catalog_name"], found["schema_name"], _ident(table_name)
        )
        return {"table": f"{found['catalog_name']}.{found['schema_name']}.{_ident(table_name)}", "rows": count}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# ------------------------------------------------------------------ 训练集


def build_training_set(
    workspace_id: str,
    feature_names: list[str],
    label_expr: str = "",
    table_ref: str = "",
    entity_ids: list[str] | None = None,
    limit: int = 500,
    materialize: bool = False,
    name: str = "",
) -> dict:
    """按主体键把多个特征拼成训练宽表并执行。

    表达式与在线推理共用同一份特征定义 —— 训练 / 推理一致性由此保证。
    """
    if not feature_names:
        raise ValueError("至少选择一个特征")

    features: list[dict] = []
    for ref in feature_names:
        feature = get_feature(workspace_id, ref)
        if not feature:
            raise ValueError(f"特征 {ref} 不存在")
        features.append(feature)

    base_ref = (table_ref or features[0]["table_ref"]).strip()
    base_found, _ = _columns_of(workspace_id, base_ref)
    if not base_found:
        raise ValueError(f"表 {base_ref} 不存在")
    base_ref = base_found["ref"]
    base_key = resolve_entity_key(workspace_id, base_ref, features[0].get("entity") or "")
    if not base_key:
        raise ValueError(f"表 {base_ref} 无法确定主体键列")

    label = (label_expr or "").strip()
    warnings: list[str] = []
    # 粒度：只要有一个聚合特征（或聚合标签），宽表就必须收敛到「一行一个主体」
    entity_grain = any(_is_aggregate(f["expr"]) for f in features) or (
        bool(label) and _is_aggregate(label)
    )

    if entity_grain:
        sql = _entity_grain_sql(
            workspace_id, base_ref, base_key, features, label, entity_ids, limit, warnings
        )
    else:
        sql = _row_grain_sql(workspace_id, base_ref, base_key, features, label, entity_ids, limit)

    out = catalog.run_workspace_sql(workspace_id, sql, limit=limit)
    result = {
        "sql": sql,
        "granularity": "entity" if entity_grain else "row",
        "columns": out.get("columns") or [],
        "rows": out.get("rows") or [],
        "row_count": out.get("row_count") or 0,
        "elapsed_ms": out.get("elapsed_ms"),
        "feature_names": [f["name"] for f in features],
        "entity_key": base_key,
        "table_ref": base_ref,
        "label": label,
        "warnings": warnings,
        "consistency": {
            "expr_source": "features 表（与在线推理共用同一份定义）",
            "versions": {f["name"]: f.get("version") for f in features},
        },
    }
    if materialize:
        table_name = _ident(name) if name else "training_set_" + "_".join(
            _ident(f["name"]) for f in features[:3]
        )
        result["materialized"] = _materialize(workspace_id, base_found, table_name, sql)
    return result


def _entity_grain_sql(
    workspace_id: str,
    base_ref: str,
    base_key: str,
    features: list[dict],
    label: str,
    entity_ids: list[str] | None,
    limit: int,
    warnings: list[str],
) -> str:
    """主体粒度宽表：一行一个主体键，每个特征一个按主体收敛的子查询。"""
    select_parts = ["b.entity_id"]
    joins: list[str] = []

    for i, feature in enumerate(features, start=1):
        found = catalog.resolve(workspace_id, feature["table_ref"])
        if not found:
            raise ValueError(f"特征 {feature['name']} 的来源表不存在")
        ref = found["ref"]
        expr = feature["expr"]
        alias = f"f{i}"
        if _is_aggregate(expr):
            sub = f"SELECT {base_key} AS _k, {expr} AS _v FROM {ref} GROUP BY {base_key}"
        else:
            # 行级特征在主体粒度上不唯一，取任意一行并显式提示
            sub = (
                f"SELECT {base_key} AS _k, any_value({expr}) AS _v FROM {ref} "
                f"GROUP BY {base_key}"
            )
            warnings.append(
                f"特征 {feature['name']} 是行级表达式，在主体粒度宽表中按 any_value 取值"
            )
        joins.append(f"LEFT JOIN ({sub}) {alias} ON {alias}._k = b.entity_id")
        select_parts.append(f'{alias}._v AS "{_ident(feature["name"])}"')

    if label:
        if _is_aggregate(label):
            sub = f"SELECT {base_key} AS _k, {label} AS _v FROM {base_ref} GROUP BY {base_key}"
        else:
            sub = (
                f"SELECT {base_key} AS _k, any_value({label}) AS _v FROM {base_ref} "
                f"GROUP BY {base_key}"
            )
            warnings.append("标签是行级表达式，在主体粒度宽表中按 any_value 取值")
        joins.append(f"LEFT JOIN ({sub}) lb ON lb._k = b.entity_id")
        select_parts.append("lb._v AS label")

    sql = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM (SELECT DISTINCT {base_key} AS entity_id FROM {base_ref}"
        f"{_where(base_key, entity_ids)}) b"
    )
    if joins:
        sql += " " + " ".join(joins)
    return sql + f" LIMIT {int(limit)}"


def _row_grain_sql(
    workspace_id: str,
    base_ref: str,
    base_key: str,
    features: list[dict],
    label: str,
    entity_ids: list[str] | None,
    limit: int,
) -> str:
    """行粒度宽表：一行一条源记录，所有特征都是行级表达式。"""
    select_parts = [f"b.{base_key} AS entity_id"]
    joins: list[str] = []
    used_tables: dict[str, str] = {base_ref: "b"}
    alias_seq = 0

    for feature in features:
        found = catalog.resolve(workspace_id, feature["table_ref"])
        if not found:
            raise ValueError(f"特征 {feature['name']} 的来源表不存在")
        ref = found["ref"]
        alias_out = _ident(feature["name"])
        alias = used_tables.get(ref)
        if not alias:
            alias_seq += 1
            alias = f"t{alias_seq}"
            used_tables[ref] = alias
            key = resolve_entity_key(workspace_id, ref, feature.get("entity") or "")
            if not key:
                raise ValueError(f"特征 {feature['name']} 的来源表 {ref} 无法确定主体键列")
            joins.append(f"LEFT JOIN {ref} {alias} ON {alias}.{key} = b.{base_key}")
        prefix = "" if alias == "b" else f"{alias}."
        select_parts.append(f'{prefix}{feature["expr"]} AS "{alias_out}"')

    if label:
        select_parts.append(f"{label} AS label")

    sql = f"SELECT {', '.join(select_parts)} FROM {base_ref} b"
    if joins:
        sql += " " + " ".join(joins)
    sql += _where(f"b.{base_key}", entity_ids)
    return sql + f" LIMIT {int(limit)}"


# ------------------------------------------------------------------ 血缘 / 概览


def record_lineage(workspace_id: str, table_ref: str, feature_name: str) -> None:
    """特征血缘：table_ref -> feature:<name>（幂等）。"""
    downstream = f"feature:{feature_name}"
    existing = store.row(
        "SELECT id FROM lineage WHERE workspace_id = ? AND upstream = ? AND downstream = ?",
        (workspace_id, table_ref, downstream),
    )
    if existing:
        return
    store.insert(
        "lineage",
        {
            "id": store.new_id("ln"),
            "workspace_id": workspace_id,
            "upstream": table_ref,
            "downstream": downstream,
            "kind": "feature",
            "detail": _dumps({"expr": feature_name}),
            "created_at": store.now(),
        },
    )


def feature_lineage(workspace_id: str, feature_ref: str) -> dict:
    """特征血缘反查：上游表 / 列，下游引用，关联指标与质量规则。"""
    feature = get_feature(workspace_id, feature_ref)
    if not feature:
        raise ValueError("特征不存在")
    found = catalog.resolve(workspace_id, feature["table_ref"])
    upstream: dict[str, Any] = {"table_ref": feature["table_ref"], "columns": []}
    if found:
        cols = catalog.columns_of(found["id"])
        used = [c["name"] for c in cols if re.search(rf"\b{re.escape(c['name'])}\b", feature["expr"])]
        upstream = {
            "table_ref": found["ref"],
            "table_id": found["id"],
            "name": found["name"],
            "row_count": found.get("row_count"),
            "columns": used or [c["name"] for c in cols],
            "entity_key": resolve_entity_key(workspace_id, found["ref"], feature.get("entity") or ""),
        }

    downstream = store.rows(
        "SELECT * FROM lineage WHERE workspace_id = ? AND downstream = ?",
        (workspace_id, f"feature:{feature['name']}"),
    )
    metrics = store.rows(
        "SELECT id, name, display_name FROM metrics WHERE workspace_id = ? AND table_ref = ?",
        (workspace_id, feature["table_ref"]),
    )
    rules = store.rows(
        "SELECT id, column_name, rule_type, last_status FROM quality_rules "
        "WHERE workspace_id = ? AND table_ref = ?",
        (workspace_id, feature["table_ref"]),
    )
    return {
        "feature": feature,
        "upstream": upstream,
        "downstream": downstream,
        "related": {"metrics": metrics, "quality_rules": rules},
    }


def table_features(workspace_id: str, table_ref: str) -> list[dict]:
    """从 table_ref 反查建在它上面的特征。"""
    found = catalog.resolve(workspace_id, table_ref)
    target = (found["ref"] if found else table_ref or "").lower()
    rows = store.rows("SELECT * FROM features WHERE workspace_id = ?", (workspace_id,))
    return [_shape(r) for r in rows if (r.get("table_ref") or "").lower() == target]


def summary(workspace_id: str) -> dict:
    rows = list_features(workspace_id)
    null_rates = [
        float(r["stats"].get("null_rate") or 0) for r in rows if r.get("stats")
    ]
    entities: dict[str, int] = {}
    for r in rows:
        key = r.get("entity") or "未指定"
        entities[key] = entities.get(key, 0) + 1
    return {
        "total": len(rows),
        "entities": entities,
        "tables": len({r.get("table_ref") for r in rows if r.get("table_ref")}),
        "avg_null_rate": round(sum(null_rates) / len(null_rates), 6) if null_rates else 0.0,
        "computed": len([r for r in rows if r.get("stats")]),
        "dtypes": DTYPES,
    }


def describe_feature(feature: dict) -> str:
    """给前端 / Agent 看的一句话说明。"""
    kind = "聚合" if _is_aggregate(feature.get("expr") or "") else "行级"
    return (
        f"{feature.get('name')}（{feature.get('dtype')}，{kind}）"
        f"= {feature.get('expr')} @ {feature.get('table_ref')}"
    )
