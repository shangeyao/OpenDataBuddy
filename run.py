#!/usr/bin/env python
"""OpenDataBuddy 启动脚本。

    python run.py                # 默认 127.0.0.1:8000
    python run.py --port 8080
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser

from databuddy import config


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 OpenDataBuddy")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重启")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    os.environ["DATABUDDY_HOST"] = args.host
    os.environ["DATABUDDY_PORT"] = str(args.port)

    try:
        import uvicorn
    except ImportError:
        print("缺少依赖，请先执行：pip install -r requirements.txt", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{args.port}"
    print("=" * 56)
    print(" OpenDataBuddy · 开源大数据智能体工作台")
    print(f" 控制台   {url}")
    print(f" API 文档 {url}/api/docs")
    print(f" 数据目录 {config.DATA_DIR}")
    print("=" * 56)

    if not args.no_browser and not args.reload:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    uvicorn.run(
        "databuddy.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
