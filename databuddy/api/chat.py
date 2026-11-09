"""对话接口：会话、消息、Agent 运行、人工确认。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import store
from ..harness import runtime
from .auth import ctx

router = APIRouter(prefix="/api", tags=["chat"])


def _parse_meta(raw: str) -> dict:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def _message_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "meta": _parse_meta(row.get("meta") or "{}"),
        "created_at": row["created_at"],
    }


@router.get("/conversations")
def list_conversations(mode: str | None = None, ctxd: dict = Depends(ctx)) -> dict:
    sql = "SELECT * FROM conversations WHERE workspace_id = ? AND user_id = ?"
    params: list = [ctxd["workspace_id"], ctxd["user"]["id"]]
    if mode:
        sql += " AND mode = ?"
        params.append(mode)
    sql += " ORDER BY updated_at DESC LIMIT 60"
    rows = store.rows(sql, params)
    for r in rows:
        r["message_count"] = store.scalar(
            "SELECT count(*) FROM messages WHERE conversation_id = ?", (r["id"],)
        )
    return {"conversations": rows}


class ConversationIn(BaseModel):
    mode: str = "engineering"
    chat_mode: str = "agent"
    title: str = "新会话"


@router.post("/conversations")
def create_conversation(payload: ConversationIn, ctxd: dict = Depends(ctx)) -> dict:
    row = {
        "id": store.new_id(),
        "workspace_id": ctxd["workspace_id"],
        "user_id": ctxd["user"]["id"],
        "mode": payload.mode,
        "chat_mode": payload.chat_mode,
        "title": payload.title,
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("conversations", row)
    return {"conversation": row}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, ctxd: dict = Depends(ctx)) -> dict:
    conv = store.row(
        "SELECT * FROM conversations WHERE id = ? AND workspace_id = ?",
        (conversation_id, ctxd["workspace_id"]),
    )
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = store.rows(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at", (conversation_id,)
    )
    pending = store.rows(
        "SELECT * FROM approvals WHERE conversation_id = ? AND status = 'pending'",
        (conversation_id,),
    )
    approvals = []
    for p in pending:
        payload = _parse_meta(p["args"])
        approvals.append(
            {
                "id": p["id"],
                "tool": p["tool"],
                "reason": p["reason"],
                "args": (payload.get("call") or {}).get("args") or {},
                "created_at": p["created_at"],
            }
        )
    return {
        "conversation": conv,
        "messages": [_message_dict(m) for m in messages],
        "pending_approvals": approvals,
    }


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, ctxd: dict = Depends(ctx)) -> dict:
    conv = store.row(
        "SELECT id FROM conversations WHERE id = ? AND workspace_id = ?",
        (conversation_id, ctxd["workspace_id"]),
    )
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    store.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
    store.execute("DELETE FROM approvals WHERE conversation_id = ?", (conversation_id,))
    store.delete("conversations", conversation_id)
    return {"ok": True}


class MessageIn(BaseModel):
    content: str
    mode: str | None = None
    chat_mode: str | None = None
    model: str | None = None


@router.post("/conversations/{conversation_id}/messages")
def send_message(conversation_id: str, payload: MessageIn, ctxd: dict = Depends(ctx)) -> dict:
    conv = store.row(
        "SELECT * FROM conversations WHERE id = ? AND workspace_id = ?",
        (conversation_id, ctxd["workspace_id"]),
    )
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    if not (payload.content or "").strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    mode = payload.mode or conv["mode"] or "engineering"
    chat_mode = payload.chat_mode or conv["chat_mode"] or "agent"
    store.update(
        "conversations",
        conversation_id,
        {"mode": mode, "chat_mode": chat_mode, "updated_at": store.now()},
    )

    rt = runtime.HarnessRuntime(
        ctxd["workspace_id"], ctxd["user"], conversation_id, mode=mode, chat_mode=chat_mode,
        model=payload.model,
    )
    out = rt.run(payload.content.strip())
    out["user_message"] = {"role": "user", "content": payload.content.strip()}
    return out


@router.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    if not row or row["status"] != "pending":
        raise HTTPException(status_code=404, detail="审批单不存在或已处理")
    conv = store.row(
        "SELECT * FROM conversations WHERE id = ? AND workspace_id = ?",
        (row["conversation_id"], ctxd["workspace_id"]),
    )
    if not conv:
        raise HTTPException(status_code=403, detail="无权处理该审批")
    store.update(
        "approvals", approval_id,
        {"status": "approved", "decided_at": store.now(), "decided_by": ctxd["user"]["id"]},
    )
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "approve_tool", row["tool"])
    rt = runtime.HarnessRuntime(
        ctxd["workspace_id"], ctxd["user"], row["conversation_id"],
        mode=conv["mode"], chat_mode=conv["chat_mode"],
    )
    return rt.resume(row)


@router.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = store.row("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    if not row or row["status"] != "pending":
        raise HTTPException(status_code=404, detail="审批单不存在或已处理")
    store.update(
        "approvals", approval_id,
        {"status": "rejected", "decided_at": store.now(), "decided_by": ctxd["user"]["id"]},
    )
    content = f"已取消 **{row['tool']}** 这一步。你可以换个方式让我做，或者直接告诉我下一步。"
    meta = {
        "engine": "system",
        "trace": [{"tool": row["tool"], "args": {}, "ok": False, "duration_ms": 0, "summary": "已拒绝"}],
        "artifacts": [{"type": "notice", "level": "info", "title": "操作已取消", "text": row["reason"]}],
        "usage": {},
    }
    store.insert(
        "messages",
        {
            "id": store.new_id(),
            "conversation_id": row["conversation_id"],
            "role": "assistant",
            "content": content,
            "meta": json.dumps(meta, ensure_ascii=False),
            "created_at": store.now(),
        },
    )
    return {"status": "ok", "content": content, "approval": None, "meta": meta}


@router.get("/conversations/{conversation_id}/artifacts")
def conversation_artifacts(conversation_id: str, ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        "SELECT meta FROM messages WHERE conversation_id = ? AND role = 'assistant' ORDER BY created_at",
        (conversation_id,),
    )
    items = []
    for r in rows:
        meta = _parse_meta(r["meta"])
        for a in meta.get("artifacts", []):
            items.append(a)
    return {"artifacts": items[::-1]}
