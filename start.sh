#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! python -c "import fastapi, duckdb, uvicorn" >/dev/null 2>&1; then
  echo "[OpenDataBuddy] 首次运行，安装依赖..."
  python -m pip install -r requirements.txt
fi

exec python run.py "$@"
