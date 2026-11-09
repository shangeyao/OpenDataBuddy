"""轨迹与评测：每次工具调用都留痕，用来算成功率、定位慢工具、复盘失败。"""
from __future__ import annotations

import json
from collections import Counter

from .. import store


def record(
    conversation_id: str,
    workspace_id: str,
    step: int,
    tool: str,
    args: dict,
    result: Any,
    ok: bool = True,
    duration_ms: int = 0,
) -> None:
    text = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
    store.insert(
        "traces",
        {
            "id": store.new_id(),
            "conversation_id": conversation_id,
            "workspace_id": workspace_id,
            "step": step,
            "tool": tool,
            "args": json.dumps(args, ensure_ascii=False, default=str)[:2000],
            "result": text[:4000],
            "ok": 1 if ok else 0,
            "duration_ms": duration_ms,
            "created_at": store.now(),
        },
    )


def of_conversation(conversation_id: str) -> list[dict]:
    return store.rows(
        "SELECT * FROM traces WHERE conversation_id = ? ORDER BY step, created_at",
        (conversation_id,),
    )


def stats(workspace_id: str, limit: int = 500) -> dict:
    rows = store.rows(
        "SELECT tool, ok, duration_ms FROM traces WHERE workspace_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (workspace_id, limit),
    )
    if not rows:
        return {"calls": 0, "success_rate": None, "avg_ms": None, "tools": []}
    total = len(rows)
    ok = sum(1 for r in rows if r["ok"])
    avg = sum(r["duration_ms"] or 0 for r in rows) / total
    per_tool = Counter(r["tool"] for r in rows)
    tool_stats = []
    for tool, count in per_tool.most_common(12):
        subset = [r for r in rows if r["tool"] == tool]
        tool_stats.append(
            {
                "tool": tool,
                "calls": count,
                "success_rate": round(sum(1 for r in subset if r["ok"]) / count, 3),
                "avg_ms": int(sum(r["duration_ms"] or 0 for r in subset) / count),
            }
        )
    return {
        "calls": total,
        "success_rate": round(ok / total, 3),
        "avg_ms": int(avg),
        "tools": tool_stats,
    }
