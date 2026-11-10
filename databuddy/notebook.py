"""Studio 开发工作台：多语法 Notebook 内核。

能力对齐腾讯云 DataBuddy「数据工程 > Studio 开发工作台」：

* 三种 cell 语法：``sql`` / ``python`` / ``markdown``
* 13 个魔法命令（% 开头）：%sql %python %md %tables %desc %run %load %param
  %time %chart %vars %help %clear
* Python cell 在**持久化命名空间**里 ``exec``，同一 notebook 内变量跨 cell 共享
* SQL cell 支持 ``{{param}}`` 参数化，按类型安全转义成 SQL 字面量
* ``sys.path`` 注入 notebook 工作目录（含项目根），可 import 同目录其他 ``.py``
* 异常返回 traceback + 行号 + 出错源码行，前端直接红框展示

``notebooks`` 表由本模块 ``ensure_table()`` 自建（CREATE TABLE IF NOT EXISTS），
不改 ``store.py`` 的 SCHEMA。
"""
from __future__ import annotations

import builtins
import contextlib
import fnmatch
import io
import json
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from . import catalog, charts, config, store, warehouse

# --------------------------------------------------------------------- 建表

NOTEBOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS notebooks (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    cells TEXT DEFAULT '[]',                      -- [{id,type,source,output,status,elapsed_ms,...}]
    params TEXT DEFAULT '{}',                     -- notebook 级默认参数
    workdir TEXT DEFAULT '',                      -- 工作目录，空则用 data/notebooks
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notebooks_ws ON notebooks(workspace_id, name);
"""

_READY = False
_TABLE_LOCK = threading.RLock()


def ensure_table() -> None:
    """幂等建表：notebooks 由本模块托管，不动 store.py。"""
    global _READY
    if _READY:
        return
    with _TABLE_LOCK:
        if _READY:
            return
        with store.db() as conn:
            conn.executescript(NOTEBOOK_SCHEMA)
        _READY = True


# --------------------------------------------------------------- 运行时状态

_NAMESPACES: dict[str, dict[str, Any]] = {}
_RUNTIME_PARAMS: dict[str, dict[str, Any]] = {}
_LAST_RESULT: dict[str, dict] = {}
_NS_LOCK = threading.RLock()

# 注入到 Python 命名空间的辅助名，%vars 里不展示
HELPER_NAMES = {
    "query", "run_sql", "sql", "show", "params", "notebook_id",
    "workdir", "store", "catalog", "warehouse", "charts", "print",
}


# ------------------------------------------------------------------- 工具函数


def _json_load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _short(value: Any, limit: int = 200) -> Any:
    """把任意对象压成前端可安全渲染的标量。"""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _sql_literal(value: Any) -> str:
    """参数值 → SQL 字面量（不拼接原始用户输入，统一转义）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "NULL"
        return repr(value)
    if isinstance(value, (list, tuple, set)):
        return "(" + ", ".join(_sql_literal(v) for v in value) + ")"
    return "'" + str(value).replace("'", "''") + "'"


PARAM_RE = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")
# 占位符被引号包起来时（'{{x}}'）只替换成字符串内容，避免出现 ''ch_01'' 这种双重引号
QUOTED_PARAM_RE = re.compile(r"'[ \t]*\{\{\s*([A-Za-z_][\w.]*)\s*\}\}[ \t]*'")


class NotebookError(Exception):
    """Notebook 执行期可预期错误（参数缺失、表不存在等）。"""


def render_params(text: str, params: dict | None) -> str:
    """把 ``{{name}}`` 占位替换成 SQL 字面量；缺参数直接报错，避免静默执行错 SQL。

    两种写法都支持：``= '{{city}}'``（带引号）与 ``= {{city}}``（裸占位）。
    """
    values = params or {}
    missing: list[str] = []
    source = text or ""

    def quoted_repl(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        value = values[key]
        if value is None:
            return "NULL"
        if isinstance(value, (list, tuple, set)):
            return "(" + ", ".join(_sql_literal(v) for v in value) + ")"
        return "'" + str(value).replace("'", "''") + "'"

    def bare_repl(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return _sql_literal(values[key])

    out = QUOTED_PARAM_RE.sub(quoted_repl, source)
    out = PARAM_RE.sub(bare_repl, out)
    if missing:
        raise NotebookError(
            "缺少参数：" + "、".join(sorted(set(missing))) + "（用 %param 名称=值 设置）"
        )
    return out


def params_of(notebook: dict) -> dict:
    """合并 notebook 默认参数与运行时 %param 设置的参数。"""
    merged: dict[str, Any] = dict(_json_load(notebook.get("params"), {}))
    merged.update(_RUNTIME_PARAMS.get(notebook.get("id") or "", {}))
    return merged


# --------------------------------------------------------------- Notebook CRUD


def list_notebooks(workspace_id: str) -> list[dict]:
    ensure_table()
    rows = store.rows(
        "SELECT * FROM notebooks WHERE workspace_id = ? ORDER BY updated_at DESC, name",
        (workspace_id,),
    )
    out = []
    for r in rows:
        cells = _json_load(r.get("cells"), [])
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "description": r.get("description") or "",
                "workdir": r.get("workdir") or "",
                "cell_count": len(cells),
                "cell_types": sorted({(c.get("type") or "sql") for c in cells}),
                "params": _json_load(r.get("params"), {}),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            }
        )
    return out


def get_notebook(notebook_id: str, workspace_id: str | None = None) -> dict | None:
    ensure_table()
    if workspace_id:
        row = store.row(
            "SELECT * FROM notebooks WHERE id = ? AND workspace_id = ?",
            (notebook_id, workspace_id),
        )
    else:
        row = store.row("SELECT * FROM notebooks WHERE id = ?", (notebook_id,))
    if not row:
        return None
    row["cells"] = _json_load(row.get("cells"), [])
    row["params"] = _json_load(row.get("params"), {})
    return row


def find_notebook(workspace_id: str, name_or_id: str) -> dict | None:
    """按 id 或名称定位 notebook（%run 用）。"""
    found = get_notebook(name_or_id, workspace_id)
    if found:
        return found
    ensure_table()
    row = store.row(
        "SELECT id FROM notebooks WHERE workspace_id = ? AND name = ? ORDER BY updated_at DESC LIMIT 1",
        (workspace_id, name_or_id),
    )
    return get_notebook(row["id"], workspace_id) if row else None


def _starter_cells() -> list[dict]:
    return [
        _blank_cell(
            "markdown",
            "# 新建 Notebook\n\n"
            "- 输入 `%help` 查看全部魔法命令\n"
            "- 直接写 SQL 查询数据，支持 `{{param}}` 参数化\n"
            "- Python cell 变量跨 cell 共享，可 import 同目录 `.py`",
        ),
        _blank_cell("sql", "%tables"),
    ]


def create_notebook(
    workspace_id: str,
    name: str,
    description: str = "",
    cells: list[dict] | None = None,
    params: dict | None = None,
    workdir: str = "",
) -> dict:
    ensure_table()
    title = (name or "").strip() or "未命名 Notebook"
    if store.row(
        "SELECT id FROM notebooks WHERE workspace_id = ? AND name = ?", (workspace_id, title)
    ):
        title = f"{title}-{store.new_id()[:4]}"
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "name": title,
        "description": description or "",
        "cells": _json_dump(_normalize_cells(cells) if cells else _starter_cells()),
        "params": _json_dump(params or {}),
        "workdir": workdir or "",
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("notebooks", row)
    row["cells"] = _json_load(row["cells"], [])
    row["params"] = _json_load(row["params"], {})
    return row


def update_notebook(notebook_id: str, data: dict) -> dict | None:
    ensure_table()
    payload: dict[str, Any] = {}
    if "name" in data and data["name"]:
        payload["name"] = str(data["name"]).strip()
    if "description" in data:
        payload["description"] = data.get("description") or ""
    if "workdir" in data:
        payload["workdir"] = data.get("workdir") or ""
    if "params" in data:
        payload["params"] = _json_dump(data.get("params") or {})
    if "cells" in data:
        payload["cells"] = _json_dump(_normalize_cells(data.get("cells") or []))
    if payload:
        payload["updated_at"] = store.now()
        store.update("notebooks", notebook_id, payload)
    return get_notebook(notebook_id)


def delete_notebook(notebook_id: str) -> None:
    ensure_table()
    store.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))
    with _NS_LOCK:
        _NAMESPACES.pop(notebook_id, None)
        _RUNTIME_PARAMS.pop(notebook_id, None)
        _LAST_RESULT.pop(notebook_id, None)


def _normalize_cells(cells: list) -> list[dict]:
    out = []
    for raw in cells or []:
        cell = dict(raw or {})
        ctype = str(cell.get("type") or "sql").lower()
        if ctype not in ("sql", "python", "markdown"):
            ctype = "sql"
        out.append(
            {
                "id": cell.get("id") or store.new_id("c"),
                "type": ctype,
                "source": cell.get("source") or "",
                "output": cell.get("output") or "",
                "status": cell.get("status") or "idle",
                "elapsed_ms": cell.get("elapsed_ms") or 0,
                "error": cell.get("error") or "",
                "traceback": cell.get("traceback") or "",
                "line_no": cell.get("line_no"),
                "source_line": cell.get("source_line") or "",
                "magic": cell.get("magic") or "",
                "columns": cell.get("columns") or [],
                "rows": cell.get("rows") or [],
                "chart": cell.get("chart") or None,
            }
        )
    return out


def _blank_cell(cell_type: str = "sql", source: str = "") -> dict:
    return {
        "id": store.new_id("c"),
        "type": cell_type if cell_type in ("sql", "python", "markdown") else "sql",
        "source": source or "",
        "output": "",
        "status": "idle",
        "elapsed_ms": 0,
        "error": "",
        "traceback": "",
        "line_no": None,
        "source_line": "",
        "magic": "",
        "columns": [],
        "rows": [],
        "chart": None,
    }


def add_cell(notebook_id: str, cell_type: str = "sql", source: str = "", index: int | None = None) -> dict:
    notebook = get_notebook(notebook_id)
    if not notebook:
        raise NotebookError("Notebook 不存在")
    cells = notebook["cells"]
    cell = _blank_cell(cell_type, source)
    if index is None or index < 0 or index > len(cells):
        cells.append(cell)
    else:
        cells.insert(int(index), cell)
    _save_cells(notebook_id, cells)
    return cell


def update_cell(notebook_id: str, cell_id: str, data: dict) -> dict | None:
    notebook = get_notebook(notebook_id)
    if not notebook:
        raise NotebookError("Notebook 不存在")
    cells = notebook["cells"]
    for i, cell in enumerate(cells):
        if cell.get("id") != cell_id:
            continue
        if "type" in data and data["type"] in ("sql", "python", "markdown"):
            cell["type"] = data["type"]
        if "source" in data:
            cell["source"] = data.get("source") or ""
        if data.get("reset_output"):
            cell.update({
                "output": "", "status": "idle", "elapsed_ms": 0, "error": "",
                "traceback": "", "line_no": None, "source_line": "", "magic": "",
                "columns": [], "rows": [], "chart": None,
            })
        cells[i] = cell
        _save_cells(notebook_id, cells)
        return cell
    raise NotebookError("Cell 不存在")


def delete_cell(notebook_id: str, cell_id: str) -> bool:
    notebook = get_notebook(notebook_id)
    if not notebook:
        raise NotebookError("Notebook 不存在")
    cells = [c for c in notebook["cells"] if c.get("id") != cell_id]
    if len(cells) == len(notebook["cells"]):
        return False
    _save_cells(notebook_id, cells)
    return True


def move_cell(notebook_id: str, cell_id: str, delta: int) -> list[dict]:
    notebook = get_notebook(notebook_id)
    if not notebook:
        raise NotebookError("Notebook 不存在")
    cells = notebook["cells"]
    idx = next((i for i, c in enumerate(cells) if c.get("id") == cell_id), -1)
    if idx < 0:
        raise NotebookError("Cell 不存在")
    target = max(0, min(len(cells) - 1, idx + int(delta)))
    cells.insert(target, cells.pop(idx))
    _save_cells(notebook_id, cells)
    return cells


def _save_cells(notebook_id: str, cells: list[dict]) -> None:
    store.update(
        "notebooks",
        notebook_id,
        {"cells": _json_dump(cells), "updated_at": store.now()},
    )


def _persist_cell_result(notebook_id: str, cell_id: str, result: dict) -> None:
    """把执行结果回写到 cell 上（前端刷新后仍能看到输出）。"""
    notebook = get_notebook(notebook_id)
    if not notebook:
        return
    cells = notebook["cells"]
    changed = False
    for i, cell in enumerate(cells):
        if cell.get("id") != cell_id:
            continue
        cells[i] = {
            **cell,
            "output": result.get("output") or "",
            "status": "error" if result.get("error") else "success",
            "elapsed_ms": result.get("elapsed_ms") or 0,
            "error": result.get("error") or "",
            "traceback": result.get("traceback") or "",
            "line_no": result.get("line_no"),
            "source_line": result.get("source_line") or "",
            "magic": result.get("magic") or "",
            "columns": (result.get("columns") or [])[:40],
            "rows": (result.get("rows") or [])[:100],
            "chart": result.get("chart") or None,
        }
        changed = True
        break
    if changed:
        _save_cells(notebook_id, cells)


# ---------------------------------------------------------------- 工作目录 / 命名空间


def workdir_of(notebook: dict) -> Path:
    raw = (notebook or {}).get("workdir") or ""
    if raw:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = config.ROOT / path
    else:
        path = config.DATA_DIR / "notebooks"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def _ensure_sys_path(*paths: str) -> None:
    """把工作目录注入 sys.path，支持 import 同目录 .py。"""
    for raw in paths:
        if not raw:
            continue
        if raw not in sys.path:
            sys.path.insert(0, raw)


def namespace_of(notebook: dict) -> dict[str, Any]:
    """同一 notebook 复用同一个 dict —— 变量因此可以跨 cell 共享。"""
    nb_id = notebook.get("id") or ""
    with _NS_LOCK:
        ns = _NAMESPACES.get(nb_id)
        if ns is None:
            ns = {
                "__name__": f"notebook_{nb_id[:8]}",
                "__doc__": "OpenDataBuddy Studio Notebook 命名空间",
                "__builtins__": builtins,
            }
            _inject_helpers(ns, notebook)
            _NAMESPACES[nb_id] = ns
        else:
            _inject_helpers(ns, notebook)
        return ns


def _inject_helpers(ns: dict, notebook: dict) -> None:
    workspace_id = notebook.get("workspace_id") or ""

    def query(sql: str, limit: int = 200) -> dict:
        """在 notebook 里跑一条只读 SQL（自动注入当前工作空间）。"""
        return catalog.run_workspace_sql(workspace_id, sql, limit=limit)

    def run_sql(sql: str, limit: int = 200) -> dict:
        return query(sql, limit=limit)

    def show(value: Any) -> None:
        """把结果标记为「上次结果」，供 %chart 渲染。"""
        _LAST_RESULT[notebook.get("id") or ""] = {
            "columns": list(value.get("columns") or []) if isinstance(value, dict) else [],
            "rows": list(value.get("rows") or []) if isinstance(value, dict) else [],
        }

    ns.setdefault("query", query)
    ns.setdefault("run_sql", run_sql)
    ns.setdefault("sql", query)
    ns.setdefault("show", show)
    ns.setdefault("store", store)
    ns.setdefault("catalog", catalog)
    ns.setdefault("warehouse", warehouse)
    ns.setdefault("charts", charts)
    ns.setdefault("print", builtins.print)


def reset_namespace(notebook_id: str) -> None:
    with _NS_LOCK:
        _NAMESPACES.pop(notebook_id, None)
        _RUNTIME_PARAMS.pop(notebook_id, None)
        _LAST_RESULT.pop(notebook_id, None)


# ------------------------------------------------------------------ 错误格式化


def _error_payload(exc: BaseException, filename: str, source: str) -> dict:
    tb_text = traceback.format_exc()
    line_no: int | None = None
    source_line = ""
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    for frame in frames:
        if frame.filename == filename:
            line_no = frame.lineno
    if line_no is None and frames:
        last = frames[-1]
        line_no = last.lineno
        source_line = (last.line or "").strip()
    if line_no is not None and not source_line:
        lines = (source or "").splitlines()
        if 0 < line_no <= len(lines):
            source_line = lines[line_no - 1].strip()
    message = f"{type(exc).__name__}: {exc}"
    return {
        "error": message,
        "traceback": tb_text,
        "line_no": line_no,
        "source_line": source_line,
    }


# -------------------------------------------------------------------- 执行内核


def _empty_result(cell_type: str, cell_id: str = "") -> dict:
    return {
        "ok": True,
        "cell_id": cell_id,
        "type": cell_type,
        "magic": "",
        "output": "",
        "columns": [],
        "rows": [],
        "row_count": 0,
        "chart": None,
        "elapsed_ms": 0,
        "error": "",
        "traceback": "",
        "line_no": None,
        "source_line": "",
    }


def _result_from_value(value: Any) -> tuple[list, list]:
    """Python 对象 → (columns, rows)，尽量能画成表格。"""
    if value is None:
        return [], []
    if isinstance(value, dict):
        if "columns" in value and "rows" in value:
            rows = [
                list(r) if isinstance(r, (list, tuple)) else [_short(r)]
                for r in (value.get("rows") or [])[:200]
            ]
            return list(value.get("columns") or []), rows
        return ["键", "值"], [[str(k), _short(v)] for k, v in list(value.items())[:200]]
    # pandas.DataFrame（可选，未安装不影响）
    if hasattr(value, "columns") and hasattr(value, "head"):
        try:
            columns = [str(c) for c in list(value.columns)]
            rows = [[_short(v) for v in row] for row in value.head(200).values.tolist()]
            return columns, rows
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(x, dict) for x in value):
            columns = list(value[0].keys())
            return columns, [[_short(r.get(c)) for c in columns] for r in value[:200]]
        if value and all(isinstance(x, (list, tuple)) for x in value):
            width = max(len(x) for x in value)
            return [f"c{i + 1}" for i in range(width)], [
                [_short(v) for v in row] for row in value[:200]
            ]
        return ["值"], [[_short(x)] for x in value[:200]]
    # 标量：给一个 1x1 表，前端能渲染成 KPI，也能被 %chart 复用
    if isinstance(value, (int, float, bool, str)):
        return ["值"], [[_short(value)]]
    return ["值"], [[_short(value)]]


def _split_last_expr(source: str) -> tuple[str, str] | None:
    """把「最后一行是表达式」拆出来，模拟 Jupyter 的自动展示。"""
    lines = (source or "").rstrip("\n").split("\n")
    if not lines or not lines[0].strip():
        return None
    for cut in range(len(lines), 0, -1):
        head = "\n".join(lines[: cut - 1])
        tail = "\n".join(lines[cut - 1:])
        try:
            compile(tail, "<probe>", "eval")
        except SyntaxError:
            continue
        if head:
            try:
                compile(head, "<probe>", "exec")
            except SyntaxError:
                continue
        return head, tail
    return None


def _run_python(notebook: dict, source: str, params: dict) -> dict:
    nb_id = notebook.get("id") or ""
    cell_id = ""
    result = _empty_result("python")
    filename = f"<notebook:{nb_id[:8]}>"
    ns = namespace_of(notebook)
    workdir = workdir_of(notebook)
    _ensure_sys_path(str(workdir), str(config.ROOT))
    ns["params"] = dict(params or {})
    ns["notebook_id"] = nb_id
    ns["workdir"] = str(workdir)

    started = time.perf_counter()
    buf = io.StringIO()
    tail_value: Any = None
    has_tail = False
    code_text = source or ""
    split = _split_last_expr(code_text)
    try:
        if split:
            head, tail = split
            compiled = compile(head, filename, "exec") if head else None
            tail_code = compile(tail, filename, "eval")
        else:
            compiled = compile(code_text, filename, "exec")
            tail_code = None
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if compiled is not None:
                exec(compiled, ns)  # noqa: S102 —— notebook 内核本就要执行用户代码
            if tail_code is not None:
                tail_value = eval(tail_code, ns)  # noqa: S307
                has_tail = True
    except BaseException as exc:  # noqa: BLE001 —— 用户代码什么都可能抛
        payload = _error_payload(exc, filename, code_text)
        result.update(payload)
        result["ok"] = False
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        result["output"] = buf.getvalue().strip()
        result["traceback"] = _decorate_traceback(
            payload.get("traceback") or "", payload.get("line_no"), payload.get("source_line")
        )
        return result

    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    printed = buf.getvalue()
    result["output"] = printed.rstrip("\n")

    display_value = tail_value if has_tail else None
    if has_tail:
        columns, rows = _result_from_value(display_value)
        if columns:
            result["columns"] = columns
            result["rows"] = rows
            result["row_count"] = len(rows)
            if rows:
                result["chart"] = charts.infer({"columns": columns, "rows": rows})
            _LAST_RESULT[nb_id] = {"columns": columns, "rows": rows}
        elif display_value is not None:
            text = repr(display_value)
            result["output"] = (result["output"] + "\n" + text).strip()
    else:
        last = _LAST_RESULT.get(nb_id)
        if last and last.get("columns") and result["output"].strip() == "":
            # 用户显式 show() 了结果
            result["columns"] = last["columns"]
            result["rows"] = last["rows"]
            result["row_count"] = len(last["rows"])
            result["chart"] = charts.infer(last)
    return result


def _decorate_traceback(tb_text: str, line_no: int | None, source_line: str) -> str:
    """在 traceback 末尾补一段「出错位置」，前端红框里直接可读。"""
    if not tb_text:
        return ""
    if line_no is None:
        return tb_text
    marker = f"第 {line_no} 行"
    if source_line:
        marker += f"：{source_line}"
    if marker in tb_text:
        return tb_text
    return tb_text.rstrip("\n") + f"\n\n── 出错位置 ──\n{marker}"


def _looks_like_sql(text: str) -> bool:
    first = re.split(r"\s+", (text or "").strip(), maxsplit=1)[0].strip("(;").lower()
    return first in warehouse.ALLOWED_STARTS


def _run_sql(notebook: dict, sql: str, params: dict, limit: int = 200) -> dict:
    result = _empty_result("sql")
    started = time.perf_counter()
    try:
        rendered = render_params(sql, params)
    except NotebookError as exc:
        result.update({"ok": False, "error": str(exc), "traceback": str(exc)})
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return result
    result["rendered_sql"] = rendered
    try:
        out = catalog.run_workspace_sql(notebook.get("workspace_id") or "", rendered, limit=limit)
    except warehouse.SqlRejected as exc:
        result.update(
            {
                "ok": False,
                "error": f"SQL 被安全护栏拒绝：{exc}",
                "traceback": f"SqlRejected: {exc}",
            }
        )
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return result
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        result.update(
            {
                "ok": False,
                "error": message,
                "traceback": f"{message}\n\nSQL:\n{rendered}",
                "line_no": 1,
                "source_line": rendered.strip().splitlines()[0] if rendered.strip() else "",
            }
        )
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    result["columns"] = out.get("columns") or []
    result["rows"] = out.get("rows") or []
    result["row_count"] = out.get("row_count") or 0
    result["truncated"] = bool(out.get("truncated"))
    result["chart"] = charts.infer(out)
    _LAST_RESULT[notebook.get("id") or ""] = {
        "columns": result["columns"],
        "rows": result["rows"],
    }
    result["output"] = f"{result['row_count']} 行"
    if result.get("truncated"):
        result["output"] += "（已截断）"
    return result


# -------------------------------------------------------------------- 魔法命令


def magic_help() -> list[dict]:
    """魔法命令清单（前端渲染帮助面板 / 自动补全）。"""
    return [
        {"name": "%sql", "usage": "%sql <语句>", "category": "语言切换",
         "desc": "切换到 SQL 执行（支持 {{param}} 参数化）", "example": "%sql SELECT * FROM demo.retail.fact_orders LIMIT 5"},
        {"name": "%python", "usage": "%python <代码>", "category": "语言切换",
         "desc": "切换到 Python 执行，变量跨 cell 共享", "example": "%python total = 42"},
        {"name": "%md", "usage": "%md <文本>", "category": "语言切换",
         "desc": "把内容当 Markdown 渲染（写文档用）", "example": "%md ## 分析结论\nGMV 环比上升 12%"},
        {"name": "%tables", "usage": "%tables [pattern]", "category": "数据探索",
         "desc": "列出工作空间所有表，可按名称模糊过滤", "example": "%tables fact_*"},
        {"name": "%desc", "usage": "%desc <表名>", "category": "数据探索",
         "desc": "查看表结构：列名 / 类型 / 可空 / 说明", "example": "%desc demo.retail.fact_orders"},
        {"name": "%run", "usage": "%run <notebook>", "category": "流程复用",
         "desc": "执行另一个 notebook（按名称或 id），复用于流水线", "example": "%run daily_gmv"},
        {"name": "%load", "usage": "%load <文件路径>", "category": "流程复用",
         "desc": "载入文件内容到输出（相对 notebook 工作目录）", "example": "%load feature_eng.py"},
        {"name": "%param", "usage": "%param k=v [k2=v2]", "category": "参数",
         "desc": "设置 notebook 参数并持久化，供 {{k}} 引用", "example": "%param start_date=2026-11-01 n=10"},
        {"name": "%time", "usage": "%time <语句>", "category": "调试",
         "desc": "计时执行一条 SQL / Python / 魔法命令", "example": "%time %sql SELECT count(*) FROM demo.retail.fact_orders"},
        {"name": "%chart", "usage": "%chart [line|bar|pie|kpi|table]", "category": "可视化",
         "desc": "把上一次查询结果渲染成图表，可强制图表类型", "example": "%chart bar"},
        {"name": "%vars", "usage": "%vars", "category": "调试",
         "desc": "查看当前 Python 命名空间里的变量与类型", "example": "%vars"},
        {"name": "%help", "usage": "%help", "category": "帮助",
         "desc": "列出全部魔法命令", "example": "%help"},
        {"name": "%clear", "usage": "%clear", "category": "调试",
         "desc": "清空当前 notebook 的 Python 变量与参数", "example": "%clear"},
    ]


MAGIC_NAMES = [m["name"] for m in magic_help()]

MAGIC_RE = re.compile(r"^%([A-Za-z_][A-Za-z0-9_]*)\b[ \t]*(.*)$")


def _split_magic(source: str) -> tuple[str, str] | None:
    text = (source or "").lstrip("\ufeff")
    lines = text.split("\n")
    if not lines:
        return None
    head = lines[0].strip()
    match = MAGIC_RE.match(head)
    if not match:
        return None
    name = match.group(1).lower()
    if f"%{name}" not in MAGIC_NAMES:
        return None
    arg = match.group(2).strip()
    rest = "\n".join(lines[1:])
    body = (arg + ("\n" + rest if rest.strip() else "")).strip() if (arg or rest.strip()) else ""
    return name, body


def _parse_params_arg(body: str) -> dict:
    """解析 ``k=v k2='hello world'`` 形式。"""
    out: dict[str, Any] = {}
    for token in re.findall(r"""[A-Za-z_][\w.]*=(?:'[^']*'|"[^"]*"|\S+)""", body or ""):
        key, _, raw = token.partition("=")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            out[key] = raw[1:-1]
            continue
        lowered = raw.lower()
        if lowered in ("true", "false"):
            out[key] = lowered == "true"
            continue
        if lowered in ("none", "null"):
            out[key] = None
            continue
        try:
            out[key] = int(raw)
            continue
        except ValueError:
            pass
        try:
            out[key] = float(raw)
            continue
        except ValueError:
            pass
        out[key] = raw
    return out


def _magic_tables(notebook: dict, body: str) -> dict:
    result = _empty_result("sql")
    pattern = (body or "").strip().strip('"').strip("'")
    tables = catalog.all_tables(notebook.get("workspace_id") or "")
    if pattern:
        lowered = pattern.lower()
        tables = [
            t for t in tables
            if fnmatch.fnmatch(t["name"].lower(), lowered)
            or fnmatch.fnmatch((t.get("ref") or "").lower(), lowered)
            or lowered in (t["name"] or "").lower()
            or lowered in (t.get("ref") or "").lower()
        ]
    result["columns"] = ["表名", "类型", "行数", "说明"]
    result["rows"] = [
        [t.get("ref") or t["name"], t.get("kind") or "table", t.get("row_count") or 0,
         t.get("description") or ""]
        for t in tables
    ]
    result["row_count"] = len(result["rows"])
    result["output"] = (
        f"共 {len(tables)} 张表" + (f"（过滤：{pattern}）" if pattern else "")
        if tables
        else "当前工作空间还没有表，先到「数据接入」导入数据"
    )
    return result


def _magic_desc(notebook: dict, body: str) -> dict:
    result = _empty_result("sql")
    ref = (body or "").strip().strip('"').strip("'")
    if not ref:
        result.update({"ok": False, "error": "%desc 需要表名，例如 %desc demo.retail.fact_orders"})
        return result
    detail = catalog.table_detail(notebook.get("workspace_id") or "", ref)
    if not detail:
        result.update({"ok": False, "error": f"表不存在：{ref}"})
        return result
    result["columns"] = ["列名", "类型", "可空", "说明"]
    result["rows"] = [
        [c.get("name"), c.get("data_type") or "", "是" if c.get("is_nullable") else "否",
         c.get("description") or ""]
        for c in (detail.get("columns") or [])
    ]
    result["row_count"] = len(result["rows"])
    result["output"] = f"{detail.get('ref')} · {len(result['rows'])} 列 · 约 {detail.get('row_count') or 0} 行"
    return result


def _magic_vars(notebook: dict, body: str) -> dict:
    result = _empty_result("sql")
    ns = namespace_of(notebook)
    names = [
        k for k in ns
        if not k.startswith("_") and k not in HELPER_NAMES
    ]
    result["columns"] = ["变量", "类型", "值"]
    rows = []
    for name in sorted(names):
        value = ns[name]
        rows.append([name, type(value).__name__, _short(value, 120)])
    result["rows"] = rows
    result["row_count"] = len(rows)
    result["output"] = f"当前命名空间有 {len(rows)} 个变量" if rows else "当前命名空间还没有变量"
    return result


def _magic_chart(notebook: dict, body: str) -> dict:
    result = _empty_result("sql")
    nb_id = notebook.get("id") or ""
    last = _LAST_RESULT.get(nb_id)
    if not last or not last.get("columns"):
        result.update({"ok": False, "error": "还没有可渲染的结果，先跑一条 SQL 或 Python cell"})
        return result
    columns = last.get("columns") or []
    rows = last.get("rows") or []
    spec = charts.infer({"columns": columns, "rows": rows})
    forced = (body or "").strip().lower()
    if forced in ("line", "bar", "pie", "kpi", "table"):
        if forced == "table":
            spec = {"type": "table"}
        elif forced == "kpi":
            numeric = [
                i for i in range(len(columns))
                if any(isinstance(r[i], (int, float)) and not isinstance(r[i], bool)
                       for r in rows[:20] if i < len(r))
            ]
            spec = {"type": "kpi", "value_index": numeric[0] if numeric else 0,
                    "label": columns[numeric[0]] if numeric else (columns[0] if columns else "")}
        else:
            numeric = [
                i for i in range(len(columns))
                if any(isinstance(r[i], (int, float)) and not isinstance(r[i], bool)
                       for r in rows[:20] if i < len(r))
            ]
            x = 0 if 0 not in numeric else (1 if len(numeric) > 1 else 0)
            y = next((i for i in numeric if i != x), numeric[0] if numeric else 1)
            if len(columns) < 2 or not numeric:
                spec = {"type": "table"}
            else:
                spec = {"type": forced, "x": x, "y": y,
                        "x_label": columns[x], "y_label": columns[y]}
    result["columns"] = columns
    result["rows"] = rows
    result["row_count"] = len(rows)
    result["chart"] = spec
    note = ""
    if forced in ("line", "bar", "pie", "kpi", "table") and spec.get("type") != forced:
        note = f"（数据不适合 {forced}，已回退为 {spec.get('type')}）"
    result["output"] = f"图表类型：{spec.get('type')}{note} · {len(rows)} 个数据点"
    return result


def _magic_load(notebook: dict, body: str) -> dict:
    result = _empty_result("markdown")
    raw = (body or "").strip().strip('"').strip("'")
    if not raw:
        result.update({"ok": False, "error": "%load 需要文件路径，例如 %load feature_eng.py"})
        return result
    workdir = workdir_of(notebook)
    candidates = []
    path = Path(raw).expanduser()
    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.extend([workdir / raw, config.ROOT / raw])
    target = next((p for p in candidates if p.exists() and p.is_file()), None)
    if not target:
        result.update(
            {"ok": False, "error": f"文件不存在：{raw}（已尝试 {', '.join(str(p) for p in candidates)}）"}
        )
        return result
    try:
        size = target.stat().st_size
        if size > 2 * 1024 * 1024:
            result.update({"ok": False, "error": f"文件过大（{size} 字节），%load 仅支持 2MB 以内"})
            return result
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result.update({"ok": False, "error": f"读取失败：{exc}"})
        return result
    result["output"] = text
    result["source"] = str(target)
    result["output_meta"] = {"file": str(target), "size": size, "lines": len(text.splitlines())}
    return result


def _magic_param(notebook: dict, body: str) -> dict:
    result = _empty_result("sql")
    parsed = _parse_params_arg(body)
    if not parsed:
        current = params_of(notebook)
        result["columns"] = ["参数", "值"]
        result["rows"] = [[k, _short(v)] for k, v in current.items()]
        result["row_count"] = len(result["rows"])
        result["output"] = f"当前参数 {len(current)} 个" if current else "还没有参数，用 %param 名称=值 设置"
        return result
    nb_id = notebook.get("id") or ""
    merged = dict(_json_load(notebook.get("params"), {}))
    merged.update(parsed)
    store.update(
        "notebooks",
        nb_id,
        {"params": _json_dump(merged), "updated_at": store.now()},
    )
    _RUNTIME_PARAMS.setdefault(nb_id, {}).update(parsed)
    notebook["params"] = merged
    result["columns"] = ["参数", "值"]
    result["rows"] = [[k, _short(v)] for k, v in merged.items()]
    result["row_count"] = len(result["rows"])
    result["output"] = "已设置参数：" + "、".join(f"{k}={v}" for k, v in parsed.items())
    return result


def _magic_clear(notebook: dict, body: str) -> dict:
    result = _empty_result("sql")
    nb_id = notebook.get("id") or ""
    reset_namespace(nb_id)
    store.update("notebooks", nb_id, {"params": "{}", "updated_at": store.now()})
    result["output"] = "已清空 Python 变量与参数"
    return result


def _magic_help(notebook: dict, body: str) -> dict:
    result = _empty_result("markdown")
    lines = ["# 魔法命令", ""]
    for item in magic_help():
        lines.append(f"- **{item['usage']}** —— {item['desc']}")
    result["output"] = "\n".join(lines)
    result["magics"] = magic_help()
    return result


def _magic_run(notebook: dict, body: str, params: dict, depth: int) -> dict:
    result = _empty_result("sql")
    name = (body or "").strip().strip('"').strip("'")
    if not name:
        result.update({"ok": False, "error": "%run 需要 notebook 名称，例如 %run daily_gmv"})
        return result
    target = find_notebook(notebook.get("workspace_id") or "", name)
    if not target:
        result.update({"ok": False, "error": f"Notebook 不存在：{name}"})
        return result
    if target["id"] == notebook.get("id"):
        result.update({"ok": False, "error": "不能 %run 自己（会无限递归）"})
        return result
    if depth >= 4:
        result.update({"ok": False, "error": "notebook 嵌套执行超过 4 层，已中止"})
        return result
    out = run_all(target["id"], params=params, depth=depth + 1)
    ok = sum(1 for r in out["results"] if not r.get("error"))
    bad = len(out["results"]) - ok
    result["columns"] = ["Cell", "类型", "状态", "耗时(ms)", "输出摘要"]
    result["rows"] = [
        [i + 1, r.get("type") or "", "失败" if r.get("error") else "成功",
         r.get("elapsed_ms") or 0,
         (r.get("error") or r.get("output") or "")[:80]]
        for i, r in enumerate(out["results"])
    ]
    result["row_count"] = len(result["rows"])
    result["ok"] = bad == 0
    if bad:
        result["error"] = f"被调用的 notebook 有 {bad} 个 cell 失败"
    result["output"] = f"已执行「{target['name']}」：{len(out['results'])} 个 cell，成功 {ok} / 失败 {bad}"
    result["nested"] = {"notebook_id": target["id"], "name": target["name"], "ok": ok, "failed": bad}
    return result


def _dispatch_magic(
    notebook: dict, source: str, params: dict, cell: dict, depth: int
) -> dict:
    parsed = _split_magic(source)
    if not parsed:
        return {}
    name, body = parsed
    started = time.perf_counter()
    if name == "sql":
        result = _run_sql(notebook, body, params) if body else _empty_result("sql")
        result["magic"] = "%sql"
    elif name == "python":
        result = _run_python(notebook, body, params) if body else _empty_result("python")
        result["magic"] = "%python"
    elif name == "md":
        result = _empty_result("markdown")
        result["output"] = body
        result["magic"] = "%md"
    elif name == "tables":
        result = _magic_tables(notebook, body)
        result["magic"] = "%tables"
    elif name == "desc":
        result = _magic_desc(notebook, body)
        result["magic"] = "%desc"
    elif name == "vars":
        result = _magic_vars(notebook, body)
        result["magic"] = "%vars"
    elif name == "chart":
        result = _magic_chart(notebook, body)
        result["magic"] = "%chart"
    elif name == "load":
        result = _magic_load(notebook, body)
        result["magic"] = "%load"
    elif name == "param":
        result = _magic_param(notebook, body)
        result["magic"] = "%param"
    elif name == "clear":
        result = _magic_clear(notebook, body)
        result["magic"] = "%clear"
    elif name == "help":
        result = _magic_help(notebook, body)
        result["magic"] = "%help"
    elif name == "run":
        result = _magic_run(notebook, body, params, depth)
        result["magic"] = "%run"
    elif name == "time":
        inner_started = time.perf_counter()
        if not body:
            result = _empty_result("sql")
            result["output"] = "耗时 0.00 ms"
        elif body.lstrip().startswith("%"):
            result = _dispatch_magic(notebook, body, params, cell, depth) or _empty_result("sql")
        elif _looks_like_sql(body):
            result = _run_sql(notebook, body, params)
        else:
            result = _run_python(notebook, body, params)
        cost = (time.perf_counter() - inner_started) * 1000
        result["elapsed_ms"] = int(round(cost))
        header = f"⏱ 耗时 {cost:.2f} ms"
        result["output"] = header + ("\n" + result["output"] if result.get("output") else "")
        result["magic"] = "%time"
        return result
    else:
        result = _empty_result("sql")
        result.update({"ok": False, "error": f"未知魔法命令：%{name}"})
        result["magic"] = f"%{name}"
    if not result.get("elapsed_ms"):
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return result


# -------------------------------------------------------------------- 对外入口


def execute_cell(
    notebook_id: str,
    cell: dict,
    params: dict | None = None,
    depth: int = 0,
    persist: bool = True,
) -> dict:
    """执行单个 cell。

    返回 ``{output, columns, rows, chart, elapsed_ms, error, ...}``，
    同时把结果回写到 notebook 的 cell 上（persist=True）。
    """
    ensure_table()
    notebook = get_notebook(notebook_id)
    if not notebook:
        raise NotebookError("Notebook 不存在")
    cell = dict(cell or {})
    cell_id = cell.get("id") or ""
    source = cell.get("source") or ""
    cell_type = str(cell.get("type") or "sql").lower()
    merged = params_of(notebook)
    if params:
        merged.update(params)

    started = time.perf_counter()
    result: dict
    if not source.strip():
        result = _empty_result(cell_type, cell_id)
        result["output"] = ""
        result["ok"] = False
        result["error"] = "cell 内容为空"
    else:
        magic = _split_magic(source)
        if magic:
            result = _dispatch_magic(notebook, source, merged, cell, depth)
            result["cell_id"] = cell_id
        elif cell_type == "markdown":
            result = _empty_result("markdown", cell_id)
            result["output"] = source
        elif cell_type == "python":
            result = _run_python(notebook, source, merged)
            result["cell_id"] = cell_id
        else:
            result = _run_sql(notebook, source, merged)
            result["cell_id"] = cell_id
    if result.get("magic") == "%time":
        result.setdefault("elapsed_ms", int((time.perf_counter() - started) * 1000))
    elif not result.get("elapsed_ms"):
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    result.setdefault("type", cell_type)
    result["ok"] = not result.get("error")

    if persist and cell_id:
        try:
            _persist_cell_result(notebook_id, cell_id, result)
        except Exception:  # noqa: BLE001 —— 回写失败不影响执行结果
            pass
    return result


def run_all(notebook_id: str, params: dict | None = None, depth: int = 0) -> dict:
    """顺序执行所有 cell（markdown 直接渲染，不打断后续）。"""
    ensure_table()
    notebook = get_notebook(notebook_id)
    if not notebook:
        raise NotebookError("Notebook 不存在")
    if depth >= 4:
        raise NotebookError("notebook 嵌套执行超过 4 层，已中止")
    started = time.perf_counter()
    results: list[dict] = []
    for cell in list(notebook["cells"]):
        if (cell.get("type") or "") == "markdown":
            res = _empty_result("markdown", cell.get("id") or "")
            res["output"] = cell.get("source") or ""
            results.append(res)
            continue
        res = execute_cell(notebook_id, cell, params=params, depth=depth, persist=True)
        results.append(res)
    total_ms = int((time.perf_counter() - started) * 1000)
    fresh = get_notebook(notebook_id) or notebook
    return {
        "notebook": fresh,
        "results": results,
        "ok": all(not r.get("error") for r in results),
        "failed": sum(1 for r in results if r.get("error")),
        "elapsed_ms": total_ms,
    }


def explain_sql(sql: str, params: dict | None = None) -> dict:
    """参数化 SQL 解释：只做渲染 + 安全校验 + 引用表解析，不执行。"""
    original = sql or ""
    warnings: list[str] = []
    rendered = original
    used: dict[str, Any] = {}
    missing: list[str] = []
    for match in PARAM_RE.finditer(original):
        key = match.group(1)
        if params and key in params:
            used[key] = params[key]
        else:
            missing.append(key)
    try:
        rendered = render_params(original, params or {})
    except NotebookError:
        pass
    guarded = ""
    ok = True
    error = ""
    try:
        guarded = warehouse.guard(rendered)
    except warehouse.SqlRejected as exc:
        ok = False
        error = str(exc)
    if missing:
        warnings.append("未提供值的参数：" + "、".join(sorted(set(missing))))
    refs: list[str] = []
    if guarded:
        for m in re.finditer(
            r"(?i)\b(?:from|join)\s+([A-Za-z_][\w.]*(?:\.[A-Za-z_][\w.]*){0,2})", guarded
        ):
            ref = m.group(1)
            if ref.lower() not in ("select",) and ref not in refs:
                refs.append(ref)
    if not refs:
        warnings.append("未能从 SQL 中解析出引用表")
    return {
        "ok": ok,
        "error": error,
        "original": original,
        "rendered": rendered,
        "guarded": guarded,
        "params": used,
        "missing_params": sorted(set(missing)),
        "tables": refs,
        "warnings": warnings,
        "lines": len(original.splitlines()),
        "hints": [
            "只读查询：仅允许 SELECT / WITH / DESCRIBE / SHOW / EXPLAIN 等",
            "参数化写法：WHERE order_date >= '{{start_date}}'",
            "一次只允许一条语句，且不支持 SQL 注释",
        ],
    }


def notebook_summary(workspace_id: str) -> dict:
    """工作台概览：notebook 数、cell 数、最近更新时间。"""
    ensure_table()
    notebooks = list_notebooks(workspace_id)
    total_cells = sum(n["cell_count"] for n in notebooks)
    return {
        "notebooks": len(notebooks),
        "cells": total_cells,
        "magics": len(magic_help()),
        "latest": notebooks[0]["updated_at"] if notebooks else "",
    }
