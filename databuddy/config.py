"""全局配置。全部可用环境变量覆盖，也可运行时在「设置」页修改（存 metadata 库）。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """极简 .env 解析，避免额外依赖。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

DATA_DIR = Path(os.environ.get("DATABUDDY_DATA_DIR", ROOT / "data")).resolve()
DB_PATH = DATA_DIR / "metadata.db"
WAREHOUSE_PATH = DATA_DIR / "warehouse.duckdb"
UPLOAD_DIR = DATA_DIR / "uploads"
WEB_DIR = ROOT / "web"

HOST = os.environ.get("DATABUDDY_HOST", "127.0.0.1")
PORT = int(os.environ.get("DATABUDDY_PORT", "8000"))

ADMIN_EMAIL = os.environ.get("DATABUDDY_ADMIN_EMAIL", "admin@local")
ADMIN_PASSWORD = os.environ.get("DATABUDDY_ADMIN_PASSWORD", "admin123")

SESSION_TTL_HOURS = int(os.environ.get("DATABUDDY_SESSION_TTL_HOURS", "720"))


def secret_key() -> str:
    """本地会话签名密钥，首次启动生成并持久化，重启后 token 仍有效。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / ".secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_hex(32)
    path.write_text(value, encoding="utf-8")
    return value


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)
