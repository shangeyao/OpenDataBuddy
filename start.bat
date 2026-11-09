@echo off
chcp 65001 >nul
cd /d "%~dp0"

python -c "import fastapi, duckdb, uvicorn" 2>nul
if errorlevel 1 (
    echo [OpenDataBuddy] 首次运行，安装依赖...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败，请检查 Python 与网络。
        pause
        exit /b 1
    )
)

python run.py %*
