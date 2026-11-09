"""长期记忆：跨会话保留用户偏好与口径约定。

只存「会影响后续回答」的事实，不存闲聊 —— 记忆脏了比没有记忆更糟。
"""
from __future__ import annotations

from .. import store

MAX_ITEMS = 30


def remember(workspace_id: str, user_id: str, key: str, value: str) -> dict:
    key = (key or "").strip()[:80]
    value = (value or "").strip()[:500]
    if not key or not value:
        raise ValueError("key 与 value 都不能为空")
    existing = store.row(
        "SELECT id FROM memories WHERE workspace_id = ? AND user_id = ? AND key = ?",
        (workspace_id, user_id, key),
    )
    if existing:
        store.update("memories", existing["id"], {"value": value, "created_at": store.now()})
        return {"key": key, "value": value, "updated": True}
    store.insert(
        "memories",
        {
            "id": store.new_id(),
            "workspace_id": workspace_id,
            "user_id": user_id,
            "key": key,
            "value": value,
            "created_at": store.now(),
        },
    )
    return {"key": key, "value": value, "updated": False}


def recall(workspace_id: str, user_id: str, limit: int = MAX_ITEMS) -> list[dict]:
    return store.rows(
        "SELECT key, value, created_at FROM memories WHERE workspace_id = ? AND user_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (workspace_id, user_id, limit),
    )


def forget(workspace_id: str, user_id: str, key: str) -> None:
    store.execute(
        "DELETE FROM memories WHERE workspace_id = ? AND user_id = ? AND key = ?",
        (workspace_id, user_id, key),
    )


def block(workspace_id: str, user_id: str) -> str:
    items = recall(workspace_id, user_id)
    if not items:
        return ""
    lines = [f"- {m['key']}：{m['value']}" for m in items[:12]]
    return "【关于这位用户的长期记忆】\n" + "\n".join(lines)
