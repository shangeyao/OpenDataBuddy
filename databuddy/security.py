"""本地账号体系：替代原产品的腾讯云 CAM / 子账号 / 企业实名 / Agent 席位。

- 密码 PBKDF2-SHA256 加盐存储
- 会话 token 存本地库，可撤销、可过期
- 两个平台角色：admin / member
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request

from . import config, store


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(calc, digest)


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    created = datetime.now(timezone.utc)
    expires = created + timedelta(hours=config.SESSION_TTL_HOURS)
    store.insert(
        "sessions",
        {
            "token": token,
            "user_id": user_id,
            "created_at": created.isoformat(timespec="seconds"),
            "expires_at": expires.isoformat(timespec="seconds"),
        },
        auto_id=False,
    )
    return token


def revoke_session(token: str) -> None:
    store.execute("DELETE FROM sessions WHERE token = ?", (token,))


def user_by_token(token: str) -> dict | None:
    if not token:
        return None
    sess = store.row("SELECT * FROM sessions WHERE token = ?", (token,))
    if not sess:
        return None
    expires = datetime.fromisoformat(sess["expires_at"])
    if expires < datetime.now(timezone.utc):
        revoke_session(token)
        return None
    return store.row("SELECT * FROM users WHERE id = ?", (sess["user_id"],))


def _token_from_request(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get("db_token", "")


def current_user(request: Request) -> dict:
    user = user_by_token(_token_from_request(request))
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    user.pop("password_hash", None)
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def effective_workspace(user: dict, workspace_id: str | None) -> str:
    """校验用户对工作空间的访问权，返回合法的 workspace_id。"""
    if workspace_id:
        allowed = store.scalar(
            "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user["id"]),
        )
        if not allowed and user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="无权访问该工作空间")
        return workspace_id
    ws = store.row(
        "SELECT w.id FROM workspaces w JOIN workspace_members m ON m.workspace_id = w.id "
        "WHERE m.user_id = ? ORDER BY w.created_at LIMIT 1",
        (user["id"],),
    )
    if not ws:
        ws = store.row("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
    if not ws:
        raise HTTPException(status_code=400, detail="尚未创建工作空间")
    return ws["id"]


def workspace_from(request: Request, user: dict) -> str:
    return effective_workspace(user, request.headers.get("x-workspace-id") or None)
