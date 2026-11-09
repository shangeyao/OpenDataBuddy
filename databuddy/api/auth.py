"""认证、用户、工作空间、系统设置。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import config, security, store
from ..harness import providers, trace

router = APIRouter(prefix="/api", tags=["auth"])


def ctx(request: Request, user: dict = Depends(security.current_user)) -> dict:
    """统一解析当前用户与工作空间。"""
    return {"user": user, "workspace_id": security.workspace_from(request, user), "request": request}


class LoginIn(BaseModel):
    email: str
    password: str


class UserIn(BaseModel):
    email: str
    name: str
    password: str
    role: str = "member"


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/auth/login")
def login(payload: LoginIn) -> dict:
    user = store.row("SELECT * FROM users WHERE lower(email) = lower(?)", (payload.email.strip(),))
    if not user or not security.verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")
    token = security.create_session(user["id"])
    store.audit(None, user["id"], "login", payload.email)
    user.pop("password_hash", None)
    return {"token": token, "user": user, "workspaces": _workspaces_of(user)}


@router.post("/auth/logout")
def logout(request: Request, user: dict = Depends(security.current_user)) -> dict:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        security.revoke_session(auth[7:].strip())
    return {"ok": True}


@router.get("/auth/me")
def me(user: dict = Depends(security.current_user)) -> dict:
    return {"user": user, "workspaces": _workspaces_of(user)}


@router.post("/auth/password")
def change_password(payload: PasswordIn, user: dict = Depends(security.current_user)) -> dict:
    row = store.row("SELECT password_hash FROM users WHERE id = ?", (user["id"],))
    if not row or not security.verify_password(payload.old_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="原密码不正确")
    store.update("users", user["id"], {"password_hash": security.hash_password(payload.new_password)})
    return {"ok": True}


def _workspaces_of(user: dict) -> list[dict]:
    rows = store.rows(
        """
        SELECT w.id, w.name, w.description, m.role
        FROM workspaces w JOIN workspace_members m ON m.workspace_id = w.id
        WHERE m.user_id = ? ORDER BY w.created_at
        """,
        (user["id"],),
    )
    if not rows and user.get("role") == "admin":
        rows = store.rows("SELECT id, name, description, 'admin' AS role FROM workspaces ORDER BY created_at")
    return rows


# ------------------------------------------------------------------- 用户管理


@router.get("/users")
def list_users(user: dict = Depends(security.current_user)) -> dict:
    rows = store.rows("SELECT id, email, name, role, created_at FROM users ORDER BY created_at")
    return {"users": rows}


@router.post("/users")
def create_user(payload: UserIn, admin: dict = Depends(security.require_admin)) -> dict:
    if store.row("SELECT id FROM users WHERE lower(email) = lower(?)", (payload.email,)):
        raise HTTPException(status_code=400, detail="该邮箱已存在")
    row = {
        "id": store.new_id(),
        "email": payload.email.strip(),
        "name": payload.name.strip() or payload.email.split("@")[0],
        "password_hash": security.hash_password(payload.password),
        "role": payload.role if payload.role in ("admin", "member") else "member",
        "created_at": store.now(),
    }
    store.insert("users", row)
    # 默认加入第一个工作空间
    ws = store.row("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
    if ws:
        store.insert(
            "workspace_members",
            {"workspace_id": ws["id"], "user_id": row["id"], "role": "member"},
            auto_id=False,
        )
    store.audit(ws["id"] if ws else None, admin["id"], "create_user", payload.email)
    row.pop("password_hash")
    return {"user": row}


@router.delete("/users/{user_id}")
def delete_user(user_id: str, admin: dict = Depends(security.require_admin)) -> dict:
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="不能删除自己")
    store.execute("DELETE FROM users WHERE id = ?", (user_id,))
    store.execute("DELETE FROM workspace_members WHERE user_id = ?", (user_id,))
    store.audit(None, admin["id"], "delete_user", user_id)
    return {"ok": True}


# ------------------------------------------------------------------ 工作空间


class WorkspaceIn(BaseModel):
    name: str
    description: str = ""


class MemberIn(BaseModel):
    user_id: str
    role: str = "member"


@router.get("/workspaces")
def list_workspaces(user: dict = Depends(security.current_user)) -> dict:
    return {"workspaces": _workspaces_of(user)}


@router.post("/workspaces")
def create_workspace(payload: WorkspaceIn, user: dict = Depends(security.current_user)) -> dict:
    if store.row("SELECT id FROM workspaces WHERE name = ?", (payload.name,)):
        raise HTTPException(status_code=400, detail="工作空间名称已存在")
    ws_id = store.new_id()
    store.insert(
        "workspaces",
        {
            "id": ws_id,
            "name": payload.name,
            "description": payload.description,
            "owner_id": user["id"],
            "created_at": store.now(),
        },
    )
    store.insert(
        "workspace_members",
        {"workspace_id": ws_id, "user_id": user["id"], "role": "admin"},
        auto_id=False,
    )
    store.audit(ws_id, user["id"], "create_workspace", payload.name)
    return {"workspace": {"id": ws_id, "name": payload.name, "description": payload.description, "role": "admin"}}


@router.patch("/workspaces/{ws_id}")
def update_workspace(ws_id: str, payload: WorkspaceIn, user: dict = Depends(security.current_user)) -> dict:
    security.effective_workspace(user, ws_id)
    store.update("workspaces", ws_id, {"name": payload.name, "description": payload.description})
    return {"ok": True}


@router.get("/workspaces/{ws_id}/members")
def list_members(ws_id: str, user: dict = Depends(security.current_user)) -> dict:
    security.effective_workspace(user, ws_id)
    rows = store.rows(
        """
        SELECT u.id, u.email, u.name, u.role AS global_role, m.role AS space_role
        FROM workspace_members m JOIN users u ON u.id = m.user_id
        WHERE m.workspace_id = ? ORDER BY u.created_at
        """,
        (ws_id,),
    )
    return {"members": rows}


@router.post("/workspaces/{ws_id}/members")
def add_member(ws_id: str, payload: MemberIn, admin: dict = Depends(security.require_admin)) -> dict:
    store.execute(
        "INSERT OR REPLACE INTO workspace_members(workspace_id, user_id, role) VALUES (?,?,?)",
        (ws_id, payload.user_id, payload.role),
    )
    store.audit(ws_id, admin["id"], "add_member", payload.user_id)
    return {"ok": True}


@router.delete("/workspaces/{ws_id}/members/{user_id}")
def remove_member(ws_id: str, user_id: str, admin: dict = Depends(security.require_admin)) -> dict:
    store.execute(
        "DELETE FROM workspace_members WHERE workspace_id = ? AND user_id = ?", (ws_id, user_id)
    )
    return {"ok": True}


# ---------------------------------------------------------------------- 设置


class SettingsIn(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None
    workspace_rule: str | None = None


@router.get("/settings")
def get_settings(user: dict = Depends(security.current_user)) -> dict:
    cfg = providers.resolve_config()
    key = cfg.api_key or ""
    masked = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已配置" if key else "")
    return {
        "llm": {
            "provider": cfg.provider,
            "base_url": cfg.base_url,
            "model": cfg.model,
            "temperature": cfg.temperature,
            "api_key_masked": masked,
            "configured": providers.is_configured(cfg),
        },
        "workspace_rule": store.get_setting("workspace_rule", ""),
        "platform": {
            "version": "0.1.0",
            "data_dir": str(config.DATA_DIR),
            "auth": "local",
            "billing": "disabled",
        },
        "providers": [
            {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
            {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            {"id": "ollama", "label": "Ollama 本地", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5-coder:7b"},
        ],
    }


@router.put("/settings")
def update_settings(payload: SettingsIn, admin: dict = Depends(security.require_admin)) -> dict:
    mapping = {
        "provider": "llm.provider",
        "base_url": "llm.base_url",
        "model": "llm.model",
    }
    for field, key in mapping.items():
        value = getattr(payload, field)
        if value is not None:
            store.set_setting(key, value)
    if payload.api_key is not None and payload.api_key != "":
        store.set_setting("llm.api_key", payload.api_key.strip())
    if payload.temperature is not None:
        store.set_setting("llm.temperature", str(payload.temperature))
    if payload.workspace_rule is not None:
        store.set_setting("workspace_rule", payload.workspace_rule)
    store.audit(None, admin["id"], "update_settings")
    return get_settings(admin)


@router.post("/settings/llm/test")
def test_llm(admin: dict = Depends(security.require_admin)) -> dict:
    return providers.ping()


@router.get("/settings/tools")
def list_tools(user: dict = Depends(security.current_user)) -> dict:
    from ..harness import skills

    return {"tools": skills.catalog_of_tools()}


@router.get("/ops/traces")
def trace_stats(ctxd: dict = Depends(ctx)) -> dict:
    return trace.stats(ctxd["workspace_id"])


@router.get("/ops/audit")
def audit_logs(limit: int = 100, ctxd: dict = Depends(ctx)) -> dict:
    rows = store.rows(
        """
        SELECT a.*, u.name AS user_name FROM audit_logs a
        LEFT JOIN users u ON u.id = a.user_id
        WHERE a.workspace_id = ? OR a.workspace_id IS NULL
        ORDER BY a.created_at DESC LIMIT ?
        """,
        (ctxd["workspace_id"], limit),
    )
    return {"logs": rows}
