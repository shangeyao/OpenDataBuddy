"""语义层扩展：本体建模（Unity Semantics 风格）。

指标解决的是「口径唯一」，但业务方还需要知道：这个指标属于哪个业务对象、
对象之间是什么关系、某个业务术语在系统里到底指哪张表哪一列。
本体层把这三件事固化成 实体（entity）/ 关系（relation）/ 术语（term）。

设计取舍：
- 实体与物理表是多对一绑定，但不强绑 —— 本体是「业务视角」，允许一个实体跨多张表，
  也允许一张表服务多个实体（例如 fact_orders 同时是「订单」和「交易」）。
- 关系是带方向、带基数、带语义动词的三元组，前端可以据此画业务关系图。
- 术语表（glossary）是自然语言到资产的桥梁，Buddy 在解析问句时会先查术语。
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import store

# 实体类型：业务对象 vs 事件 vs 维度
ENTITY_KINDS = {
    "subject": "业务对象",
    "event": "业务事件",
    "dimension": "分析维度",
    "metric_group": "指标集合",
}

# 关系基数
CARDINALITIES = {
    "1:1": "一对一",
    "1:N": "一对多",
    "N:1": "多对一",
    "N:N": "多对多",
}

# 关系语义动词：让图上的边能读成一句话
RELATION_VERBS = {
    "has_many": "包含多个",
    "belongs_to": "隶属于",
    "references": "引用",
    "derives_from": "派生自",
    "measures": "度量",
    "aggregates": "汇总自",
}


def _ensure_tables() -> None:
    """本体三张表随模块初始化，避免每次升级都改 store.SCHEMA。"""
    with store.db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ontology_entities (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                name TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                kind TEXT DEFAULT 'subject',
                description TEXT DEFAULT '',
                table_refs TEXT DEFAULT '[]',
                key_column TEXT DEFAULT '',
                owner TEXT DEFAULT '',
                color TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ontology_relations (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                from_entity TEXT NOT NULL,
                to_entity TEXT NOT NULL,
                verb TEXT DEFAULT 'references',
                cardinality TEXT DEFAULT '1:N',
                description TEXT DEFAULT '',
                join_hint TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS glossary_terms (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                term TEXT NOT NULL,
                definition TEXT DEFAULT '',
                synonyms TEXT DEFAULT '[]',
                asset_type TEXT DEFAULT 'table',
                asset_ref TEXT DEFAULT '',
                entity_id TEXT DEFAULT '',
                owner TEXT DEFAULT '',
                status TEXT DEFAULT 'published',
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ont_ent_ws ON ontology_entities(workspace_id);
            CREATE INDEX IF NOT EXISTS idx_ont_rel_ws ON ontology_relations(workspace_id);
            CREATE INDEX IF NOT EXISTS idx_glossary_ws ON glossary_terms(workspace_id);
            """
        )


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _slug(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fa5]", "_", (text or "").strip()).lower()


# ------------------------------------------------------------------ 实体


def list_entities(workspace_id: str) -> list[dict]:
    _ensure_tables()
    rows = store.rows(
        "SELECT * FROM ontology_entities WHERE workspace_id = ? ORDER BY name", (workspace_id,)
    )
    for r in rows:
        r["table_refs"] = _loads(r.get("table_refs"), [])
        r["kind_label"] = ENTITY_KINDS.get(r.get("kind") or "", r.get("kind") or "")
        r["relation_count"] = store.scalar(
            "SELECT COUNT(*) FROM ontology_relations WHERE workspace_id = ? "
            "AND (from_entity = ? OR to_entity = ?)",
            (workspace_id, r["id"], r["id"]),
            default=0,
        )
    return rows


def get_entity(workspace_id: str, entity_id: str) -> dict | None:
    for e in list_entities(workspace_id):
        if e["id"] == entity_id or e["name"] == entity_id:
            return e
    return None


def create_entity(workspace_id: str, payload: dict) -> dict:
    _ensure_tables()
    name = _slug(payload.get("name"))
    if not name:
        raise ValueError("实体名不能为空")
    if store.row(
        "SELECT id FROM ontology_entities WHERE workspace_id = ? AND name = ?",
        (workspace_id, name),
    ):
        raise ValueError(f"实体 {name} 已存在")
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "name": name,
        "display_name": payload.get("display_name") or payload.get("name") or name,
        "kind": payload.get("kind") or "subject",
        "description": payload.get("description") or "",
        "table_refs": json.dumps(payload.get("table_refs") or [], ensure_ascii=False),
        "key_column": payload.get("key_column") or "",
        "owner": payload.get("owner") or "",
        "color": payload.get("color") or "",
        "created_at": store.now(),
    }
    store.insert("ontology_entities", row)
    return row


def update_entity(entity_id: str, payload: dict) -> None:
    _ensure_tables()
    patch: dict[str, Any] = {}
    for key in ("display_name", "kind", "description", "key_column", "owner", "color"):
        if key in payload:
            patch[key] = payload[key]
    if "table_refs" in payload:
        patch["table_refs"] = json.dumps(payload["table_refs"] or [], ensure_ascii=False)
    store.update("ontology_entities", entity_id, patch)


def delete_entity(entity_id: str) -> None:
    _ensure_tables()
    store.delete("ontology_entities", entity_id)
    store.execute(
        "DELETE FROM ontology_relations WHERE from_entity = ? OR to_entity = ?",
        (entity_id, entity_id),
    )


# ------------------------------------------------------------------ 关系


def list_relations(workspace_id: str) -> list[dict]:
    _ensure_tables()
    entities = {e["id"]: e for e in list_entities(workspace_id)}
    rows = store.rows(
        "SELECT * FROM ontology_relations WHERE workspace_id = ? ORDER BY created_at",
        (workspace_id,),
    )
    for r in rows:
        r["from_name"] = (entities.get(r["from_entity"]) or {}).get("display_name") or r["from_entity"]
        r["to_name"] = (entities.get(r["to_entity"]) or {}).get("display_name") or r["to_entity"]
        r["verb_label"] = RELATION_VERBS.get(r.get("verb") or "", r.get("verb") or "")
        r["cardinality_label"] = CARDINALITIES.get(r.get("cardinality") or "", r.get("cardinality") or "")
    return rows


def create_relation(workspace_id: str, payload: dict) -> dict:
    _ensure_tables()
    src = (payload.get("from_entity") or "").strip()
    dst = (payload.get("to_entity") or "").strip()
    if not src or not dst:
        raise ValueError("关系的起点与终点实体必填")
    if src == dst:
        raise ValueError("关系的起点与终点不能是同一个实体")
    if not get_entity(workspace_id, src):
        raise ValueError(f"实体 {src} 不存在")
    if not get_entity(workspace_id, dst):
        raise ValueError(f"实体 {dst} 不存在")
    # 同一对实体 + 同一动词视为重复
    if store.row(
        "SELECT id FROM ontology_relations WHERE workspace_id = ? "
        "AND from_entity = ? AND to_entity = ? AND verb = ?",
        (workspace_id, src, dst, payload.get("verb") or "references"),
    ):
        raise ValueError("该关系已存在")
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "from_entity": src,
        "to_entity": dst,
        "verb": payload.get("verb") or "references",
        "cardinality": payload.get("cardinality") or "1:N",
        "description": payload.get("description") or "",
        "join_hint": payload.get("join_hint") or "",
        "created_at": store.now(),
    }
    store.insert("ontology_relations", row)
    return row


def delete_relation(relation_id: str) -> None:
    _ensure_tables()
    store.delete("ontology_relations", relation_id)


def relation_graph(workspace_id: str) -> dict:
    """返回可直接渲染的本体图：节点 + 边。"""
    entities = list_entities(workspace_id)
    relations = list_relations(workspace_id)
    nodes = [
        {
            "id": e["id"],
            "label": e.get("display_name") or e["name"],
            "kind": e.get("kind") or "subject",
            "kind_label": e.get("kind_label") or "",
            "tables": e.get("table_refs") or [],
        }
        for e in entities
    ]
    edges = [
        {
            "id": r["id"],
            "from": r["from_entity"],
            "to": r["to_entity"],
            "label": r.get("verb_label") or r.get("verb") or "",
            "cardinality": r.get("cardinality") or "",
        }
        for r in relations
    ]
    return {"nodes": nodes, "edges": edges}


# ------------------------------------------------------------------ 术语表


def list_terms(workspace_id: str, keyword: str = "") -> list[dict]:
    _ensure_tables()
    sql = "SELECT * FROM glossary_terms WHERE workspace_id = ?"
    params: list[Any] = [workspace_id]
    if keyword:
        sql += " AND (term LIKE ? OR definition LIKE ?)"
        params += [f"%{keyword}%", f"%{keyword}%"]
    sql += " ORDER BY term"
    rows = store.rows(sql, params)
    for r in rows:
        r["synonyms"] = _loads(r.get("synonyms"), [])
    return rows


def create_term(workspace_id: str, payload: dict) -> dict:
    _ensure_tables()
    term = (payload.get("term") or "").strip()
    if not term:
        raise ValueError("术语不能为空")
    if store.row(
        "SELECT id FROM glossary_terms WHERE workspace_id = ? AND term = ?", (workspace_id, term)
    ):
        raise ValueError(f"术语 {term} 已存在")
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "term": term,
        "definition": payload.get("definition") or "",
        "synonyms": json.dumps(payload.get("synonyms") or [], ensure_ascii=False),
        "asset_type": payload.get("asset_type") or "table",
        "asset_ref": payload.get("asset_ref") or "",
        "entity_id": payload.get("entity_id") or "",
        "owner": payload.get("owner") or "",
        "status": payload.get("status") or "published",
        "created_at": store.now(),
    }
    store.insert("glossary_terms", row)
    return row


def update_term(term_id: str, payload: dict) -> None:
    _ensure_tables()
    patch: dict[str, Any] = {}
    for key in ("term", "definition", "asset_type", "asset_ref", "entity_id", "owner", "status"):
        if key in payload:
            patch[key] = payload[key]
    if "synonyms" in payload:
        patch["synonyms"] = json.dumps(payload["synonyms"] or [], ensure_ascii=False)
    store.update("glossary_terms", term_id, patch)


def delete_term(term_id: str) -> None:
    _ensure_tables()
    store.delete("glossary_terms", term_id)


def resolve_term(workspace_id: str, text: str) -> dict | None:
    """在问句里找术语（含同义词），返回命中的术语及其绑定资产。

    Buddy 解析问句时先走这一步：业务方说「销售额」到底指哪个指标，
    由术语表决定，而不是让模型猜。
    """
    if not text:
        return None
    low = text.lower()
    hits: list[tuple[int, dict]] = []
    for term in list_terms(workspace_id):
        candidates = [term["term"]] + list(term.get("synonyms") or [])
        for cand in candidates:
            if cand and cand.lower() in low:
                hits.append((len(cand), term))
                break
    if not hits:
        return None
    # 长词优先，避免「销售额」被「销售」抢走
    hits.sort(key=lambda x: -x[0])
    return hits[0][1]


def term_prompt_block(workspace_id: str) -> str:
    """给模型的术语上下文：把业务黑话翻译成资产引用。"""
    lines: list[str] = []
    for t in list_terms(workspace_id):
        syn = "、".join(t.get("synonyms") or []) or "-"
        lines.append(
            f'- {t["term"]}（同义: {syn}）: {t.get("definition") or ""} '
            f'→ {t.get("asset_type")}:{t.get("asset_ref") or "-"}'
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ 总览


def overview(workspace_id: str) -> dict:
    _ensure_tables()
    entities = list_entities(workspace_id)
    relations = list_relations(workspace_id)
    terms = list_terms(workspace_id)
    covered = {ref for e in entities for ref in (e.get("table_refs") or [])}
    total_tables = store.scalar(
        "SELECT COUNT(*) FROM tables t JOIN schemas s ON s.id = t.schema_id "
        "JOIN catalogs c ON c.id = s.catalog_id WHERE c.workspace_id = ?",
        (workspace_id,),
        default=0,
    )
    return {
        "entities": len(entities),
        "relations": len(relations),
        "terms": len(terms),
        "covered_tables": len(covered),
        "total_tables": total_tables,
        "coverage": round(len(covered) / total_tables, 3) if total_tables else 0.0,
        "kinds": ENTITY_KINDS,
        "verbs": RELATION_VERBS,
        "cardinalities": CARDINALITIES,
    }
