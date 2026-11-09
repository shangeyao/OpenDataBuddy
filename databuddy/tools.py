"""面向数据平台的领域工具集 —— Agent 通过 function calling 调用它们干活。"""
from __future__ import annotations

import json
import re
from typing import Any

from . import catalog, charts, flows, governance, semantic, store
from .harness import knowledge, memory, skills

Ctx = dict


def _ws(ctx: Ctx) -> str:
    return ctx["workspace_id"]


def _user(ctx: Ctx) -> dict:
    return ctx.get("user") or {}


def _brief_columns(cols: list[dict]) -> list[dict]:
    return [
        {
            "name": c["name"],
            "type": c["data_type"],
            "tags": [t["tag"] for t in c.get("tags", [])],
        }
        for c in cols
    ]


# --------------------------------------------------------------------- 目录


@skills.tool(
    "list_tables",
    "列出当前工作空间所有数据表（catalog.schema.table 全名、行数、描述）。不知道有哪些表时先调这个。",
    {"type": "object", "properties": {}, "required": []},
)
def list_tables(ctx: Ctx) -> dict:
    tables = catalog.all_tables(_ws(ctx))
    return {
        "count": len(tables),
        "tables": [
            {"ref": t["ref"], "rows": t["row_count"], "description": t.get("description") or ""}
            for t in tables
        ],
    }


@skills.tool(
    "describe_table",
    "查看某张表的字段结构、类型、已打标签与样例行。写 SQL 前必须确认字段名。",
    {
        "type": "object",
        "properties": {"table": {"type": "string", "description": "表名，如 demo.retail.fact_orders 或 fact_orders"}},
        "required": ["table"],
    },
)
def describe_table(table: str, ctx: Ctx) -> dict:
    detail = catalog.table_detail(_ws(ctx), table)
    if not detail:
        return {"error": f"表 {table} 不存在，先用 list_tables 查可用表"}
    sample = {}
    try:
        from . import warehouse

        sample = warehouse.preview(detail["catalog_name"], detail["schema_name"], detail["name"], limit=3)
    except Exception:  # noqa: BLE001
        pass
    return {
        "table": detail["ref"],
        "rows": detail["row_count"],
        "description": detail.get("description") or "",
        "columns": _brief_columns(detail["columns"]),
        "sample": {"columns": sample.get("columns", []), "rows": sample.get("rows", [])},
        "artifact": {
            "type": "table_schema",
            "table": detail["ref"],
            "columns": detail["columns"],
        },
    }


@skills.tool(
    "run_query",
    "执行只读 SQL 查询并返回结果。表中引用写成 catalog.schema.table。只能 SELECT。",
    {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "只读 SELECT 语句，不要带分号"},
            "limit": {"type": "integer", "description": "最多返回行数，默认 200"},
        },
        "required": ["sql"],
    },
)
def run_query(sql: str, limit: int = 200, ctx: Ctx = None) -> dict:
    try:
        result = catalog.run_workspace_sql(_ws(ctx), sql, limit=int(limit or 200))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"SQL 执行失败：{exc}", "sql": sql}
    spec = charts.infer(result)
    return {
        "columns": result["columns"],
        "rows": result["rows"][: int(limit or 200)],
        "row_count": result["row_count"],
        "truncated": result["truncated"],
        "elapsed_ms": result["elapsed_ms"],
        "sql": result["sql"],
        "chart": spec,
        "artifact": {
            "type": "query",
            "sql": result["sql"],
            "columns": result["columns"],
            "rows": result["rows"][:50],
            "chart": spec,
        },
    }


# --------------------------------------------------------------------- 语义层


@skills.tool(
    "list_metrics",
    "列出已注册的指标（含口径表达式、单位、可用维度）。问数时优先用指标，避免口径不一致。",
    {"type": "object", "properties": {}, "required": []},
)
def list_metrics(ctx: Ctx) -> dict:
    metrics = semantic.list_metrics(_ws(ctx))
    return {
        "count": len(metrics),
        "metrics": [
            {
                "name": m["name"],
                "display_name": m.get("display_name"),
                "expr": m["expr"],
                "table": m["table_ref"],
                "unit": m.get("unit"),
                "dimensions": m.get("dimensions"),
                "description": m.get("description"),
            }
            for m in metrics
        ],
    }


@skills.tool(
    "query_metric",
    "按语义层口径查指标：选指标 + 维度 + 时间范围，系统自动生成 SQL 并执行。这是问数的首选方式。",
    {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "description": "指标名或中文名，如 gmv / GMV"},
            "dimensions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "维度字段名列表，如 ['channel_name']；按时间看趋势时用 '__date__'",
            },
            "start": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
            "end": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
            "granularity": {"type": "string", "enum": ["day", "week", "month", "quarter"]},
        },
        "required": ["metric"],
    },
)
def query_metric(
    metric: str,
    dimensions: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    granularity: str | None = None,
    ctx: Ctx = None,
) -> dict:
    m = semantic.find_metric(_ws(ctx), metric)
    if not m:
        return {"error": f"指标 {metric} 未注册", "hint": "先用 list_metrics 看可用指标，或直接写 SQL 调 run_query"}
    dims = dimensions or []
    if "__date__" in dims and not granularity:
        granularity = "day"
    built = semantic.build_metric_sql(m, dims, start, end, granularity, order="time" if "__date__" in dims else "auto")
    try:
        result = catalog.run_workspace_sql(_ws(ctx), built["sql"], limit=500)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"口径执行失败：{exc}", "sql": built["sql"]}
    spec = charts.infer(result)
    return {
        "metric": m["name"],
        "display_name": m.get("display_name"),
        "unit": m.get("unit"),
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "sql": result["sql"],
        "chart": spec,
        "artifact": {
            "type": "query",
            "title": m.get("display_name"),
            "sql": result["sql"],
            "columns": result["columns"],
            "rows": result["rows"][:50],
            "chart": spec,
        },
    }


@skills.tool(
    "create_metric",
    "注册/固化一个指标口径，让后续问数复用同一套算法。写操作。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "英文标识，如 net_gmv"},
            "display_name": {"type": "string", "description": "中文名，如 净GMV"},
            "expr": {"type": "string", "description": "SQL 聚合表达式，如 sum(f.amount)"},
            "table_ref": {"type": "string", "description": "主表，如 demo.retail.fact_orders"},
            "unit": {"type": "string"},
            "dimensions": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
        },
        "required": ["name", "expr", "table_ref"],
    },
    risk="write",
    group="semantic",
)
def create_metric(name, expr, table_ref, display_name="", unit="", dimensions=None, description="", ctx: Ctx = None) -> dict:
    ws = _ws(ctx)
    base_joins = []
    # 复用已有指标的标准 join 结构，避免新指标查不到维度
    sample_metric = store.row(
        "SELECT joins FROM metrics WHERE workspace_id = ? AND joins != '[]' LIMIT 1", (ws,)
    )
    if sample_metric:
        try:
            base_joins = json.loads(sample_metric["joins"])
        except json.JSONDecodeError:
            base_joins = []
    try:
        row = semantic.create_metric(
            ws,
            {
                "name": name,
                "display_name": display_name or name,
                "expr": expr,
                "table_ref": table_ref,
                "unit": unit,
                "dimensions": dimensions or [],
                "description": description,
                "joins": base_joins,
                "owner": _user(ctx).get("name") or "",
            },
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"created": True, "metric": row["name"], "display_name": row.get("display_name"), "expr": row["expr"]}


@skills.tool(
    "search_knowledge",
    "检索知识库（业务术语、指标口径、规范文档）。回答「口径是什么」「规范怎么定」这类问题前先查。",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        "required": ["query"],
    },
    group="knowledge",
)
def search_knowledge(query: str, top_k: int = 4, ctx: Ctx = None) -> dict:
    docs = knowledge.search(_ws(ctx), query, top_k)
    return {
        "hits": [
            {"title": d["title"], "snippet": d["snippet"], "tags": d.get("tags"), "score": d["score"]}
            for d in docs
        ]
    }


@skills.tool(
    "write_knowledge",
    "把一段结论/口径/规范沉淀进知识库，供后续会话复用。写操作。",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
            "tags": {"type": "string", "description": "逗号分隔标签"},
        },
        "required": ["title", "content"],
    },
    risk="write",
    group="knowledge",
)
def write_knowledge(title: str, content: str, tags: str = "", ctx: Ctx = None) -> dict:
    store.insert(
        "knowledge_docs",
        {
            "id": store.new_id(),
            "workspace_id": _ws(ctx),
            "title": title,
            "content": content,
            "source_type": "agent",
            "tags": tags,
            "scope": "workspace",
            "created_at": store.now(),
            "updated_at": store.now(),
        },
    )
    return {"saved": True, "title": title}


# --------------------------------------------------------------------- 治理


@skills.tool(
    "scan_sensitive",
    "扫描一张表的敏感字段（手机号、邮箱、身份证、银行卡等），返回分级、置信度与证据。",
    {
        "type": "object",
        "properties": {"table": {"type": "string"}},
        "required": ["table"],
    },
    group="governance",
)
def scan_sensitive(table: str, ctx: Ctx = None) -> dict:
    try:
        result = governance.scan_table(_ws(ctx), table)
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        **result,
        "artifact": {"type": "sensitive_scan", **result},
    }


@skills.tool(
    "apply_tags",
    "把敏感识别结果写入字段标签（分级 L1~L4）。写操作，涉及敏感数据时需人工确认。",
    {
        "type": "object",
        "properties": {
            "table": {"type": "string"},
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要打标的列名；留空表示采用扫描出的全部结果",
            },
        },
        "required": ["table"],
    },
    risk="write",
    group="governance",
)
def apply_tags(table: str, columns: list[str] | None = None, ctx: Ctx = None) -> dict:
    try:
        scan = governance.scan_table(_ws(ctx), table)
    except ValueError as exc:
        return {"error": str(exc)}
    findings = scan["findings"]
    if columns:
        wanted = {c.lower() for c in columns}
        findings = [f for f in findings if f["column"].lower() in wanted]
    applied = governance.apply_tags(_ws(ctx), table, findings)
    return {"applied": applied, "table": scan["table"], "findings": findings}


@skills.tool(
    "suggest_quality_rules",
    "为一张表推荐数据质量规则（空值率、唯一性、值域、时效性），并说明理由。",
    {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]},
    group="governance",
)
def suggest_quality_rules(table: str, ctx: Ctx = None) -> dict:
    try:
        suggestions = governance.suggest_rules(_ws(ctx), table)
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "table": table,
        "suggestions": suggestions,
        "artifact": {"type": "quality_suggestions", "table": table, "items": suggestions},
    }


@skills.tool(
    "create_quality_rule",
    "创建一条数据质量规则。rule_type: null_rate / uniqueness / range / freshness / custom_sql。写操作。",
    {
        "type": "object",
        "properties": {
            "table": {"type": "string"},
            "column": {"type": "string"},
            "rule_type": {"type": "string"},
            "config": {"type": "object", "description": "如 {max_rate:0} / {max_dup:0} / {min:0,max:1000} / {max_days:1}"},
        },
        "required": ["table", "rule_type"],
    },
    risk="write",
    group="governance",
)
def create_quality_rule(table: str, rule_type: str, column: str = "", config: dict | None = None, ctx: Ctx = None) -> dict:
    detail = catalog.resolve(_ws(ctx), table)
    if not detail:
        return {"error": f"表 {table} 不存在"}
    rule = {
        "id": store.new_id(),
        "workspace_id": _ws(ctx),
        "table_ref": detail["ref"],
        "column_name": column or None,
        "rule_type": rule_type,
        "config": json.dumps(config or {}, ensure_ascii=False),
        "enabled": 1,
        "created_at": store.now(),
    }
    store.insert("quality_rules", rule)
    return {"created": True, "rule_id": rule["id"], "table": detail["ref"], "rule_type": rule_type}


@skills.tool(
    "run_quality_rules",
    "立即执行某张表（或全部）的质量规则并返回通过/失败结果。",
    {"type": "object", "properties": {"table": {"type": "string"}}, "required": []},
    group="governance",
)
def run_quality_rules(table: str = "", ctx: Ctx = None) -> dict:
    ws = _ws(ctx)
    if table:
        detail = catalog.resolve(ws, table)
        if not detail:
            return {"error": f"表 {table} 不存在"}
        rules = store.rows(
            "SELECT * FROM quality_rules WHERE workspace_id = ? AND table_ref = ? AND enabled = 1",
            (ws, detail["ref"]),
        )
    else:
        rules = store.rows("SELECT * FROM quality_rules WHERE workspace_id = ? AND enabled = 1", (ws,))
    results = []
    for rule in rules:
        try:
            res = governance.run_rule(ws, rule)
        except Exception as exc:  # noqa: BLE001
            res = {"status": "error", "value": "", "detail": str(exc)}
        results.append(
            {
                "table": rule["table_ref"],
                "column": rule["column_name"],
                "rule_type": rule["rule_type"],
                **res,
            }
        )
    passed = sum(1 for r in results if r["status"] == "pass")
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
        "artifact": {"type": "quality_results", "results": results},
    }


@skills.tool(
    "profile_table",
    "对一张表做资产盘点：行数、空值率、去重数、敏感字段、质量得分。",
    {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]},
    group="governance",
)
def profile_table(table: str, ctx: Ctx = None) -> dict:
    try:
        prof = governance.profile(_ws(ctx), table)
    except ValueError as exc:
        return {"error": str(exc)}
    return {**prof, "artifact": {"type": "profile", **prof}}


# ------------------------------------------------------------------- 工作流


@skills.tool(
    "list_flows",
    "列出工作流及其调度状态。",
    {"type": "object", "properties": {}, "required": []},
    group="engineering",
)
def list_flows(ctx: Ctx) -> dict:
    rows = store.rows("SELECT * FROM flows WHERE workspace_id = ? ORDER BY created_at DESC", (_ws(ctx),))
    return {
        "flows": [
            {
                "id": r["id"],
                "name": r["name"],
                "enabled": bool(r["enabled"]),
                "cron": r["cron"],
                "schedule": flows.describe_cron(r.get("cron") or ""),
                "nodes": len(json.loads(r.get("nodes") or "[]")),
            }
            for r in rows
        ]
    }


@skills.tool(
    "create_flow",
    "创建一个顺序执行的工作流（DAG）。steps 按顺序串成链，可配 cron 定时。写操作。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": ["sql", "quality"]},
                        "sql": {"type": "string"},
                    },
                    "required": ["name", "sql"],
                },
            },
            "cron": {"type": "string", "description": "5 段 cron，如 '0 7 * * *'；留空则仅手动触发"},
            "description": {"type": "string"},
        },
        "required": ["name", "steps"],
    },
    risk="write",
    group="engineering",
)
def create_flow(name: str, steps: list[dict], cron: str = "", description: str = "", ctx: Ctx = None) -> dict:
    ws = _ws(ctx)
    nodes = []
    edges = []
    prev = None
    for i, step in enumerate(steps or []):
        node_id = f"n{i + 1}"
        nodes.append(
            {
                "id": node_id,
                "type": step.get("type") or "sql",
                "name": step.get("name") or f"步骤 {i + 1}",
                "config": {"sql": step.get("sql", "")},
            }
        )
        if prev:
            edges.append([prev, node_id])
        prev = node_id
    if not nodes:
        return {"error": "steps 不能为空"}
    flow_id = store.new_id()
    store.insert(
        "flows",
        {
            "id": flow_id,
            "workspace_id": ws,
            "name": name,
            "description": description,
            "nodes": json.dumps(nodes, ensure_ascii=False),
            "edges": json.dumps(edges, ensure_ascii=False),
            "cron": cron or None,
            "enabled": 1 if cron else 0,
            "created_at": store.now(),
            "updated_at": store.now(),
        },
    )
    return {
        "created": True,
        "flow_id": flow_id,
        "name": name,
        "nodes": len(nodes),
        "schedule": flows.describe_cron(cron) if cron else "手动触发",
    }


@skills.tool(
    "run_flow",
    "立即运行一个工作流并返回每个节点的执行结果。写操作。",
    {"type": "object", "properties": {"flow": {"type": "string", "description": "工作流名称或 id"}}, "required": ["flow"]},
    risk="write",
    group="engineering",
)
def run_flow(flow: str, ctx: Ctx = None) -> dict:
    ws = _ws(ctx)
    row = store.row(
        "SELECT * FROM flows WHERE workspace_id = ? AND (id = ? OR name = ?)", (ws, flow, flow)
    )
    if not row:
        return {"error": f"工作流 {flow} 不存在"}
    try:
        result = flows.run_flow_by_id(row["id"], trigger="agent")
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "flow": row["name"],
        "status": result["status"],
        "nodes": [
            {
                "name": entry.get("name"),
                "status": entry.get("status"),
                "rows": entry.get("rows"),
                "message": entry.get("message", ""),
                "sql": entry.get("sql", ""),
            }
            for entry in result["log"]
        ],
        "artifact": {"type": "flow_run", "flow": row["name"], "log": result["log"]},
    }


@skills.tool(
    "diagnose_run",
    "诊断工作流最近一次失败：定位失败节点、解析错误信息、给出修复建议。",
    {"type": "object", "properties": {"flow": {"type": "string"}}, "required": ["flow"]},
    group="engineering",
)
def diagnose_run(flow: str, ctx: Ctx = None) -> dict:
    ws = _ws(ctx)
    row = store.row("SELECT * FROM flows WHERE workspace_id = ? AND (id = ? OR name = ?)", (ws, flow, flow))
    if not row:
        return {"error": f"工作流 {flow} 不存在"}
    runs = store.rows(
        "SELECT * FROM flow_runs WHERE flow_id = ? ORDER BY started_at DESC LIMIT 5", (row["id"],)
    )
    if not runs:
        return {"flow": row["name"], "diagnosis": "该工作流还没有运行记录，先运行一次再诊断。"}
    latest = runs[0]
    log = json.loads(latest.get("log") or "[]")
    failed = [e for e in log if e.get("status") == "failed"]
    if not failed:
        return {"flow": row["name"], "diagnosis": f"最近一次运行（{latest['started_at']}）状态为 {latest['status']}，没有失败节点。"}
    node = failed[0]
    msg = node.get("message") or ""
    hints = []
    lowered = msg.lower()
    if "does not exist" in lowered or "not found" in lowered:
        hints.append("对象不存在：核对表名/字段名拼写，确认 catalog.schema.table 完整路径")
    if "conversion" in lowered or "cast" in lowered:
        hints.append("类型不匹配：显式 CAST 后再比较，或检查字段类型")
    if "binder" in lowered:
        hints.append("绑定失败：通常是字段名或别名写错，先用 describe_table 确认")
    if "permission" in lowered:
        hints.append("权限不足：确认运行身份对该表有查询权限")
    if not hints:
        hints.append("查看节点 SQL 与错误原文，逐步缩小到具体字段或函数")
    return {
        "flow": row["name"],
        "failed_node": node.get("name"),
        "error": msg,
        "sql": node.get("sql", ""),
        "suggestions": hints,
        "recent_runs": [
            {"status": r["status"], "started_at": r["started_at"], "trigger": r["trigger"]} for r in runs
        ],
    }


# --------------------------------------------------------------------- 看板


@skills.tool(
    "create_dashboard",
    "生成一个仪表盘看板：每个卡片给标题和只读 SQL，系统自动选图表类型。写操作。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "cards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "sql": {"type": "string"},
                        "chart_type": {"type": "string", "enum": ["line", "bar", "pie", "kpi", "table"]},
                    },
                    "required": ["title", "sql"],
                },
            },
        },
        "required": ["name", "cards"],
    },
    risk="write",
    group="analysis",
)
def create_dashboard(name: str, cards: list[dict], description: str = "", ctx: Ctx = None) -> dict:
    ws = _ws(ctx)
    valid_cards = []
    errors = []
    for card in cards or []:
        try:
            catalog.run_workspace_sql(ws, card["sql"], limit=1)
        except Exception as exc:  # noqa: BLE001
            errors.append({"title": card.get("title"), "error": str(exc)})
            continue
        chart = {"type": card.get("chart_type")} if card.get("chart_type") else None
        valid_cards.append({"title": card["title"], "sql": card["sql"], "chart": chart})
    if not valid_cards:
        return {"error": "所有卡片 SQL 都无法执行", "details": errors}
    board_id = store.new_id()
    store.insert(
        "dashboards",
        {
            "id": board_id,
            "workspace_id": ws,
            "name": name,
            "description": description,
            "spec": json.dumps({"cards": valid_cards}, ensure_ascii=False),
            "created_at": store.now(),
            "updated_at": store.now(),
        },
    )
    return {
        "created": True,
        "dashboard_id": board_id,
        "name": name,
        "cards": len(valid_cards),
        "skipped": errors,
        "artifact": {"type": "dashboard", "dashboard_id": board_id, "name": name, "cards": valid_cards},
    }


# --------------------------------------------------------------------- 记忆


@skills.tool(
    "remember",
    "记住用户的长期偏好或口径约定，例如「GMV 一律用未税口径」。写操作。",
    {
        "type": "object",
        "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
        "required": ["key", "value"],
    },
    risk="write",
    group="memory",
)
def remember(key: str, value: str, ctx: Ctx = None) -> dict:
    return memory.remember(_ws(ctx), _user(ctx).get("id") or "anonymous", key, value)


TOOL_GROUPS = {
    "engineering": ["general", "engineering", "semantic", "knowledge", "memory"],
    "governance": ["general", "governance", "semantic", "knowledge", "memory"],
    "analysis": ["general", "analysis", "semantic", "knowledge", "memory"],
}
