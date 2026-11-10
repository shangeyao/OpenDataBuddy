"""OPS 四层视图 + 基于规则推理的 AI 诊断。

四层
----
* **platform**  平台层：全局任务吞吐、成功率、资源总量
* **workspace** 工作空间层：各空间的任务与资产
* **resource**  资源层：工作流（可调度资源）+ 计算资源（算力）
* **task**      任务层：flow_runs 实例

每层统一返回 `{level, name, health, metrics, children}`，前端可以无差别递归渲染。

健康度
------
四因子加权（0-100）：

    score = 100 × (0.50·成功率 + 0.25·(1-失败率) + 0.15·(1-排队率) + 0.10·运行负载因子)

* 成功率是主信号，权重最高；
* 失败率单独惩罚 —— 同样是 80% 成功率，集中失败比分散失败更危险，由下游诊断继续细分；
* 排队率高说明调度拥塞，会拖慢 SLA；
* 运行负载因子对「长期 running 堆积」做温和惩罚（不是失败，但值得关注）。

诊断
----
`diagnose()` 是纯规则推理，**不调用任何外部 LLM**：把失败率、连续失败次数、
超时、资源不足、质量规则失败、未确认异常等信号逐条评估，命中即产出
「根因 + 建议」，并按严重度排序。这样离线环境也能得到可复现、可解释的结论。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from . import catalog, store

#: 四层，顺序即从粗到细
LEVELS: list[dict] = [
    {"key": "platform", "label": "平台层", "desc": "全局任务吞吐与资源总量"},
    {"key": "workspace", "label": "工作空间层", "desc": "按空间拆分任务与资产"},
    {"key": "resource", "label": "资源层", "desc": "工作流与计算资源"},
    {"key": "task", "label": "任务层", "desc": "运行实例明细"},
]

#: 健康度权重（运行中 / 失败 / 排队 / 成功率）
HEALTH_WEIGHTS = {"success_rate": 0.50, "failure": 0.25, "queue": 0.15, "running": 0.10}

#: 超过这个时长仍是 running 视为疑似超时（秒）
TIMEOUT_SECONDS = 1800

#: 连续失败达到这个次数视为「持续性故障」
CONSECUTIVE_FAIL_LIMIT = 3

#: 失败率告警阈值
FAILURE_RATE_THRESHOLD = 0.20


# ---------------------------------------------------------------- 工具函数


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _parse(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _duration_seconds(run: dict) -> float | None:
    started = _parse(run.get("started_at"))
    if not started:
        return None
    finished = _parse(run.get("finished_at")) or _now()
    return max((finished - started).total_seconds(), 0.0)


def _as_json(value: Any, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def grade_of(score: float) -> dict:
    """分数 → 等级 / 颜色 / 文案。"""
    if score >= 90:
        return {"grade": "healthy", "tone": "green", "label": "健康"}
    if score >= 70:
        return {"grade": "warning", "tone": "orange", "label": "关注"}
    if score >= 50:
        return {"grade": "degraded", "tone": "orange", "label": "降级"}
    return {"grade": "critical", "tone": "red", "label": "异常"}


def compute_health(
    total: int, success: int = 0, failed: int = 0, running: int = 0, queued: int = 0
) -> dict:
    """四因子加权健康度。空集合视为健康（无任务 ≠ 不健康）。"""
    total = max(int(total or 0), 0)
    if total == 0:
        return {
            "score": 100.0,
            "grade": "healthy",
            "tone": "green",
            "label": "健康",
            "factors": {"success_rate": 1.0, "failure_rate": 0.0, "queue_rate": 0.0, "load_factor": 1.0},
            "basis": "暂无任务",
        }
    success_rate = success / total
    failure_rate = failed / total
    queue_rate = queued / total
    # 运行负载：running 占比越高说明堆积越多，温和扣分（最多扣 50%）
    load_factor = 1.0 - min(running / total, 1.0) * 0.5

    score = 100.0 * (
        HEALTH_WEIGHTS["success_rate"] * success_rate
        + HEALTH_WEIGHTS["failure"] * (1.0 - failure_rate)
        + HEALTH_WEIGHTS["queue"] * (1.0 - queue_rate)
        + HEALTH_WEIGHTS["running"] * load_factor
    )
    score = round(max(0.0, min(100.0, score)), 1)
    return {
        "score": score,
        **grade_of(score),
        "factors": {
            "success_rate": round(success_rate, 4),
            "failure_rate": round(failure_rate, 4),
            "queue_rate": round(queue_rate, 4),
            "load_factor": round(load_factor, 4),
        },
        "basis": f"{total} 次任务：成功 {success} / 失败 {failed} / 运行 {running} / 排队 {queued}",
    }


def _node(
    level: str,
    name: str,
    health: dict,
    metrics: dict,
    children: list[dict] | None = None,
    *,
    node_id: str = "",
    kind: str = "",
    extra: dict | None = None,
) -> dict:
    return {
        "id": node_id or f"{level}:{name}",
        "level": level,
        "name": name,
        "kind": kind or level,
        "health": health,
        "health_score": health.get("score", 100.0),
        "metrics": metrics,
        "children": children or [],
        **(extra or {}),
    }


# ---------------------------------------------------------------- 数据采集


def _runs_for_flows(flow_ids: list[str], limit: int = 200) -> list[dict]:
    if not flow_ids:
        return []
    marks = ", ".join("?" for _ in flow_ids)
    return store.rows(
        f"SELECT * FROM flow_runs WHERE flow_id IN ({marks}) "
        f"ORDER BY coalesce(started_at, '') DESC LIMIT ?",
        (*flow_ids, limit),
    )


def _tally(runs: list[dict]) -> dict:
    counts = {"total": len(runs), "success": 0, "failed": 0, "running": 0, "queued": 0}
    for r in runs:
        status = (r.get("status") or "").lower()
        if status in ("success", "succeeded", "ok"):
            counts["success"] += 1
        elif status in ("failed", "fail", "error"):
            counts["failed"] += 1
        elif status in ("running", "started"):
            counts["running"] += 1
        elif status in ("queued", "pending", "waiting"):
            counts["queued"] += 1
    counts["success_rate"] = round(counts["success"] / counts["total"], 4) if counts["total"] else 1.0
    counts["failure_rate"] = round(counts["failed"] / counts["total"], 4) if counts["total"] else 0.0
    return counts


def _task_node(run: dict, flow_name: str) -> dict:
    status = (run.get("status") or "running").lower()
    duration = _duration_seconds(run)
    if status in ("success", "succeeded", "ok"):
        health = {"score": 100.0, **grade_of(100.0)}
    elif status in ("failed", "fail", "error"):
        health = {"score": 0.0, **grade_of(0.0)}
    else:
        # 运行中：按是否疑似超时给分
        timed_out = duration is not None and duration > TIMEOUT_SECONDS
        score = 55.0 if timed_out else 85.0
        health = {"score": score, **grade_of(score)}
    log = _as_json(run.get("log"), [])
    failed_nodes = [e for e in log if isinstance(e, dict) and e.get("status") == "failed"]
    return _node(
        "task",
        f"{flow_name} · {run.get('id', '')[-8:]}",
        health,
        {
            "status": status,
            "trigger": run.get("trigger") or "manual",
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "duration_seconds": round(duration, 1) if duration is not None else None,
            "log_entries": len(log),
            "failed_nodes": len(failed_nodes),
            "timeout_suspect": bool(duration is not None and duration > TIMEOUT_SECONDS
                                    and status not in ("success", "failed", "succeeded")),
        },
        [],
        node_id=f"task:{run.get('id')}",
        kind="flow_run",
        extra={"run_id": run.get("id"), "flow_name": flow_name},
    )


def _flow_resource_node(flow: dict, runs: list[dict], max_tasks: int = 8) -> dict:
    counts = _tally(runs)
    health = compute_health(
        counts["total"], counts["success"], counts["failed"], counts["running"], counts["queued"]
    )
    tasks = [_task_node(r, flow["name"]) for r in runs[:max_tasks]]
    nodes = _as_json(flow.get("nodes"), [])
    return _node(
        "resource",
        flow["name"],
        health,
        {
            **counts,
            "enabled": bool(flow.get("enabled")),
            "cron": flow.get("cron") or "",
            "node_count": len(nodes),
            "trigger": "定时" if flow.get("cron") else "手动",
            "last_run_at": runs[0].get("started_at") if runs else None,
            "last_status": runs[0].get("status") if runs else None,
        },
        tasks,
        node_id=f"flow:{flow['id']}",
        kind="flow",
        extra={"flow_id": flow["id"]},
    )


def _compute_resource_node(resource: dict) -> dict:
    status = (resource.get("status") or "unknown").lower()
    if status == "running":
        score = 95.0
    elif status in ("creating", "stopping"):
        score = 75.0
    elif status == "stopped":
        score = 60.0
    else:
        score = 70.0
    health = {"score": score, **grade_of(score)}
    return _node(
        "resource",
        resource.get("name") or resource.get("id"),
        health,
        {
            "type": resource.get("type") or "",
            "engine": resource.get("engine") or "",
            "compute_type": resource.get("compute_type") or "CPU",
            "cu": resource.get("cu") or 0,
            "status": status,
            "image": resource.get("image") or "",
            "network": resource.get("network") or "",
        },
        [],
        node_id=f"compute:{resource.get('id')}",
        kind="compute",
        extra={"resource_id": resource.get("id")},
    )


def _workspace_metrics(workspace_id: str) -> dict:
    tables = catalog.all_tables(workspace_id)
    flows = store.rows("SELECT * FROM flows WHERE workspace_id = ?", (workspace_id,))
    computes = store.rows("SELECT * FROM compute_resources WHERE workspace_id = ?", (workspace_id,))
    runs = _runs_for_flows([f["id"] for f in flows])
    counts = _tally(runs)
    rules = store.rows(
        "SELECT last_status FROM quality_rules WHERE workspace_id = ? AND last_status IS NOT NULL",
        (workspace_id,),
    )
    passed = sum(1 for r in rules if r["last_status"] == "pass")
    return {
        "tables": len(tables),
        "rows": sum(t.get("row_count") or 0 for t in tables),
        "flows": len(flows),
        "enabled_flows": sum(1 for f in flows if f.get("enabled")),
        "compute_resources": len(computes),
        "running_compute": sum(1 for c in computes if (c.get("status") or "") == "running"),
        "total_cu": sum(int(c.get("cu") or 0) for c in computes),
        "quality_rules": len(rules),
        "quality_pass_rate": round(passed / len(rules), 4) if rules else None,
        **counts,
    }


def _workspace_node(workspace: dict, depth: str = "all", max_tasks: int = 8) -> dict:
    workspace_id = workspace["id"]
    metrics = _workspace_metrics(workspace_id)
    health = compute_health(
        metrics["total"], metrics["success"], metrics["failed"], metrics["running"], metrics["queued"]
    )
    children: list[dict] = []
    if depth in ("all", "resource", "task"):
        flows = store.rows(
            "SELECT * FROM flows WHERE workspace_id = ? ORDER BY created_at", (workspace_id,)
        )
        runs = _runs_for_flows([f["id"] for f in flows])
        by_flow: dict[str, list[dict]] = {}
        for r in runs:
            by_flow.setdefault(r.get("flow_id") or "", []).append(r)
        for flow in flows:
            flow_runs = by_flow.get(flow["id"], [])
            node = _flow_resource_node(flow, flow_runs, max_tasks)
            if depth == "resource":
                node["children"] = []
            children.append(node)

        computes = store.rows(
            "SELECT * FROM compute_resources WHERE workspace_id = ? ORDER BY created_at",
            (workspace_id,),
        )
        children.extend(_compute_resource_node(c) for c in computes)

    return _node(
        "workspace",
        workspace.get("name") or workspace_id,
        health,
        metrics,
        children,
        node_id=f"workspace:{workspace_id}",
        extra={"workspace_id": workspace_id, "description": workspace.get("description") or ""},
    )


def platform_node(workspace_id: str | None = None, depth: str = "all", max_tasks: int = 8) -> dict:
    """平台层根节点：`depth` 控制展开到哪一层（platform/workspace/resource/task）。"""
    if workspace_id:
        workspaces = store.rows("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        if not workspaces:
            workspaces = [{"id": workspace_id, "name": "当前工作空间", "description": ""}]
    else:
        workspaces = store.rows("SELECT * FROM workspaces ORDER BY created_at")

    ws_nodes = [_workspace_node(w, depth, max_tasks) for w in workspaces]
    if depth == "platform":
        for node in ws_nodes:
            node["children"] = []

    totals = {"total": 0, "success": 0, "failed": 0, "running": 0, "queued": 0}
    for node in ws_nodes:
        for key in totals:
            totals[key] += int(node["metrics"].get(key) or 0)
    health = compute_health(
        totals["total"], totals["success"], totals["failed"], totals["running"], totals["queued"]
    )
    totals["success_rate"] = round(totals["success"] / totals["total"], 4) if totals["total"] else 1.0
    totals["failure_rate"] = round(totals["failed"] / totals["total"], 4) if totals["total"] else 0.0
    totals["workspaces"] = len(ws_nodes)
    totals["resources"] = sum(len(n["children"]) for n in ws_nodes)
    totals["tasks"] = totals["total"]

    return _node(
        "platform",
        "OpenDataBuddy 平台",
        health,
        totals,
        ws_nodes,
        node_id="platform",
        extra={"depth": depth},
    )


def levels_tree(workspace_id: str | None = None, depth: str = "all") -> dict:
    """四层视图入口。返回 {levels, tree, summary}。"""
    depth = depth if depth in {l["key"] for l in LEVELS} else "all"
    tree = platform_node(workspace_id, depth if depth != "all" else "all")
    summary = {
        "levels": LEVELS,
        "depth": depth,
        "health": tree["health"],
        "counts": {
            "workspaces": len(tree["children"]),
            "resources": sum(len(c["children"]) for c in tree["children"]),
            "tasks": tree["metrics"].get("total", 0),
        },
        "weights": HEALTH_WEIGHTS,
    }
    return {"levels": LEVELS, "tree": tree, "summary": summary}


def flatten(tree: dict) -> list[dict]:
    """把树拍平成列表（前端做表格视图或测试断言都方便）。"""
    out: list[dict] = []

    def walk(node: dict, path: str) -> None:
        current = f"{path}/{node['name']}" if path else node["name"]
        out.append({**{k: v for k, v in node.items() if k != "children"}, "path": current,
                    "child_count": len(node.get("children") or [])})
        for child in node.get("children") or []:
            walk(child, current)

    walk(tree, "")
    return out


# ------------------------------------------------------------------ AI 诊断


def _signal(
    severity: str, title: str, detail: str, suggestion: str, evidence: dict | None = None
) -> dict:
    return {
        "severity": severity,
        "title": title,
        "detail": detail,
        "suggestion": suggestion,
        "evidence": evidence or {},
    }


def _consecutive_failures(runs: list[dict]) -> tuple[int, dict | None]:
    """按时间倒序数连续失败次数。"""
    streak = 0
    first = None
    for run in runs:
        status = (run.get("status") or "").lower()
        if status in ("failed", "fail", "error"):
            streak += 1
            first = first or run
        elif status in ("success", "succeeded", "ok"):
            break
    return streak, first


def _timeouts(runs: list[dict]) -> list[dict]:
    out = []
    for run in runs:
        status = (run.get("status") or "").lower()
        if status in ("running", "started", "queued", "pending"):
            duration = _duration_seconds(run)
            if duration is not None and duration > TIMEOUT_SECONDS:
                out.append({**run, "duration_seconds": round(duration, 1)})
    return out


def _diagnose_scope(workspace_id: str, scope: str) -> tuple[str, list[dict], list[dict]]:
    """返回 (scope_label, runs, flows)。scope 支持 platform/workspace/flow/resource/task。"""
    kind, _, target = (scope or "platform").partition(":")
    kind = kind or "platform"

    if kind in ("platform", "workspace", ""):
        flows = store.rows("SELECT * FROM flows WHERE workspace_id = ?", (workspace_id,))
        runs = _runs_for_flows([f["id"] for f in flows], limit=300)
        label = "平台" if kind in ("platform", "") else "当前工作空间"
        return label, runs, flows
    if kind == "flow":
        flows = store.rows(
            "SELECT * FROM flows WHERE workspace_id = ? AND id = ?", (workspace_id, target)
        )
        runs = _runs_for_flows([target], limit=100)
        return (flows[0]["name"] if flows else target), runs, flows
    if kind == "resource":
        flows = store.rows(
            "SELECT * FROM flows WHERE workspace_id = ? AND id = ?", (workspace_id, target)
        )
        runs = _runs_for_flows([target], limit=100)
        return (flows[0]["name"] if flows else target), runs, flows
    if kind == "task":
        run = store.row("SELECT * FROM flow_runs WHERE id = ?", (target,))
        runs = [run] if run else []
        flows = store.rows("SELECT * FROM flows WHERE workspace_id = ?", (workspace_id,))
        return f"任务 {target}", runs, flows
    # 未知 scope：退回平台级
    flows = store.rows("SELECT * FROM flows WHERE workspace_id = ?", (workspace_id,))
    runs = _runs_for_flows([f["id"] for f in flows], limit=300)
    return "平台", runs, flows


def diagnose(workspace_id: str, scope: str = "platform") -> dict:
    """基于规则推理的 AI 诊断。不调用外部 LLM，结论可解释、可复现。"""
    label, runs, flows = _diagnose_scope(workspace_id, scope)
    counts = _tally(runs)
    signals: list[dict] = []

    # ① 失败率
    if counts["total"] and counts["failure_rate"] >= FAILURE_RATE_THRESHOLD:
        signals.append(
            _signal(
                "critical",
                "失败率显著偏高",
                f"最近 {counts['total']} 次运行中失败 {counts['failed']} 次"
                f"（{counts['failure_rate']:.1%}），超过 {FAILURE_RATE_THRESHOLD:.0%} 的告警线。",
                "先按「最近一次失败」定位具体节点，核对表名/字段名与上游数据是否就绪，"
                "确认无误后重跑；若同一节点反复失败，优先修数据而非改调度。",
                {"failure_rate": counts["failure_rate"], "failed": counts["failed"], "total": counts["total"]},
            )
        )

    # ② 连续失败
    streak, first = _consecutive_failures(runs)
    if streak >= CONSECUTIVE_FAIL_LIMIT:
        failed_node = ""
        log = _as_json((first or {}).get("log"), [])
        for entry in log:
            if isinstance(entry, dict) and entry.get("status") == "failed":
                failed_node = entry.get("name") or entry.get("message") or ""
                break
        signals.append(
            _signal(
                "critical",
                f"连续失败 {streak} 次",
                f"最近 {streak} 次运行连续失败"
                + (f"，首个失败节点：{failed_node}。" if failed_node else "。")
                + "这不是偶发抖动，通常是配置或上游数据问题未修复就反复重试。",
                "暂停该调度，修复失败节点后手动跑一次验证，成功后再恢复定时。",
                {"streak": streak, "first_failed_run": (first or {}).get("id"), "failed_node": failed_node},
            )
        )

    # ③ 超时 / 长时间运行
    timeouts = _timeouts(runs)
    if timeouts:
        worst = max(timeouts, key=lambda r: r["duration_seconds"])
        signals.append(
            _signal(
                "warning",
                f"{len(timeouts)} 个任务疑似超时",
                f"最长已运行 {worst['duration_seconds']:.0f}s（阈值 {TIMEOUT_SECONDS}s），"
                "可能是锁等待、全表扫描或下游依赖阻塞。",
                "检查是否缺少分区裁剪与索引；对超大表改用增量同步；必要时终止并调大资源。",
                {"count": len(timeouts), "worst_seconds": worst["duration_seconds"],
                 "run_ids": [r.get("id") for r in timeouts[:5]]},
            )
        )

    # ④ 排队积压
    if counts["queued"] >= 3:
        signals.append(
            _signal(
                "warning",
                f"排队任务积压 {counts['queued']} 个",
                "排队数持续升高说明调度吞吐不足或上游资源被占满。",
                "提高计算资源 CU 或错峰调度；检查是否有长任务占用了全部并发。",
                {"queued": counts["queued"]},
            )
        )

    # ⑤ 资源不足
    computes = store.rows("SELECT * FROM compute_resources WHERE workspace_id = ?", (workspace_id,))
    if computes:
        stopped = [c for c in computes if (c.get("status") or "") == "stopped"]
        if stopped and counts["failed"]:
            signals.append(
                _signal(
                    "warning",
                    f"{len(stopped)} 个计算资源处于停止状态",
                    "停止的计算资源无法承接任务，若工作流绑定到该资源会直接失败。",
                    "确认是否为预期的停机；非预期则重新启动该计算资源。",
                    {"stopped": [c.get("name") for c in stopped[:5]]},
                )
            )
        if not stopped and sum(int(c.get("cu") or 0) for c in computes) == 0:
            signals.append(
                _signal(
                    "info",
                    "计算资源 CU 配额为 0",
                    "所有计算资源都没有分配 CU，任务只能串行排队。",
                    "给至少一个计算资源分配 CU，或降低并发任务的调度频率。",
                    {"resources": len(computes)},
                )
            )

    # ⑥ 质量规则失败
    failed_rules = store.rows(
        "SELECT table_ref, column_name, rule_type, last_status FROM quality_rules "
        "WHERE workspace_id = ? AND last_status = 'fail'",
        (workspace_id,),
    )
    if failed_rules:
        tables = sorted({r["table_ref"] for r in failed_rules})
        signals.append(
            _signal(
                "warning",
                f"{len(failed_rules)} 条质量规则未通过",
                "涉及表：" + "、".join(tables[:5]) + "。数据质量问题会传导到下游指标与看板。",
                "先看规则失败的具体值（空值率/越界/时效），在数据接入侧修复，"
                "再重跑规则确认通过；必要时加前置阻断避免脏数据继续扩散。",
                {"rules": len(failed_rules), "tables": tables[:5]},
            )
        )

    # ⑦ 未确认异常
    try:
        pending_anomalies = store.rows(
            "SELECT table_ref, kind, severity, detail FROM anomaly_findings "
            "WHERE workspace_id = ? AND acknowledged = 0 ORDER BY detected_at DESC LIMIT 20",
            (workspace_id,),
        )
    except Exception:  # noqa: BLE001 - 表可能尚未建立
        pending_anomalies = []
    if pending_anomalies:
        critical = [a for a in pending_anomalies if a.get("severity") == "critical"]
        signals.append(
            _signal(
                "critical" if critical else "warning",
                f"{len(pending_anomalies)} 条异常待确认",
                "异常类型：" + "、".join(sorted({a["kind"] for a in pending_anomalies})) + "。",
                "逐条确认异常是否属于业务正常波动；确认后标记，避免噪声淹没真实故障。",
                {"count": len(pending_anomalies), "critical": len(critical)},
            )
        )

    # ⑧ 调度健康
    enabled = [f for f in flows if f.get("enabled")]
    if flows and not enabled:
        signals.append(
            _signal(
                "info",
                "没有启用中的定时调度",
                f"{len(flows)} 个工作流全部为手动触发，OPS 视图的任务量会依赖人工操作。",
                "为需要周期性运行的工作流配置 cron 并启用。",
                {"flows": len(flows)},
            )
        )

    # ⑨ 完全无运行记录
    if not runs:
        signals.append(
            _signal(
                "info",
                "暂无运行记录",
                "该范围内还没有任何运行实例，健康度按满分处理，诊断暂无数据可依据。",
                "先手动运行一次工作流，让 OPS 视图与诊断积累基线。",
                {},
            )
        )

    order = {"critical": 0, "warning": 1, "info": 2}
    signals.sort(key=lambda s: order.get(s["severity"], 3))

    root_causes = [
        {"severity": s["severity"], "title": s["title"], "detail": s["detail"], "evidence": s["evidence"]}
        for s in signals
    ]
    suggestions = [{"severity": s["severity"], "title": s["title"], "action": s["suggestion"]} for s in signals]

    health = compute_health(
        counts["total"], counts["success"], counts["failed"], counts["running"], counts["queued"]
    )
    if not signals:
        summary = (
            f"{label} 最近 {counts['total']} 次任务运行正常"
            f"（成功率 {counts['success_rate']:.1%}），未发现异常信号。"
        )
    else:
        head = "、".join(s["title"] for s in signals[:3])
        summary = (
            f"{label} 健康度 {health['score']}（{health['label']}），"
            f"共识别 {len(root_causes)} 条信号，主要问题：{head}。"
            f"任务 {counts['total']} 次，成功 {counts['success']} / 失败 {counts['failed']}。"
        )

    return {
        "scope": scope or "platform",
        "scope_label": label,
        "summary": summary,
        "health": health,
        "counts": counts,
        "root_causes": root_causes,
        "suggestions": suggestions,
        "rules_applied": [
            {"id": "failure_rate", "threshold": FAILURE_RATE_THRESHOLD, "label": "失败率告警线"},
            {"id": "consecutive_failures", "threshold": CONSECUTIVE_FAIL_LIMIT, "label": "连续失败次数"},
            {"id": "timeout", "threshold": TIMEOUT_SECONDS, "label": "超时判定（秒）"},
        ],
        "generated_at": store.now(),
        "engine": "rule-based",
    }


def overview(workspace_id: str) -> dict:
    """OPS 首页聚合：健康度 + 关键计数 + 最近任务，供前端一次拉取。"""
    tree = platform_node(workspace_id, depth="all")
    runs = store.rows(
        """
        SELECT r.*, f.name AS flow_name FROM flow_runs r
        LEFT JOIN flows f ON f.id = r.flow_id
        WHERE f.workspace_id = ? ORDER BY coalesce(r.started_at, '') DESC LIMIT 20
        """,
        (workspace_id,),
    )
    counts = _tally(runs)
    return {
        "health": tree["health"],
        "metrics": tree["metrics"],
        "counts": counts,
        "recent_runs": [
            {
                "id": r.get("id"),
                "flow_id": r.get("flow_id"),
                "flow_name": r.get("flow_name") or r.get("flow_id"),
                "status": r.get("status"),
                "trigger": r.get("trigger"),
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "duration_seconds": round(_duration_seconds(r) or 0, 1),
            }
            for r in runs
        ],
        "timeout_threshold_seconds": TIMEOUT_SECONDS,
    }
