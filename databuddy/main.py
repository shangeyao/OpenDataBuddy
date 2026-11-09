"""FastAPI 应用入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, seed
from .api import auth, chat, data, ops
from .flows import scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed.bootstrap()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(
    title="OpenDataBuddy",
    description="开源版大数据智能体工作台 · DeepSeek Harness 底座",
    version="0.1.0",
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

app.include_router(auth.router)
app.include_router(data.router)
app.include_router(chat.router)
app.include_router(ops.router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": "0.1.0", "auth": "local", "billing": "disabled"}


@app.get("/")
def index():
    target = config.WEB_DIR / "index.html"
    if not target.exists():
        return JSONResponse({"message": "前端未构建：缺少 web/index.html"}, status_code=500)
    return FileResponse(target)


if config.WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(config.WEB_DIR)), name="assets")
