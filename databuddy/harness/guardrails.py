"""护栏（Guardrails）：Agent 能干活，但不能闯祸。

三要素判定模型 —— 三个条件同时成立才升级为「必须人工确认」：

    ① 输入不可信   用户消息、知识库文档、工具返回都可能夹带指令
    ② 会改变状态   建表、改口径、删资源、发外部请求
    ③ 触及敏感数据 命中 L3/L4 分级字段或涉及导出

任一条件不成立时，只做审计记录，不打断流程 —— 否则 Agent 会被审批弹窗淹死。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .. import store

READ_TOOLS = {
    "list_tables",
    "describe_table",
    "run_query",
    "list_metrics",
    "search_knowledge",
    "profile_table",
    "scan_sensitive",
    "suggest_quality_rules",
    "list_flows",
    "diagnose_run",
}
WRITE_TOOLS = {
    "create_table_from_sql",
    "create_metric",
    "write_knowledge",
    "apply_tags",
    "create_quality_rule",
    "create_flow",
    "run_flow",
    "create_dashboard",
    "remember",
}
DESTRUCTIVE_TOOLS = {"drop_table", "delete_metric"}

INJECTION_PATTERNS = [
    r"忽略(以上|之前|前面|上述)(的)?(所有)?(指令|规则|设定)",
    r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions|rules|prompts)",
    r"(system|系统)\s*(prompt|提示词)",
    r"你现在是(?!数据)",
    r"开发者模式|developer\s*mode",
    r"revael|reveal\s+your\s+(system|hidden)",
]


def scan_user_input(text: str) -> dict:
    hits = []
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text or "", re.IGNORECASE):
            hits.append(pattern)
    return {"suspicious": bool(hits), "hits": hits}


def sensitive_tables(workspace_id: str) -> set[str]:
    rows = store.rows(
        """
        SELECT DISTINCT ca.name || '.' || s.name || '.' || t.name AS ref
        FROM column_tags ct
        JOIN columns c ON c.id = ct.column_id
        JOIN tables t ON t.id = c.table_id
        JOIN schemas s ON s.id = t.schema_id
        JOIN catalogs ca ON ca.id = s.catalog_id
        WHERE ca.workspace_id = ? AND ct.level IN ('L3', 'L4')
        """,
        (workspace_id,),
    )
    return {r["ref"] for r in rows}


def _mentions(args: dict, needles: set[str]) -> list[str]:
    blob = json.dumps(args, ensure_ascii=False).lower()
    return [n for n in needles if n.lower() in blob]


def assess(tool: str, args: dict, workspace_id: str) -> dict:
    factors: list[str] = ["输入不可信：来自对话，可能夹带指令"]
    if tool in DESTRUCTIVE_TOOLS:
        factors.append("会改变状态：破坏性操作")
    elif tool in WRITE_TOOLS:
        factors.append("会改变状态：写入/修改平台资产")

    touched = _mentions(args, sensitive_tables(workspace_id)) if workspace_id else []
    if tool in ("apply_tags", "scan_sensitive", "profile_table") and args.get("table"):
        touched = touched or [args["table"]]
    if touched:
        factors.append("触及敏感数据：" + "、".join(touched[:3]))

    stateful = tool in WRITE_TOOLS or tool in DESTRUCTIVE_TOOLS
    sensitive = bool(touched)
    require = bool(stateful and (sensitive or tool in DESTRUCTIVE_TOOLS))
    if tool in DESTRUCTIVE_TOOLS:
        require = True

    if tool in READ_TOOLS:
        level = "read"
    elif tool in DESTRUCTIVE_TOOLS:
        level = "destructive"
    else:
        level = "write"

    reason = ""
    if require:
        reason = (
            "该操作会修改平台资产"
            + ("，且涉及敏感字段（L3/L4）" if sensitive else "")
            + "，需要你确认后执行。"
        )
    return {
        "tool": tool,
        "level": level,
        "factors": factors,
        "require_approval": require,
        "reason": reason,
    }


def mask_pii(columns: list[str], rows: list[list[Any]], pii_columns: set[str]) -> list[list[Any]]:
    from ..governance import mask_value

    if not pii_columns:
        return rows
    idx = [i for i, c in enumerate(columns) if c in pii_columns]
    if not idx:
        return rows
    out = []
    for row in rows:
        new = list(row)
        for i in idx:
            if i < len(new) and new[i] is not None:
                new[i] = mask_value(new[i], "手机号" if "phone" in columns[i].lower() else "邮箱")
        out.append(new)
    return out


def policy_hint(workspace_id: str) -> str:
    """写进 system prompt 的合规约束。"""
    tables = sensitive_tables(workspace_id)
    if not tables:
        return "当前工作空间尚未识别敏感字段；查询结果可直接展示。"
    return (
        "当前工作空间已标记敏感字段的表："
        + "、".join(sorted(tables)[:5])
        + "。查询这些表时，涉及手机号/邮箱/身份证/银行卡的列必须脱敏后再展示，"
        "并在回答中提示该字段属于个人敏感信息。"
    )
