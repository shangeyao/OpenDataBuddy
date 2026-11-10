"""端到端血缘：表级 / 字段级血缘记录、双向图查询、影响分析与 SQL 自动解析。

设计要点
--------
* 元数据库里的 ``lineage`` 表只有 upstream/downstream/kind/detail 四个业务列。
  字段级血缘需要 src_column / dst_column / transform 三列，本模块用
  ``ensure_columns()`` 做一次幂等 ``ALTER TABLE ADD COLUMN`` 补齐，**不改 store.py**。
* 节点用"引用字符串"表示，并支持带类型前缀的资产：
  ``catalog.schema.table``（表）、``dashboard:零售经营总览``、``metric:gmv``、
  ``model:churn_v2``、``volume:raw_files`` 等。``parse_ref()`` 负责识别类型，
  这样影响分析才能把下游分成 表 / 仪表盘 / 指标 / 模型 几类。
* 所有写入都是幂等的：同一组 (upstream, downstream, kind, src_column, dst_column)
  只会存在一行；重复记录只更新 detail / transform 等描述性字段。
* 图查询用双向 BFS：从中心节点向上游、下游各走 ``depth`` 层，然后把"两端都
  落在可达集合内"的边全部收进来，避免只画到一半的悬空边。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

from . import store

# --------------------------------------------------------------------------- 常量

#: 字段级血缘需要的扩展列（缺列时自动 ALTER TABLE 补齐）
FIELD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("src_column", "TEXT"),
    ("dst_column", "TEXT"),
    ("transform", "TEXT"),
)

#: 带前缀的资产类型。没有前缀的引用一律视为表。
REF_TYPES: dict[str, str] = {
    "table": "table",
    "tables": "table",
    "view": "table",
    "dashboard": "dashboard",
    "report": "dashboard",
    "metric": "metric",
    "model": "model",
    "volume": "volume",
    "source": "source",
    "file": "file",
    "app": "app",
    "api": "api",
    "notebook": "notebook",
}

TYPE_LABELS: dict[str, str] = {
    "table": "表",
    "dashboard": "仪表盘",
    "metric": "指标",
    "model": "模型",
    "volume": "卷",
    "source": "数据源",
    "file": "文件",
    "app": "应用",
    "api": "接口",
    "notebook": "Notebook",
    "column": "字段",
}

#: 血缘 kind 的中文说明，前端下拉与图例直接用。
KIND_LABELS: dict[str, str] = {
    "sql": "SQL 加工",
    "ctas": "CTAS 建表",
    "insert": "INSERT 写入",
    "merge": "MERGE 合并",
    "select": "查询引用",
    "etl": "ETL 作业",
    "stream": "实时同步",
    "cdc": "CDC 捕获",
    "manual": "手工登记",
    "dashboard": "看板引用",
    "metric": "指标引用",
    "model": "模型训练",
    "copy": "数据拷贝",
}

DEFAULT_KIND = "sql"

_ensured = False


# ------------------------------------------------------------------- 表结构扩展


def ensure_columns(force: bool = False) -> list[str]:
    """给 lineage 表补齐字段级血缘需要的列，返回本次新增的列名。

    表还不存在时（首次启动、store.init_db 尚未执行）不缓存状态，
    下一次调用会重试，避免永久跳过迁移。
    """
    global _ensured
    if _ensured and not force:
        return []
    added: list[str] = []
    try:
        with store.db() as conn:
            existing = {r[1] for r in conn.execute("PRAGMA table_info(lineage)").fetchall()}
            if not existing:
                return []  # 表还没建，留给 init_db
            for name, decl in FIELD_COLUMNS:
                if name not in existing:
                    conn.execute(f'ALTER TABLE "lineage" ADD COLUMN "{name}" {decl}')
                    added.append(name)
        _ensured = True
    except Exception:  # noqa: BLE001 - 迁移失败不应该拖垮查询
        return added
    return added


# ----------------------------------------------------------------------- 工具


def _key(ref: Any) -> str:
    return str(ref or "").strip().lower()


def normalize_ref(ref: Any) -> str:
    """去掉多余空白与包裹引号，保留原始大小写用于展示。"""
    text = str(ref or "").strip()
    if not text:
        return ""
    if text.startswith(("'", '"', "`")) and text.endswith(text[0]) and len(text) > 1:
        text = text[1:-1]
    return re.sub(r"\s*\.\s*", ".", text).strip()


def parse_ref(ref: Any) -> tuple[str, str]:
    """把引用拆成 (类型, 名称)。``dashboard:首页看板`` -> ('dashboard', '首页看板')。"""
    text = normalize_ref(ref)
    if not text:
        return "table", ""
    if ":" in text:
        prefix, _, rest = text.partition(":")
        kind = REF_TYPES.get(prefix.strip().lower())
        if kind and rest.strip():
            return kind, rest.strip()
    return "table", text


def type_label(typ: str) -> str:
    return TYPE_LABELS.get(typ, typ or "-")


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind or "-")


def _row_edge(row: dict) -> dict:
    """数据库行 -> 统一的边结构。"""
    up = normalize_ref(row.get("upstream"))
    down = normalize_ref(row.get("downstream"))
    src = (row.get("src_column") or "").strip()
    dst = (row.get("dst_column") or "").strip()
    return {
        "id": row.get("id") or f"{_key(up)}->{_key(down)}",
        "workspace_id": row.get("workspace_id") or "",
        "upstream": up,
        "downstream": down,
        "kind": row.get("kind") or DEFAULT_KIND,
        "detail": row.get("detail") or "",
        "src_column": src,
        "dst_column": dst,
        "transform": (row.get("transform") or "").strip(),
        "created_at": row.get("created_at") or "",
        "column_level": bool(src or dst),
    }


def _node(ref: str, level: int = 0, side: str = "center") -> dict:
    typ, name = parse_ref(ref)
    parts = [p for p in name.split(".") if p]
    node: dict[str, Any] = {
        "id": ref,
        "ref": ref,
        "label": parts[-1] if typ == "table" and parts else name,
        "name": name,
        "type": typ,
        "type_label": type_label(typ),
        "level": level,
        "side": side,
        "distance": abs(level),
    }
    if typ == "table":
        node["catalog"] = parts[0] if len(parts) >= 3 else ""
        node["schema"] = parts[-2] if len(parts) >= 2 else ""
        node["table"] = parts[-1] if parts else ""
    return node


def _direction(direction: str | None) -> str:
    value = (direction or "both").strip().lower()
    if value in ("up", "upstream", "upstreams", "source", "sources", "in"):
        return "upstream"
    if value in ("down", "downstream", "downstreams", "target", "targets", "out"):
        return "downstream"
    return "both"


# ----------------------------------------------------------------------- 记录


def record(
    upstream: str,
    downstream: str,
    kind: str = DEFAULT_KIND,
    src_column: str | None = None,
    dst_column: str | None = None,
    transform: str = "",
    detail: str = "",
    workspace_id: str | None = None,
) -> dict:
    """幂等写入一条血缘。返回 ``{"id":..., "created": bool, ...}``。"""
    ensure_columns()
    up = normalize_ref(upstream)
    down = normalize_ref(downstream)
    if not up or not down:
        raise ValueError("upstream 与 downstream 都不能为空")
    if _key(up) == _key(down) and not (src_column or dst_column):
        raise ValueError("upstream 与 downstream 不能是同一条记录")
    kind = (kind or DEFAULT_KIND).strip() or DEFAULT_KIND
    src = (src_column or "").strip()
    dst = (dst_column or "").strip()

    existing = store.row(
        "SELECT * FROM lineage WHERE lower(trim(upstream)) = ? AND lower(trim(downstream)) = ? "
        "AND lower(trim(ifnull(kind, ''))) = ? AND ifnull(src_column, '') = ? "
        "AND ifnull(dst_column, '') = ?",
        (_key(up), _key(down), kind.lower(), src, dst),
    )
    if existing:
        patch: dict[str, Any] = {}
        if transform and (existing.get("transform") or "") != transform:
            patch["transform"] = transform
        if detail and (existing.get("detail") or "") != detail:
            patch["detail"] = detail
        if workspace_id and not existing.get("workspace_id"):
            patch["workspace_id"] = workspace_id
        if patch:
            store.update("lineage", existing["id"], patch)
        row = {**existing, **patch}
        edge = _row_edge(row)
        edge["created"] = False
        return edge

    payload = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "upstream": up,
        "downstream": down,
        "kind": kind,
        "detail": detail or "",
        "src_column": src,
        "dst_column": dst,
        "transform": transform or "",
        "created_at": store.now(),
    }
    store.insert("lineage", payload)
    edge = _row_edge(payload)
    edge["created"] = True
    return edge


def record_many(workspace_id: str | None, relations: Iterable[dict]) -> dict:
    """批量写入，返回 created/updated 计数。"""
    created = updated = 0
    for rel in relations or []:
        if not isinstance(rel, dict):
            continue
        edge = record(
            rel.get("upstream") or rel.get("source") or "",
            rel.get("downstream") or rel.get("target") or "",
            kind=rel.get("kind") or DEFAULT_KIND,
            src_column=rel.get("src_column"),
            dst_column=rel.get("dst_column"),
            transform=rel.get("transform") or "",
            detail=rel.get("detail") or "",
            workspace_id=workspace_id,
        )
        if edge.get("created"):
            created += 1
        else:
            updated += 1
    return {"created": created, "updated": updated, "total": created + updated}


def delete(edge_id: str) -> None:
    store.delete("lineage", edge_id)


# ----------------------------------------------------------------------- 读取


def all_edges(workspace_id: str | None = None, limit: int = 5000) -> list[dict]:
    """读取全部血缘边（本地单机规模足够，一次拉全图更好算）。"""
    ensure_columns()
    sql = "SELECT * FROM lineage"
    params: list[Any] = []
    if workspace_id:
        sql += " WHERE workspace_id = ? OR workspace_id IS NULL OR workspace_id = ''"
        params.append(workspace_id)
    sql += " ORDER BY created_at, rowid"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = store.rows(sql, params)
    edges = []
    for row in rows:
        edge = _row_edge(row)
        if edge["upstream"] and edge["downstream"]:
            edges.append(edge)
    return edges


def table_edges(workspace_id: str | None = None) -> list[dict]:
    """只要表级边（字段级边会被折叠进 column_map）。"""
    return [e for e in all_edges(workspace_id) if not e["column_level"]]


def _index(edges: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, str]]:
    children: dict[str, list[dict]] = defaultdict(list)
    parents: dict[str, list[dict]] = defaultdict(list)
    refs: dict[str, str] = {}
    for e in edges:
        u, d = _key(e["upstream"]), _key(e["downstream"])
        if not u or not d:
            continue
        refs.setdefault(u, e["upstream"])
        refs.setdefault(d, e["downstream"])
        children[u].append(e)
        parents[d].append(e)
    return children, parents, refs


def _reachable(
    center_key: str, depth: int, edges: list[dict], direction: str
) -> tuple[dict[str, dict], dict[str, str]]:
    """双向 BFS，返回 {key: {level, side}} 与 {key: 原始引用}。"""
    children, parents, refs = _index(edges)
    found: dict[str, dict] = {center_key: {"level": 0, "side": "center"}}
    frontier = [center_key]
    for d in range(1, max(1, int(depth)) + 1):
        nxt: list[str] = []
        for key in frontier:
            if direction in ("both", "upstream"):
                for e in parents.get(key, []):
                    k = _key(e["upstream"])
                    if k and k not in found:
                        found[k] = {"level": -d, "side": "upstream"}
                        nxt.append(k)
            if direction in ("both", "downstream"):
                for e in children.get(key, []):
                    k = _key(e["downstream"])
                    if k and k not in found:
                        found[k] = {"level": d, "side": "downstream"}
                        nxt.append(k)
        frontier = nxt
        if not frontier:
            break
    return found, refs


def _collect_edges(
    edges: list[dict], found: dict[str, dict]
) -> tuple[list[dict], dict[tuple[str, str], list[dict]]]:
    """把两端都在可达集合里的边聚合起来（同表对的多条字段级边合成一条）。"""
    pairs: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in edges:
        u, d = _key(e["upstream"]), _key(e["downstream"])
        if u in found and d in found and u != d:
            pairs[(u, d)].append(e)
    out: list[dict] = []
    for (u, d), group in pairs.items():
        group = sorted(group, key=lambda x: (not x["column_level"], x["src_column"], x["dst_column"]))
        columns = [
            {"src_column": g["src_column"], "dst_column": g["dst_column"], "transform": g["transform"]}
            for g in group
            if g["column_level"]
        ]
        kinds = sorted({g["kind"] or DEFAULT_KIND for g in group})
        out.append(
            {
                "id": f"{u}->{d}",
                "from": group[0]["upstream"],
                "to": group[0]["downstream"],
                "kind": kinds[0] if len(kinds) == 1 else "mixed",
                "kinds": kinds,
                "kind_label": kind_label(kinds[0]) if len(kinds) == 1 else "混合",
                "detail": next((g["detail"] for g in group if g["detail"]), ""),
                "column_map": columns,
                "column_count": len(columns),
                "created_at": group[0]["created_at"],
            }
        )
    out.sort(key=lambda e: (_key(e["from"]), _key(e["to"])))
    return out, pairs


def graph(
    table_ref: str,
    depth: int = 3,
    workspace_id: str | None = None,
    direction: str = "both",
) -> dict:
    """双向 BFS 血缘图。节点带 level（上游为负、下游为正）与 side。"""
    center = normalize_ref(table_ref)
    if not center:
        raise ValueError("table_ref 不能为空")
    depth = max(1, min(int(depth or 3), 10))
    direction = _direction(direction)
    edges = all_edges(workspace_id)
    center_key = _key(center)
    found, refs = _reachable(center_key, depth, edges, direction)
    view_edges, _pairs = _collect_edges(edges, found)

    nodes: list[dict] = []
    for key, meta in found.items():
        ref = center if key == center_key else refs.get(key, key)
        node = _node(ref, meta["level"], meta["side"])
        node["is_center"] = key == center_key
        nodes.append(node)
    nodes.sort(key=lambda n: (n["level"], n["label"]))
    for node in nodes:
        node["degree"] = sum(
            1 for e in view_edges if _key(e["from"]) == _key(node["ref"]) or _key(e["to"]) == _key(node["ref"])
        )

    upstream = [n for n in nodes if n["side"] == "upstream"]
    downstream = [n for n in nodes if n["side"] == "downstream"]
    return {
        "view": "table",
        "table_ref": center,
        "depth": depth,
        "direction": direction,
        "nodes": nodes,
        "edges": view_edges,
        "upstream": upstream,
        "downstream": downstream,
        "counts": {
            "nodes": len(nodes),
            "edges": len(view_edges),
            "upstream": len(upstream),
            "downstream": len(downstream),
            "columns": sum(e["column_count"] for e in view_edges),
        },
    }


def table_level_view(
    table_ref: str, depth: int = 3, workspace_id: str | None = None, direction: str = "both"
) -> dict:
    """表级视图：节点是表，边携带聚合后的 column_map。"""
    result = graph(table_ref, depth=depth, workspace_id=workspace_id, direction=direction)
    result["view"] = "table"
    for node in result["nodes"]:
        node["column_count"] = sum(
            1
            for e in result["edges"]
            if _key(e["from"]) == _key(node["ref"]) or _key(e["to"]) == _key(node["ref"])
            for _ in e["column_map"]
        )
    return result


def column_level_view(
    table_ref: str, depth: int = 3, workspace_id: str | None = None, direction: str = "both"
) -> dict:
    """字段级视图：节点是 ``表::字段``，边是字段映射关系。"""
    center = normalize_ref(table_ref)
    if not center:
        raise ValueError("table_ref 不能为空")
    depth = max(1, min(int(depth or 3), 10))
    direction = _direction(direction)
    edges = all_edges(workspace_id)
    center_key = _key(center)
    found, refs = _reachable(center_key, depth, edges, direction)

    def table_ref_of(key: str) -> str:
        return center if key == center_key else refs.get(key, key)

    nodes: dict[str, dict] = {}
    col_edges: list[dict] = []
    for e in edges:
        u, d = _key(e["upstream"]), _key(e["downstream"])
        if u not in found or d not in found:
            continue
        if not e["column_level"]:
            continue
        up_ref, down_ref = table_ref_of(u), table_ref_of(d)
        src = e["src_column"] or ""
        dst = e["dst_column"] or ""
        if src:
            nid = f"{up_ref}::{src}"
            node = nodes.setdefault(
                nid,
                {
                    "id": nid,
                    "ref": nid,
                    "label": src,
                    "name": src,
                    "type": "column",
                    "type_label": type_label("column"),
                    "table": up_ref,
                    "column": src,
                    "level": found[u]["level"],
                    "side": found[u]["side"],
                    "distance": abs(found[u]["level"]),
                    "is_center": u == center_key,
                },
            )
            node["is_center"] = node["is_center"] or u == center_key
        if dst:
            nid = f"{down_ref}::{dst}"
            node = nodes.setdefault(
                nid,
                {
                    "id": nid,
                    "ref": nid,
                    "label": dst,
                    "name": dst,
                    "type": "column",
                    "type_label": type_label("column"),
                    "table": down_ref,
                    "column": dst,
                    "level": found[d]["level"],
                    "side": found[d]["side"],
                    "distance": abs(found[d]["level"]),
                    "is_center": d == center_key,
                },
            )
            node["is_center"] = node["is_center"] or d == center_key
        if src and dst:
            col_edges.append(
                {
                    "id": f"{up_ref}::{src}->{down_ref}::{dst}",
                    "from": f"{up_ref}::{src}",
                    "to": f"{down_ref}::{dst}",
                    "from_table": up_ref,
                    "to_table": down_ref,
                    "src_column": src,
                    "dst_column": dst,
                    "transform": e["transform"],
                    "kind": e["kind"],
                    "kind_label": kind_label(e["kind"]),
                }
            )

    # 没有字段级血缘时，退回把中心表字段当作孤立节点展示（前端提示用户去补录）
    if not nodes:
        for col in _columns_of_ref(workspace_id, center):
            nid = f"{center}::{col}"
            nodes[nid] = {
                "id": nid,
                "ref": nid,
                "label": col,
                "name": col,
                "type": "column",
                "type_label": type_label("column"),
                "table": center,
                "column": col,
                "level": 0,
                "side": "center",
                "distance": 0,
                "is_center": True,
            }

    tables: dict[str, dict] = {}
    for node in nodes.values():
        bucket = tables.setdefault(
            node["table"],
            {
                "ref": node["table"],
                "type": parse_ref(node["table"])[0],
                "level": node["level"],
                "side": node["side"],
                "columns": [],
            },
        )
        bucket["columns"].append(node["id"])
    for bucket in tables.values():
        bucket["columns"].sort()

    node_list = sorted(nodes.values(), key=lambda n: (n["level"], n["table"], n["label"]))
    for node in node_list:
        node["degree"] = sum(
            1 for e in col_edges if e["from"] == node["id"] or e["to"] == node["id"]
        )
    return {
        "view": "column",
        "table_ref": center,
        "depth": depth,
        "direction": direction,
        "nodes": node_list,
        "edges": col_edges,
        "tables": sorted(tables.values(), key=lambda t: (t["level"], t["ref"])),
        "upstream": [n for n in node_list if n["side"] == "upstream"],
        "downstream": [n for n in node_list if n["side"] == "downstream"],
        "counts": {
            "nodes": len(node_list),
            "edges": len(col_edges),
            "upstream": sum(1 for n in node_list if n["side"] == "upstream"),
            "downstream": sum(1 for n in node_list if n["side"] == "downstream"),
            "tables": len(tables),
        },
    }


def _columns_of_ref(workspace_id: str | None, ref: str) -> list[str]:
    """尽力拿到表的字段名（用于没有字段级血缘时的占位展示）。"""
    if not workspace_id:
        return []
    try:
        from . import catalog  # 延迟导入，避免循环依赖

        found = catalog.resolve(workspace_id, ref)
        if not found:
            return []
        return [c["name"] for c in catalog.columns_of(found["id"])]
    except Exception:  # noqa: BLE001
        return []


def impact_analysis(
    table_ref: str, workspace_id: str | None = None, max_depth: int = 5
) -> dict:
    """影响分析：列出该资产变更后会受影响的所有下游资产。"""
    center = normalize_ref(table_ref)
    if not center:
        raise ValueError("table_ref 不能为空")
    max_depth = max(1, min(int(max_depth or 5), 10))
    edges = all_edges(workspace_id)
    center_key = _key(center)
    found, refs = _reachable(center_key, max_depth, edges, "downstream")

    # 记录最短路径，便于前端展开"为什么受影响"
    path_of: dict[str, list[str]] = {center_key: [center]}
    for _ in range(max_depth + 1):
        changed = False
        for key, meta in found.items():
            if meta["level"] <= 0:
                continue
            for e in edges:
                if _key(e["downstream"]) != key:
                    continue
                up = _key(e["upstream"])
                if up in path_of and key not in path_of:
                    path_of[key] = path_of[up] + [refs.get(key, key)]
                    changed = True
        if not changed:
            break

    assets: list[dict] = []
    for key, meta in found.items():
        if key == center_key:
            continue
        ref = refs.get(key, key)
        typ, name = parse_ref(ref)
        assets.append(
            {
                "ref": ref,
                "name": name,
                "label": name.split(".")[-1] if typ == "table" else name,
                "type": typ,
                "type_label": type_label(typ),
                "distance": meta["level"],
                "path": path_of.get(key, [center, ref]),
                "path_text": " → ".join(path_of.get(key, [center, ref])),
            }
        )
    assets.sort(key=lambda a: (a["distance"], a["type"], a["label"]))

    by_type: dict[str, int] = {}
    for a in assets:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    groups = [
        {
            "type": typ,
            "type_label": type_label(typ),
            "count": count,
            "items": [a for a in assets if a["type"] == typ],
        }
        for typ, count in sorted(by_type.items(), key=lambda kv: -kv[1])
    ]
    return {
        "table_ref": center,
        "max_depth": max_depth,
        "total": len(assets),
        "assets": assets,
        "groups": groups,
        "counts": by_type,
        "tables": by_type.get("table", 0),
        "dashboards": by_type.get("dashboard", 0),
        "metrics": by_type.get("metric", 0),
        "models": by_type.get("model", 0),
        "has_impact": bool(assets),
    }


def summary(workspace_id: str | None = None) -> dict:
    """血缘总览：边数、节点数、字段级覆盖、类型分布，供目录页头部展示。"""
    edges = all_edges(workspace_id)
    nodes: dict[str, str] = {}
    by_kind: dict[str, int] = {}
    column_edges = 0
    for e in edges:
        nodes.setdefault(_key(e["upstream"]), e["upstream"])
        nodes.setdefault(_key(e["downstream"]), e["downstream"])
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        if e["column_level"]:
            column_edges += 1
    by_type: dict[str, int] = {}
    for ref in nodes.values():
        typ, _ = parse_ref(ref)
        by_type[typ] = by_type.get(typ, 0) + 1
    return {
        "edges": len(edges),
        "nodes": len(nodes),
        "column_edges": column_edges,
        "table_edges": len(edges) - column_edges,
        "by_kind": by_kind,
        "by_type": by_type,
        "kinds": [
            {"kind": k, "label": kind_label(k), "count": v}
            for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])
        ],
        "types": [
            {"type": k, "label": type_label(k), "count": v}
            for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])
        ],
    }


# ------------------------------------------------------------------ SQL 解析

_IDENT = r'(?:`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_\u4e00-\u9fa5][\w$\u4e00-\u9fa5]*)'
_REF = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}"

_CTAS_RE = re.compile(
    rf"(?is)\bcreate\s+(?:or\s+replace\s+)?"
    rf"(?:(?:temporary|temp|external|transient|global|local|materialized)\s+)*"
    rf"(?P<what>table|view)\s+(?:if\s+not\s+exists\s+)?(?P<ref>{_REF})"
)
_INSERT_RE = re.compile(
    rf"(?is)\binsert\s+(?:(?:overwrite|into)\s+)*(?:table\s+)?(?P<ref>{_REF})"
)
_MERGE_RE = re.compile(rf"(?is)\bmerge\s+into\s+(?P<ref>{_REF})")
_SOURCE_RE = re.compile(rf"(?is)\b(?P<verb>from|join)\s+(?P<ref>{_REF})")
_USING_RE = re.compile(rf"(?is)\busing\s+(?P<ref>{_REF})")

#: 出现在 select 列表里但不是字段名的关键字/类型名，用于过滤误判
_NON_COLUMN = {
    "as", "case", "when", "then", "else", "end", "null", "true", "false", "distinct",
    "cast", "interval", "date", "timestamp", "varchar", "int", "integer", "bigint",
    "double", "float", "decimal", "numeric", "boolean", "string", "text", "sum", "avg",
    "count", "min", "max", "round", "coalesce", "ifnull", "nullif", "over", "partition",
    "by", "order", "group", "and", "or", "not", "is", "in", "like", "between", "asc",
    "desc", "limit", "from", "select", "where", "having", "union", "all", "any", "some",
    "current_date", "current_timestamp", "now",
}


def _strip_sql(sql: str) -> str:
    """去掉注释与字符串字面量，便于结构解析（字符串内容用 '' 占位）。"""
    text = str(sql or "")
    text = re.sub(r"'(?:[^']|'')*'", " '' ", text)
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip()


def _clean_ref(token: str) -> str:
    parts = [p.strip() for p in re.split(r"\s*\.\s*", str(token or "").strip()) if p.strip()]
    out: list[str] = []
    for part in parts:
        if len(part) > 1 and part[0] in '"`[':
            closing = "]" if part[0] == "[" else part[0]
            if part.endswith(closing):
                part = part[1:-1]
        out.append(part.strip())
    return ".".join(out)


def _cte_names(sql: str) -> set[str]:
    names: set[str] = set()
    if not re.match(r"(?is)^\s*with\b", sql):
        return names
    for m in re.finditer(rf"(?is)(?:\bwith\s+(?:recursive\s+)?|,)\s*({_IDENT})\s+as\s*\(", sql):
        names.add(_clean_ref(m.group(1)).lower())
    return names


def _word_at(text: str, index: int, word: str) -> bool:
    if text[index : index + len(word)].lower() != word:
        return False
    before = text[index - 1] if index > 0 else " "
    after = text[index + len(word)] if index + len(word) < len(text) else " "
    return not (before.isalnum() or before in "_$") and not (after.isalnum() or after in "_$")


def _select_items(sql: str) -> list[str]:
    """取第一个 SELECT 的投影列表（按顶层逗号切分）。"""
    m = re.search(r"(?is)\bselect\s+(?:distinct\s+|all\s+)?", sql)
    if not m:
        return []
    rest = sql[m.end() :]
    depth = 0
    cut = len(rest)
    for i, ch in enumerate(rest):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and _word_at(rest, i, "from"):
            cut = i
            break
    return _split_top(rest[:cut])


def _split_top(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    depth = 0
    buf = ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _expr_alias(item: str) -> tuple[str, str]:
    """把 ``sum(x) as gmv`` 拆成 ('sum(x)', 'gmv')。"""
    text = item.strip()
    m = re.match(rf"(?is)^(.*?)\s+as\s+({_IDENT})\s*$", text)
    if m:
        return m.group(1).strip(), _clean_ref(m.group(2))
    m = re.match(rf"(?is)^(.*?)\s+({_IDENT})\s*$", text)
    if m and _clean_ref(m.group(2)).lower() not in _NON_COLUMN:
        return m.group(1).strip(), _clean_ref(m.group(2))
    return text, ""


def _expr_source_column(expr: str) -> str:
    """从投影表达式里推断来源字段（只在能唯一定位时返回）。"""
    body = expr.strip()
    plain = re.fullmatch(rf"(?is)(?:{_IDENT}\s*\.\s*)?({_IDENT})", body)
    if plain:
        name = _clean_ref(plain.group(1))
        if name.lower() not in _NON_COLUMN:
            return name
        return ""
    # 注意 \b：没有它时 `sum(` 会回溯成 `su` 被误当成字段名
    tokens = re.findall(rf"(?<![\w.$])({_IDENT}(?:\s*\.\s*{_IDENT})?)\b(?!\s*\()", body)
    columns: list[str] = []
    for token in tokens:
        name = _clean_ref(token)
        last = name.split(".")[-1]
        if last.lower() in _NON_COLUMN or not last:
            continue
        if name not in columns:
            columns.append(name)
    if len(columns) == 1:
        return columns[-1].split(".")[-1]
    return ""


def auto_discover(sql: str) -> dict:
    """从 SQL 里解析上下游关系。

    支持 CTAS / CREATE VIEW、INSERT INTO / OVERWRITE、MERGE INTO，以及
    FROM / JOIN / USING 里的源表；会自动剔除 CTE 名与表函数。
    """
    raw = str(sql or "").strip()
    prepared = _strip_sql(raw)
    notes: list[str] = []
    if not prepared:
        return {
            "ok": False,
            "target": "",
            "kind": "",
            "sources": [],
            "relations": [],
            "columns": [],
            "ctes": [],
            "notes": ["SQL 为空"],
            "sql": raw,
        }

    ctes = _cte_names(prepared)
    target = ""
    kind = ""
    what = ""

    m = _CTAS_RE.search(prepared)
    if m:
        target = _clean_ref(m.group("ref"))
        what = (m.group("what") or "table").lower()
        kind = "ctas" if what == "table" else "view"
    if not target:
        m = _MERGE_RE.search(prepared)
        if m:
            target = _clean_ref(m.group("ref"))
            kind = "merge"
    if not target:
        m = _INSERT_RE.search(prepared)
        if m:
            candidate = _clean_ref(m.group("ref"))
            if candidate and candidate.lower() not in ("into", "table", "overwrite"):
                target = candidate
                kind = "insert"

    sources: list[str] = []
    for m in _SOURCE_RE.finditer(prepared):
        ref = _clean_ref(m.group("ref"))
        if not ref:
            continue
        tail = prepared[m.end() : m.end() + 2]
        if tail.lstrip().startswith("("):
            continue  # 子查询 / 表函数
        if ref.lower() in ctes or ref.lower().split(".")[-1] in ctes:
            continue
        if ref.lower() in ("select", "values", "unnest", "lateral", "only"):
            continue
        if target and _key(ref) == _key(target):
            continue
        if ref not in sources:
            sources.append(ref)
    for m in _USING_RE.finditer(prepared):
        ref = _clean_ref(m.group("ref"))
        tail = prepared[m.end() : m.end() + 2]
        if not ref or tail.lstrip().startswith("("):
            continue
        if ref.lower() in ctes:
            continue
        if target and _key(ref) == _key(target):
            continue
        if ref not in sources:
            sources.append(ref)

    if not target and not kind:
        kind = "select"
    if ctes:
        notes.append(f"识别到 {len(ctes)} 个 CTE，已从源表中剔除：{', '.join(sorted(ctes))}")
    if not target:
        notes.append("未识别到写入目标（非 CTAS/INSERT/MERGE），仅登记查询引用关系")

    # ---------------------------------------------------- 字段级映射（尽力而为）
    columns: list[dict] = []
    if target and len(sources) == 1:
        items = _select_items(prepared)
        explicit = _insert_columns(prepared, m_insert=None)
        mapped: list[dict] = []
        for i, item in enumerate(items):
            expr, alias = _expr_alias(item)
            src_col = _expr_source_column(expr)
            dst_col = explicit[i] if i < len(explicit) else alias
            if not dst_col and src_col:
                dst_col = src_col
            if not src_col or not dst_col:
                continue
            mapped.append(
                {
                    "src_table": sources[0],
                    "src_column": src_col,
                    "dst_column": dst_col,
                    "transform": expr,
                }
            )
        columns = mapped
        if columns:
            notes.append(f"推断出 {len(columns)} 条字段级映射（单源表投影）")
    elif target and len(sources) > 1:
        notes.append("多源表 JOIN 无法自动确定字段归属，仅登记表级血缘")

    relations = [
        {"upstream": src, "downstream": target or "", "kind": kind or "select"}
        for src in sources
    ]
    return {
        "ok": bool(relations),
        "target": target,
        "target_type": "table" if target else "",
        "kind": kind or "select",
        "kind_label": kind_label(kind or "select"),
        "sources": sources,
        "relations": relations,
        "columns": columns,
        "ctes": sorted(ctes),
        "notes": notes,
        "sql": raw,
    }


def _insert_columns(sql: str, m_insert: Any = None) -> list[str]:
    """取 INSERT INTO t (a, b) 里的显式列名。"""
    m = re.search(
        rf"(?is)\binsert\s+(?:(?:overwrite|into)\s+)*(?:table\s+)?(?P<ref>{_REF})\s*\(",
        sql,
    )
    if not m:
        return []
    start = m.end()
    depth = 1
    end = start
    while end < len(sql) and depth > 0:
        ch = sql[end]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        end += 1
    inner = sql[start : end - 1] if depth == 0 else ""
    if not inner or re.search(r"(?is)\bselect\b", inner):
        return []
    return [_clean_ref(part) for part in _split_top(inner) if _clean_ref(part)]


def discover_and_record(
    workspace_id: str | None, sql: str, downstream: str | None = None, record_columns: bool = True
) -> dict:
    """解析 SQL 并把识别到的血缘落库（表级 + 字段级）。"""
    result = auto_discover(sql)
    target = normalize_ref(downstream) or result["target"]
    if not target:
        return {**result, "recorded": 0, "recorded_columns": 0, "recorded_relations": []}
    recorded_relations: list[dict] = []
    table_created = 0
    for src in result["sources"]:
        edge = record(
            src,
            target,
            kind=result["kind"] or DEFAULT_KIND,
            detail="由 SQL 自动解析",
            workspace_id=workspace_id,
        )
        if edge.get("created"):
            table_created += 1
        recorded_relations.append({"upstream": src, "downstream": target, "kind": result["kind"]})
    col_created = 0
    if record_columns:
        for col in result["columns"]:
            edge = record(
                col["src_table"],
                target,
                kind=result["kind"] or DEFAULT_KIND,
                src_column=col["src_column"],
                dst_column=col["dst_column"],
                transform=col.get("transform") or "",
                detail="由 SQL 自动解析",
                workspace_id=workspace_id,
            )
            if edge.get("created"):
                col_created += 1
    return {
        **result,
        "target": target,
        "recorded": len(result["sources"]),
        "created": table_created,
        "recorded_columns": col_created,
        "recorded_relations": recorded_relations,
    }


def record_query_assets(workspace_id: str | None, sql: str, assets: Iterable[tuple[str, str]]) -> dict:
    """把「查询引用」型血缘登记下来：源表 -> 指标 / 仪表盘 等资产。

    assets 形如 ``[("dashboard", "零售经营总览"), ("metric", "gmv")]``。
    """
    result = auto_discover(sql)
    created = 0
    for typ, name in assets or []:
        ref = f"{typ}:{name}" if typ and typ != "table" else normalize_ref(name)
        for src in result["sources"]:
            edge = record(
                src,
                ref,
                kind=typ if typ in KIND_LABELS else DEFAULT_KIND,
                detail="由 SQL 引用自动解析",
                workspace_id=workspace_id,
            )
            if edge.get("created"):
                created += 1
    return {"sources": result["sources"], "created": created}
