"""FastAPI 应用入口。

路由用「可选加载」的方式挂载：某个扩展模块缺失或导入失败时只跳过它并记日志，
不会让整个应用起不来。这样开源版可以按需裁剪能力，也方便分模块迭代。
"""
from __future__ import annotations

import importlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, seed
from .api import auth, chat, console, data, ops
from .flows import scheduler

log = logging.getLogger("databuddy")

# 核心路由：缺失即视为构建损坏，必须让错误暴露出来
CORE_ROUTERS = ["auth", "data", "chat", "ops", "console"]

# 扩展路由：按能力模块划分，缺失时降级跳过
EXTRA_ROUTERS = [
    "quality",      # 数据质量 / 剖析 / 漂移
    "workflow",     # 工作流引擎
    "cicd",         # Bundle 发布
    "mlops",        # 特征 / 模型注册 / 服务
    "catalog_ext",  # 卷 / 函数 / 外部目录 / 血缘
    "ingest_ext",   # 数据源模板 / 智能诊断
    "notebook",     # Studio Notebook
    "ops_ext",      # 告警 / 四层视图 / 安全防护
    "analysis",     # 问数 / 归因 / 报告
    "semantic_ext", # 本体 / 术语表
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed.bootstrap()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(
    title="OpenDataBuddy",
    description="开源版大数据智能体工作台 · DeepSeek Harness 底座",
    version="0.3.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _mount_core() -> None:
    for mod in (auth, data, chat, ops, console):
        app.include_router(mod.router)


def _mount_extras() -> list[str]:
    """挂载扩展路由，返回实际挂载成功的模块名。"""
    loaded: list[str] = []
    for name in EXTRA_ROUTERS:
        try:
            mod = importlib.import_module(f".api.{name}", package=__package__)
        except Exception as exc:  # noqa: BLE001 - 缺模块/语法错都不该阻断启动
            log.warning("扩展路由 %s 未加载：%s", name, exc)
            continue
        router = getattr(mod, "router", None)
        if router is None:
            log.warning("扩展路由 %s 缺少 router 对象", name)
            continue
        app.include_router(router)
        loaded.append(name)
    return loaded


_mount_core()
LOADED_EXTRAS = _mount_extras()


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": "0.3.0",
        "auth": "local",
        "billing": "disabled",
        "capabilities": LOADED_EXTRAS,
    }


@app.get("/api/capabilities")
def capabilities() -> dict:
    """前端可据此隐藏尚未就绪的入口。"""
    return {
        "core": CORE_ROUTERS,
        "extra": LOADED_EXTRAS,
        "missing": [m for m in EXTRA_ROUTERS if m not in LOADED_EXTRAS],
    }


@app.get("/")
def index():
    target = config.WEB_DIR / "index.html"
    if not target.exists():
        return JSONResponse({"message": "前端未构建：缺少 web/index.html"}, status_code=500)
    return FileResponse(target)


if config.WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(config.WEB_DIR)), name="assets")
