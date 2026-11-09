"""Buddy 定时任务执行器。

与工作流不同，定时任务跑的是「一次 Agent 对话」：创建会话 → 执行提示词 → 记录结果。
这样早上 9 点的经营早报会真的产生一条会话，用户点进去能看完整过程与产物。
"""
from __future__ import annotations

import time

from . import store

_running: set[str] = set()


def _actor(task: dict) -> dict | None:
    """定时任务没有交互用户，回落到任务归属人或工作空间管理员。"""
    uid = task.get("user_id")
    row = None
    if uid:
        row = store.row("SELECT * FROM users WHERE id = ?", (uid,))
    if not row:
        row = store.row(
            "SELECT u.* FROM users u JOIN workspace_members m ON m.user_id = u.id "
            "WHERE m.workspace_id = ? AND m.role = 'admin' ORDER BY u.created_at LIMIT 1",
            (task.get("workspace_id"),),
        )
    if not row:
        row = store.row("SELECT * FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1")
    if row:
        row.pop("password_hash", None)
    return row


def run_task(task_id: str) -> dict:
    from .harness import runtime  # 延迟导入，避免与 tools 的循环依赖

    task = store.row("SELECT * FROM buddy_tasks WHERE id = ?", (task_id,))
    if not task:
        raise ValueError("定时任务不存在")
    if task_id in _running:
        raise ValueError("该任务正在执行中")
    _running.add(task_id)

    workspace_id = task["workspace_id"]
    actor = _actor(task)
    if not actor:
        _running.discard(task_id)
        raise ValueError("找不到可用的执行身份")

    conv_id = store.new_id()
    store.insert(
        "conversations",
        {
            "id": conv_id,
            "workspace_id": workspace_id,
            "user_id": actor["id"],
            "mode": "analysis",
            "chat_mode": "agent",
            "title": f"[定时] {task['name']}",
            "created_at": store.now(),
            "updated_at": store.now(),
        },
    )
    run_id = store.new_id()
    started = store.now()
    store.insert(
        "buddy_task_runs",
        {
            "id": run_id,
            "task_id": task_id,
            "workspace_id": workspace_id,
            "status": "running",
            "summary": "",
            "conversation_id": conv_id,
            "error": "",
            "started_at": started,
        },
    )

    try:
        rt = runtime.HarnessRuntime(workspace_id, actor, conv_id, mode="analysis", chat_mode="agent")
        out = rt.run(task["prompt"] or task["name"])
        status = "success"
        error = ""
        if out.get("status") == "awaiting_approval":
            status = "pending"
            error = "该任务需要人工确认才能继续，已挂起"
        summary = (out.get("content") or "")[:600]
        store.update(
            "buddy_task_runs",
            run_id,
            {"status": status, "summary": summary, "error": error, "finished_at": store.now()},
        )
        store.update("buddy_tasks", task_id, {"last_run": store.now()})
        store.audit(workspace_id, actor["id"], "run_buddy_task", task["name"], {"status": status})
        return {"id": run_id, "status": status, "summary": summary, "conversation_id": conv_id}
    except Exception as exc:  # noqa: BLE001
        store.update(
            "buddy_task_runs",
            run_id,
            {"status": "failed", "error": str(exc), "finished_at": store.now()},
        )
        raise
    finally:
        _running.discard(task_id)


def runs_of(task_id: str, limit: int = 10) -> list[dict]:
    return store.rows(
        "SELECT * FROM buddy_task_runs WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
        (task_id, limit),
    )


def recent_runs(workspace_id: str, limit: int = 20) -> list[dict]:
    return store.rows(
        """
        SELECT r.*, t.name AS task_name FROM buddy_task_runs r
        JOIN buddy_tasks t ON t.id = r.task_id
        WHERE r.workspace_id = ? ORDER BY r.started_at DESC LIMIT ?
        """,
        (workspace_id, limit),
    )


def run_due(when=None) -> list[dict]:
    """调度器每分钟调用一次：跑所有到点的任务。"""
    from .flows import cron_matches
    from datetime import datetime

    when = when or datetime.now()
    stamp = when.strftime("%Y-%m-%d %H:%M")
    out = []
    for task in store.rows(
        "SELECT * FROM buddy_tasks WHERE enabled = 1 AND cron IS NOT NULL AND cron != ''"
    ):
        if not cron_matches(task["cron"], when):
            continue
        key = f"{task['id']}@{stamp}"
        if store.scalar("SELECT 1 FROM settings WHERE key = ?", (key,)):
            continue
        store.set_setting(key, "1")
        try:
            out.append({**run_task(task["id"]), "task": task["name"]})
        except Exception as exc:  # noqa: BLE001
            out.append({"task": task["name"], "status": "failed", "error": str(exc)})
        time.sleep(0.05)
    return out
