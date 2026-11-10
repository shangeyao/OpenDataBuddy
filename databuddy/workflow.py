"""数据工程：工作流引擎 v2。

能力对齐「数据工程 > 工作流开发」：
* 9 种节点类型（sql / python / shell / notebook / quality / http / subflow / notify / approval），
  每种都有独立 executor，统一返回 ``{ok, output, rows?, error?}``。
* 12 种运行条件，决定节点是否执行；不满足时标记 skipped，下游按自身条件继续。
* 两层参数：工作流级 params（默认值）+ 节点级 params（覆盖、可引用上游输出）。
* 模板替换：``{{workflow.xxx}}`` / ``{{params.xxx}}`` / ``{{nodes.xxx.output}}``。
* DAG：拓扑排序 + 环检测 + 条件跳过。
* run_flow 逐节点写 ``flow_node_runs``，方便前端展示节点级明细。
* 调度：cron 支持时区；continuous（持续运行）模式最小间隔 60s。

本模块不改动 store.py：flows 表缺失的扩展列（params / run_mode / interval_seconds /
timezone）由 ``ensure_columns()`` 用 ALTER TABLE 幂等补齐，失败也不影响主流程。
"""
from __future__ import annotations

import builtins
import contextlib
import io
import json
import math
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from . import catalog, store, warehouse

# ------------------------------------------------------------------ 常量

MAX_OUTPUT_CHARS = 4000
MAX_DEPTH = 5
MIN_CONTINUOUS_SECONDS = 60
DEFAULT_SHELL_TIMEOUT = 60
DEFAULT_PYTHON_TIMEOUT = 30

_FLOW_EXTRA_COLUMNS = {
    "params": "TEXT DEFAULT '{}'",
    "run_mode": "TEXT DEFAULT 'cron'",
    "interval_seconds": "INTEGER DEFAULT 60",
    "timezone": "TEXT DEFAULT ''",
}

_running: set[str] = set()
_running_lock = threading.Lock()
_columns_ready = False


# ------------------------------------------------------------------ 通用工具


def ensure_columns(force: bool = False) -> None:
    """给 flows 表补齐工作流引擎需要的扩展列（幂等、容错）。"""
    global _columns_ready
    if _columns_ready and not force:
        return
    try:
        existing = {r["name"] for r in store.rows("PRAGMA table_info(flows)")}
    except Exception:  # noqa: BLE001
        return
    if not existing:
        return
    for name, decl in _FLOW_EXTRA_COLUMNS.items():
        if name in existing:
            continue
        try:
            store.execute(f'ALTER TABLE flows ADD COLUMN "{name}" {decl}')
        except Exception:  # noqa: BLE001
            pass
    _columns_ready = True


def _json_load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _truncate(text: Any, limit: int = MAX_OUTPUT_CHARS) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[:limit] + "…（已截断）"


def _json_safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _truncate(value)
    try:
        return _truncate(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return _truncate(value)


def _result(ok: bool, output: Any = "", rows: int | None = None, error: Any = None, **extra: Any) -> dict:
    """节点统一返回结构：{ok, output, rows?, error?}。"""
    out = {
        "ok": bool(ok),
        "output": _truncate(output),
        "rows": int(rows) if isinstance(rows, (int, float)) else rows,
        "error": None if not error else _truncate(error),
    }
    out.update(extra)
    return out


def _safe_key(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "_", str(text or ""))


# ------------------------------------------------------------------ 参数与模板


_MISS = object()
_TPL_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _lookup(scope: dict, path: str) -> Any:
    cur: Any = scope
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, (list, tuple)) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISS
    return cur


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def render_template(text: Any, scope: dict | None = None) -> Any:
    """把 ``{{a.b.c}}`` 替换成 scope 里的值；支持 ``{{a.b | default: 'x'}}``。

    * 未命中的变量替换为空串（不抛异常，避免模板写错导致整个工作流挂掉）。
    * dict / list 递归处理；非字符串原样返回。
    """
    if isinstance(text, dict):
        return {k: render_template(v, scope) for k, v in text.items()}
    if isinstance(text, (list, tuple)):
        return [render_template(v, scope) for v in text]
    if not isinstance(text, str) or "{{" not in text:
        return text
    scope = scope or {}

    def _replace(match: re.Match) -> str:
        raw = match.group(1).strip()
        default = ""
        for sep in ("|", "||"):
            if sep in raw:
                head, _, tail = raw.partition(sep)
                raw = head.strip()
                tail = tail.strip()
                if tail.lower().startswith("default"):
                    tail = tail[len("default"):].strip()
                    if tail[:1] in (":", "="):
                        tail = tail[1:].strip()
                default = tail.strip().strip("'\"")
                break
        value = _lookup(scope, raw)
        if value is _MISS:
            return default
        return _stringify(value)

    return _TPL_RE.sub(_replace, text)


def build_scope(
    flow: dict,
    workflow_params: dict | None = None,
    run_id: str = "",
    trigger: str = "manual",
    node_results: dict | None = None,
) -> dict:
    """构造模板作用域：workflow / params / nodes / env。"""
    defaults = _json_load(flow.get("params") if isinstance(flow, dict) else None, {}) or {}
    if not isinstance(defaults, dict):
        defaults = {}
    merged = {**defaults, **(workflow_params or {})}
    now = datetime.now()
    wf = {
        **merged,
        "id": flow.get("id", ""),
        "name": flow.get("name", ""),
        "description": flow.get("description", ""),
        "run_id": run_id,
        "trigger": trigger,
        "date": now.strftime("%Y-%m-%d"),
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "now": now.isoformat(timespec="seconds"),
        "timestamp": int(now.timestamp()),
        "yyyy": now.strftime("%Y"),
        "mm": now.strftime("%m"),
        "dd": now.strftime("%d"),
    }
    nodes: dict[str, dict] = {}
    for nid, entry in (node_results or {}).items():
        payload = {
            "status": entry.get("status", ""),
            "ok": entry.get("ok", entry.get("status") == "success"),
            "output": entry.get("output", ""),
            "rows": entry.get("rows"),
            "error": entry.get("error") or "",
            "node_id": nid,
        }
        nodes[nid] = payload
        name = entry.get("name")
        if name and name not in nodes:
            nodes[name] = payload
        safe = _safe_key(name or nid)
        if safe and safe not in nodes:
            nodes[safe] = payload
    return {
        "workflow": wf,
        "params": merged,
        "nodes": nodes,
        "env": {
            "run_id": run_id,
            "trigger": trigger,
            "flow_id": flow.get("id", ""),
            "flow_name": flow.get("name", ""),
            "date": wf["date"],
        },
    }


# ------------------------------------------------------------------ 时区 / cron


_TZ_OFFSETS = {
    "utc": 0, "etc/utc": 0, "gmt": 0, "z": 0,
    "asia/shanghai": 8, "asia/chongqing": 8, "asia/hong_kong": 8,
    "asia/taipei": 8, "asia/singapore": 8, "asia/macau": 8,
    "asia/tokyo": 9, "asia/seoul": 9,
    "asia/kolkata": 5.5, "asia/dubai": 4, "asia/bangkok": 7,
    "europe/london": 0, "europe/paris": 1, "europe/berlin": 1,
    "europe/moscow": 3, "america/new_york": -5, "america/chicago": -6,
    "america/denver": -7, "america/los_angeles": -8, "america/sao_paulo": -3,
    "australia/sydney": 10, "pacific/auckland": 12,
}


def resolve_tz(name: str | None):
    """解析时区名；标准库 zoneinfo 不可用（缺 tzdata）时退回固定偏移表。"""
    text = (name or "").strip()
    if not text:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(text)
    except Exception:  # noqa: BLE001 - 缺 tzdata / 非法时区名都走降级
        pass
    key = text.lower().replace(" ", "")
    if key in _TZ_OFFSETS:
        return dt_timezone(timedelta(hours=_TZ_OFFSETS[key]))
    m = re.fullmatch(r"(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", key)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        return dt_timezone(sign * timedelta(hours=hours, minutes=minutes))
    return None


def to_timezone(when: datetime, tz_name: str | None) -> datetime:
    tz = resolve_tz(tz_name)
    if tz is None:
        return when
    if when.tzinfo is None:
        return when.astimezone(tz)
    return when.astimezone(tz)


def _field_match(field: str, value: int, lo: int, hi: int) -> bool:
    for part in (field or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                step = int(part[2:] or 1)
            except ValueError:
                continue
            if step > 0 and (value - lo) % step == 0:
                return True
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                if int(a) <= value <= int(b):
                    return True
            except ValueError:
                continue
            continue
        try:
            if int(part) == value:
                return True
        except ValueError:
            continue
    return False


def cron_matches(expr: str, when: datetime | None = None, timezone: str | None = None) -> bool:
    """5 段 cron 匹配，支持时区参数（缺 tzdata 时按固定偏移降级）。"""
    when = when or datetime.now()
    when = to_timezone(when, timezone)
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    return (
        _field_match(minute, when.minute, 0, 59)
        and _field_match(hour, when.hour, 0, 23)
        and _field_match(dom, when.day, 1, 31)
        and _field_match(month, when.month, 1, 12)
        and _field_match(dow, (when.weekday() + 1) % 7, 0, 6)
    )


def describe_cron(expr: str) -> str:
    parts = (expr or "").split()
    if len(parts) != 5:
        return "未配置调度"
    m, h, dom, mon, dow = parts
    if m.isdigit() and h.isdigit() and dom == "*" and mon == "*" and dow == "*":
        return f"每天 {int(h):02d}:{int(m):02d}"
    if m.startswith("*/") and h == "*":
        return f"每 {m[2:]} 分钟"
    if m.isdigit() and h == "*":
        return f"每小时第 {int(m)} 分钟"
    if h.isdigit() and dom == "*" and mon == "*" and dow.isdigit():
        names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
        return f"每{names[int(dow) % 7]} {int(h):02d}:{int(m):02d}"
    if h.isdigit() and dow == "*" and mon == "*" and dom.isdigit():
        return f"每月 {int(dom)} 日 {int(h):02d}:{int(m):02d}"
    return expr


# ------------------------------------------------------------------ DAG


def topo_order(nodes: list[dict], edges: list[list[str]]) -> list[dict]:
    """拓扑排序；有环抛 ValueError。"""
    by_id = {n["id"]: n for n in nodes if isinstance(n, dict) and n.get("id")}
    indeg = {nid: 0 for nid in by_id}
    children: dict[str, list[str]] = {nid: [] for nid in by_id}
    for edge in edges or []:
        if not isinstance(edge, (list, tuple)) or len(edge) < 2:
            continue
        src, dst = str(edge[0]), str(edge[1])
        if src in indeg and dst in indeg:
            indeg[dst] += 1
            children[src].append(dst)
    queue = [nid for nid, d in indeg.items() if d == 0]
    ordered: list[dict] = []
    while queue:
        nid = queue.pop(0)
        ordered.append(by_id[nid])
        for child in children[nid]:
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)
    if len(ordered) != len(by_id):
        raise ValueError("工作流存在环，无法执行")
    if len(by_id) != len([n for n in nodes if isinstance(n, dict)]):
        raise ValueError("存在缺少 id 的节点")
    return ordered


def detect_cycle(nodes: list[dict], edges: list[list[str]]) -> bool:
    try:
        topo_order(nodes, edges)
        return False
    except ValueError:
        return True


def upstream_of(node_id: str, edges: list[list[str]]) -> list[str]:
    return [
        str(e[0])
        for e in (edges or [])
        if isinstance(e, (list, tuple)) and len(e) >= 2 and str(e[1]) == node_id
    ]


# ------------------------------------------------------------------ 条件（12 种）


def _ups(upstream: list[dict]) -> list[dict]:
    return [u for u in (upstream or []) if isinstance(u, dict)]


def _statuses(upstream: list[dict]) -> list[str]:
    return [str(u.get("status") or "") for u in _ups(upstream)]


def _cond_all_success(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    ups = _ups(upstream)
    return bool(ups) and all(s == "success" for s in _statuses(ups))


def _cond_any_success(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    return any(s == "success" for s in _statuses(upstream))


def _cond_any_failed(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    return any(s == "failed" for s in _statuses(upstream))


def _cond_all_failed(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    ups = _ups(upstream)
    return bool(ups) and all(s == "failed" for s in _statuses(ups))


def _cond_all_done(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    ups = _ups(upstream)
    return bool(ups) and all(s in ("success", "failed", "skipped") for s in _statuses(ups))


def _cond_has_output(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    return any(str(u.get("output") or "").strip() for u in _ups(upstream))


def _cond_expression(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    cfg = _condition_cfg(node)
    expr = str(cfg.get("expression") or cfg.get("expr") or "").strip()
    if not expr:
        return _cond_all_success(upstream, node, scope)
    ups = _ups(upstream)
    statuses = _statuses(ups)
    variables = {
        "success": statuses.count("success"),
        "failed": statuses.count("failed"),
        "skipped": statuses.count("skipped"),
        "waiting": statuses.count("waiting"),
        "total": len(ups),
        "rows_total": sum(int(u.get("rows") or 0) for u in ups if isinstance(u.get("rows"), (int, float))),
        "rows_max": max([int(u.get("rows") or 0) for u in ups] or [0]),
        "ok_all": all(s == "success" for s in statuses) if ups else True,
    }
    for u in ups:
        key = _safe_key(u.get("node_id") or "")
        if key:
            variables[f"n_{key}"] = 1 if u.get("status") == "success" else 0
    try:
        return bool(eval(expr, {"__builtins__": {}}, variables))  # noqa: S307 - 受限命名空间
    except Exception:  # noqa: BLE001
        return False


def _cond_time_window(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    cfg = _condition_cfg(node)
    start = str(cfg.get("start") or cfg.get("from") or "").strip()
    end = str(cfg.get("end") or cfg.get("to") or "").strip()
    days = cfg.get("days")
    now = datetime.now()
    tz = resolve_tz(str(cfg.get("timezone") or ""))
    if tz is not None:
        now = now.astimezone(tz)
    if isinstance(days, (list, tuple)) and days:
        # 1=周一 ... 7=周日
        if (now.isoweekday()) not in {int(d) for d in days if str(d).strip().isdigit()}:
            return False
    current = now.hour * 60 + now.minute

    def _minutes(text: str) -> int | None:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", text or "")
        if not m:
            return None
        return int(m.group(1)) * 60 + int(m.group(2))

    s = _minutes(start) if start else None
    e = _minutes(end) if end else None
    if s is None and e is None:
        return True
    if s is not None and e is not None:
        if s <= e:
            return s <= current <= e
        return current >= s or current <= e  # 跨零点窗口
    if s is not None:
        return current >= s
    return current <= (e or 0)


def _cond_row_threshold(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    cfg = _condition_cfg(node)
    total = sum(int(u.get("rows") or 0) for u in _ups(upstream) if isinstance(u.get("rows"), (int, float)))
    try:
        min_rows = cfg.get("min_rows", cfg.get("min"))
        max_rows = cfg.get("max_rows", cfg.get("max"))
        if min_rows not in (None, ""):
            if total < int(min_rows):
                return False
        if max_rows not in (None, ""):
            if total > int(max_rows):
                return False
    except (TypeError, ValueError):
        return True
    return True


def _cond_quality_passed(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    ups = _ups(upstream)
    quality = [u for u in ups if str(u.get("type") or "") == "quality"]
    if quality:
        return all(bool(u.get("ok")) for u in quality)
    return bool(ups) and all(s == "success" for s in _statuses(ups))


def _cond_manual_confirm(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    cfg = _condition_cfg(node)
    node_id = str((node or {}).get("id") or "")
    merged = (scope or {}).get("params") or {}
    flag_keys = [
        str(cfg.get("param") or ""),
        f"confirm_{node_id}" if node_id else "",
        f"confirm_{_safe_key(node_id)}" if node_id else "",
        "confirmed",
    ]
    for key in flag_keys:
        if not key:
            continue
        if str(merged.get(key, "")).lower() in ("1", "true", "yes", "y", "ok"):
            return True
    if cfg.get("approved") in (True, 1, "1", "true"):
        return True
    approval_id = str(cfg.get("approval_id") or "").strip()
    try:
        if approval_id:
            found = store.row("SELECT status FROM approvals WHERE id = ?", (approval_id,))
            return bool(found and found.get("status") == "approved")
        if node_id:
            found = store.row(
                "SELECT status FROM approvals WHERE status = 'approved' AND (reason LIKE ? OR args LIKE ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (f"%{node_id}%", f"%{node_id}%"),
            )
            return bool(found)
    except Exception:  # noqa: BLE001
        return False
    return False


def _cond_always(upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    return True


CONDITIONS: dict[str, dict] = {
    "all_success": {
        "label": "全部成功",
        "desc": "所有上游节点都成功才执行（默认）",
        "fn": _cond_all_success,
        "params": [],
    },
    "any_success": {
        "label": "任一成功",
        "desc": "只要有一个上游节点成功就执行",
        "fn": _cond_any_success,
        "params": [],
    },
    "any_failed": {
        "label": "任一失败",
        "desc": "任一上游失败时执行，常用于失败告警分支",
        "fn": _cond_any_failed,
        "params": [],
    },
    "all_failed": {
        "label": "全部失败",
        "desc": "所有上游都失败时执行",
        "fn": _cond_all_failed,
        "params": [],
    },
    "all_done": {
        "label": "全部结束",
        "desc": "上游都已结束（成功/失败/跳过）后执行",
        "fn": _cond_all_done,
        "params": [],
    },
    "has_output": {
        "label": "有输出",
        "desc": "任一上游产出了非空输出",
        "fn": _cond_has_output,
        "params": [],
    },
    "expression": {
        "label": "表达式",
        "desc": "自定义表达式，可用 success/failed/skipped/total/rows_total/n_节点ID",
        "fn": _cond_expression,
        "params": [{"key": "expression", "label": "表达式", "type": "text", "placeholder": "success >= 2 and rows_total > 0"}],
    },
    "time_window": {
        "label": "时间窗口",
        "desc": "仅在指定时间窗口内执行（支持跨零点与周几过滤）",
        "fn": _cond_time_window,
        "params": [
            {"key": "start", "label": "开始 HH:MM", "type": "text", "placeholder": "09:00"},
            {"key": "end", "label": "结束 HH:MM", "type": "text", "placeholder": "18:00"},
            {"key": "days", "label": "周几（1-7，逗号分隔）", "type": "text", "placeholder": "1,2,3,4,5"},
            {"key": "timezone", "label": "时区", "type": "text", "placeholder": "Asia/Shanghai"},
        ],
    },
    "row_threshold": {
        "label": "行数阈值",
        "desc": "上游行数合计落在阈值区间内才执行",
        "fn": _cond_row_threshold,
        "params": [
            {"key": "min_rows", "label": "最少行数", "type": "number"},
            {"key": "max_rows", "label": "最多行数", "type": "number"},
        ],
    },
    "quality_passed": {
        "label": "质量通过",
        "desc": "上游质量节点全部通过才执行",
        "fn": _cond_quality_passed,
        "params": [],
    },
    "manual_confirm": {
        "label": "人工确认",
        "desc": "需要人工审批通过（或运行参数 confirm_节点ID=true）",
        "fn": _cond_manual_confirm,
        "params": [
            {"key": "approval_id", "label": "审批单 ID（可选）", "type": "text"},
            {"key": "param", "label": "确认参数名（可选）", "type": "text", "placeholder": "confirm_publish"},
        ],
    },
    "always": {
        "label": "总是执行",
        "desc": "忽略上游状态，总是执行",
        "fn": _cond_always,
        "params": [],
    },
}


def _condition_cfg(node: dict | None) -> dict:
    node = node or {}
    cfg = node.get("condition_config")
    if not isinstance(cfg, dict):
        cfg = {}
    merged = {}
    node_cfg = node.get("config")
    if isinstance(node_cfg, dict):
        merged.update({k: v for k, v in node_cfg.items() if k in _CONDITION_CFG_KEYS})
    merged.update(cfg)
    return merged


_CONDITION_CFG_KEYS = {
    "expression", "expr", "start", "end", "from", "to", "days", "timezone",
    "min_rows", "max_rows", "min", "max", "approved", "approval_id", "param",
}


def evaluate_condition(name: str, upstream: list[dict], node: dict | None = None, scope: dict | None = None) -> bool:
    """评估运行条件；未知条件按 all_success 处理。根节点无上游时总是执行。"""
    ups = _ups(upstream)
    if not ups:
        return True
    spec = CONDITIONS.get(str(name or "").strip() or "all_success")
    if not spec:
        spec = CONDITIONS["all_success"]
    try:
        return bool(spec["fn"](ups, node, scope))
    except Exception:  # noqa: BLE001 - 条件写错不该炸整个工作流
        return False


def describe_condition(name: str) -> dict:
    spec = CONDITIONS.get(str(name or "").strip() or "all_success") or {
        "label": "未知条件",
        "desc": "",
        "params": [],
    }
    return {"key": name or "all_success", "label": spec["label"], "desc": spec["desc"], "params": spec.get("params", [])}


# ------------------------------------------------------------------ 节点执行器（9 种）


def _executor_ctx(
    workspace_id: str,
    flow: dict,
    run_id: str,
    node: dict,
    scope: dict,
    depth: int,
) -> dict:
    return {
        "workspace_id": workspace_id,
        "flow": flow,
        "run_id": run_id,
        "node": node,
        "scope": scope,
        "depth": depth,
        "config": node.get("config") if isinstance(node.get("config"), dict) else {},
        "node_id": str(node.get("id") or ""),
        "node_name": str(node.get("name") or node.get("id") or ""),
        "params": scope.get("params") or {},
    }


def _exec_sql(ctx: dict) -> dict:
    cfg = ctx["config"]
    sql = str(cfg.get("sql") or "").strip()
    if not sql:
        return _result(False, error="SQL 节点未配置 sql")
    limit = int(cfg.get("limit") or 200)
    res = catalog.run_workspace_sql(ctx["workspace_id"], sql, limit=limit)
    columns = res.get("columns") or []
    rows = res.get("rows") or []
    preview = {"columns": columns, "rows": rows[:20]}
    return _result(
        True,
        output=f"查询成功，返回 {res.get('row_count', len(rows))} 行（{res.get('elapsed_ms', 0)}ms）",
        rows=res.get("row_count", len(rows)),
        sql=res.get("sql", sql),
        preview=preview,
        elapsed_ms=res.get("elapsed_ms", 0),
    )


_SAFE_BUILTIN_NAMES = (
    "len", "range", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
    "min", "max", "sum", "abs", "round", "sorted", "enumerate", "zip", "map",
    "filter", "any", "all", "isinstance", "print", "repr", "format", "divmod",
    "pow", "reversed", "slice", "hash", "chr", "ord", "bytes", "type",
)


def _python_namespace(scope: dict) -> dict:
    safe = {name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES if hasattr(builtins, name)}
    return {
        "__builtins__": safe,
        "json": json,
        "math": math,
        "re": re,
        "datetime": datetime,
        "timedelta": timedelta,
        "workflow": scope.get("workflow") or {},
        "params": scope.get("params") or {},
        "nodes": scope.get("nodes") or {},
        "env": scope.get("env") or {},
        "result": None,
        "log": lambda *a: None,
    }


def _exec_python_code(code: str, namespace: dict, timeout: int) -> tuple[bool, str, str | None]:
    """在受限命名空间里 exec 代码，捕获 stdout；超时用线程兜底。"""
    buf = io.StringIO()
    holder: dict[str, Any] = {}

    def _work() -> None:
        try:
            with contextlib.redirect_stdout(buf):
                exec(compile(code, "<workflow-python>", "exec"), namespace)  # noqa: S102
        except BaseException as exc:  # noqa: BLE001 - 用户代码什么都可能抛
            holder["error"] = exc

    thread = threading.Thread(target=_work, daemon=True)
    thread.start()
    thread.join(max(1, int(timeout)))
    if thread.is_alive():
        return False, buf.getvalue(), f"Python 节点执行超时（>{timeout}s）"
    if "error" in holder:
        exc = holder["error"]
        return False, buf.getvalue(), f"{type(exc).__name__}: {exc}"
    return True, buf.getvalue(), None


def _exec_python(ctx: dict) -> dict:
    cfg = ctx["config"]
    code = str(cfg.get("code") or cfg.get("script") or "")
    if not code.strip():
        return _result(False, error="Python 节点未配置 code")
    timeout = int(cfg.get("timeout") or DEFAULT_PYTHON_TIMEOUT)
    namespace = _python_namespace(ctx["scope"])
    ok, stdout, error = _exec_python_code(code, namespace, timeout)
    if not ok:
        return _result(False, output=stdout, error=error)
    produced = namespace.get("result")
    parts = []
    if produced is not None:
        parts.append(_stringify(produced))
    if stdout.strip():
        parts.append(stdout.strip())
    output = "\n".join(parts) or "执行完成（无输出）"
    return _result(True, output=output, rows=cfg.get("rows"))


def _exec_shell(ctx: dict) -> dict:
    cfg = ctx["config"]
    command = str(cfg.get("command") or cfg.get("cmd") or "").strip()
    if not command:
        return _result(False, error="Shell 节点未配置 command")
    timeout = int(cfg.get("timeout") or DEFAULT_SHELL_TIMEOUT)
    try:
        proc = subprocess.run(  # noqa: S602 - 工作流节点本身就是要跑命令
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cfg.get("cwd") or None,
        )
    except subprocess.TimeoutExpired:
        return _result(False, error=f"Shell 命令执行超时（>{timeout}s）")
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return _result(False, output=stdout, error=stderr or f"退出码 {proc.returncode}")
    return _result(True, output=stdout or "执行成功（无输出）", rows=None)


def _exec_notebook(ctx: dict) -> dict:
    cfg = ctx["config"]
    cells = cfg.get("cells")
    if isinstance(cells, str):
        cells = [{"type": "code", "source": cells}]
    if not isinstance(cells, list) or not cells:
        return _result(False, error="Notebook 节点未配置 cells")
    namespace = _python_namespace(ctx["scope"])
    outputs: list[str] = []
    rows_total = 0
    for index, cell in enumerate(cells, start=1):
        if isinstance(cell, str):
            cell = {"type": "code", "source": cell}
        if not isinstance(cell, dict):
            continue
        source = str(cell.get("source") or cell.get("code") or "")
        if not source.strip():
            continue
        if str(cell.get("type") or "code") != "code":
            outputs.append(f"[cell {index} markdown] {source.strip()[:200]}")
            continue
        ok, stdout, error = _exec_python_code(source, namespace, int(cfg.get("timeout") or DEFAULT_PYTHON_TIMEOUT))
        if not ok:
            return _result(False, output="\n".join(outputs), error=f"cell {index} 失败：{error}")
        produced = namespace.get("result")
        piece = stdout.strip()
        if produced is not None:
            piece = (piece + "\n" + _stringify(produced)).strip()
        if piece:
            outputs.append(f"[cell {index}] {piece}")
        rows_total += 1
    return _result(True, output="\n".join(outputs) or "Notebook 执行完成（无输出）", rows=rows_total)


def _quality_rule_sql(rule: dict) -> str | None:
    """把 quality_rules 的一行翻译成一条断言 SQL（返回异常值，0 表示通过）。"""
    kind = str(rule.get("rule_type") or rule.get("type") or "").lower()
    column = rule.get("column_name") or rule.get("column")
    cfg = _json_load(rule.get("config"), {}) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    col = f'"{column}"' if column else ""
    if kind in ("null", "null_rate", "not_null"):
        if not col:
            return None
        return f"SELECT count(*) FROM {{tbl}} WHERE {col} IS NULL"
    if kind in ("unique", "uniqueness", "duplicate"):
        if not col:
            return None
        return f"SELECT count(*) - count(DISTINCT {col}) FROM {{tbl}}"
    if kind == "range":
        if not col:
            return None
        clauses = []
        if cfg.get("min") is not None:
            clauses.append(f"{col} < {float(cfg['min'])}")
        if cfg.get("max") is not None:
            clauses.append(f"{col} > {float(cfg['max'])}")
        if not clauses:
            return None
        return f"SELECT count(*) FROM {{tbl}} WHERE " + " OR ".join(clauses)
    if kind == "enum":
        if not col:
            return None
        values = cfg.get("values") or cfg.get("enum") or []
        if not values:
            return None
        quoted = ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)
        return f"SELECT count(*) FROM {{tbl}} WHERE {col} IS NOT NULL AND CAST({col} AS VARCHAR) NOT IN ({quoted})"
    if kind == "regex":
        if not col:
            return None
        pattern = str(cfg.get("pattern") or cfg.get("regex") or "")
        if not pattern:
            return None
        safe = pattern.replace("'", "''")
        return (
            f"SELECT count(*) FROM {{tbl}} WHERE {col} IS NOT NULL "
            f"AND NOT regexp_matches(CAST({col} AS VARCHAR), '{safe}')"
        )
    if kind == "freshness":
        target = col or str(cfg.get("column") or "")
        if not target:
            return None
        days = float(cfg.get("max_age_days") or cfg.get("days") or 1)
        return f"SELECT count(*) FROM {{tbl}} WHERE \"{target}\" < now() - INTERVAL {days} DAY"
    if kind in ("sql", "custom_sql"):
        sql = str(cfg.get("sql") or cfg.get("expression") or "").strip()
        return sql or None
    return None


def _reason_text(reason: Any) -> str:
    """把质量模块返回的 reason（dict 或 str）转成一行可读文本。"""
    if isinstance(reason, dict):
        kind = reason.get("rule_type") or reason.get("type") or "规则"
        column = reason.get("column_name") or reason.get("column") or ""
        value = reason.get("value")
        detail = reason.get("detail") or reason.get("message") or ""
        head = f"{kind}({column})" if column else str(kind)
        if value not in (None, ""):
            head += f" 值={value}"
        return f"{head}：{detail}" if detail else head
    return _stringify(reason)


def _quality_precheck(table_ref: str, workspace_id: str) -> tuple[dict | None, str]:
    """尝试调用 W1 质量模块的 precheck。返回 (verdict, 降级原因)。

    模块缺失、导入报错、签名不兼容、内部抛异常 —— 任何一种情况都返回 (None, 原因)，
    由调用方降级为直接读 quality_rules 做简单校验，绝不因此让工作流崩溃。
    """
    try:
        from . import quality  # type: ignore
    except ImportError as exc:
        return None, f"质量模块不可用（{exc}）"
    except Exception as exc:  # noqa: BLE001 - 模块导入期就炸也要降级
        return None, f"质量模块导入异常（{type(exc).__name__}: {exc}）"
    precheck = getattr(quality, "precheck", None)
    if not callable(precheck):
        return None, "质量模块缺少 precheck()"
    try:
        try:
            verdict = precheck(table_ref, workspace_id)
        except TypeError:
            verdict = precheck(table_ref)
    except Exception as exc:  # noqa: BLE001 - 质量模块内部异常不影响工作流主流程
        return None, f"质量模块调用异常（{type(exc).__name__}: {exc}）"
    if not isinstance(verdict, dict):
        return None, "质量模块返回了非预期结构"
    return verdict, ""


def _exec_quality(ctx: dict) -> dict:
    cfg = ctx["config"]
    table_ref = str(cfg.get("table_ref") or cfg.get("table") or "").strip()
    workspace_id = ctx["workspace_id"]

    # 优先走 W1 的质量模块；模块未就绪 / 异常时降级为直接读 quality_rules 跑简单校验
    fallback_reason = ""
    if table_ref:
        verdict, fallback_reason = _quality_precheck(table_ref, workspace_id)
        if verdict is not None:
            blocked = bool(verdict.get("blocked"))
            reasons = verdict.get("reasons") or []
            warnings = verdict.get("warnings") or []
            text = "；".join(_reason_text(r) for r in reasons) if reasons else "质量校验通过"
            warn_text = "；".join(_reason_text(r) for r in warnings) if warnings else ""
            if warn_text:
                text = f"{text}（告警：{warn_text}）"
            return _result(
                not blocked,
                output=f"{'已阻断' if blocked else '通过'}：{text}",
                rows=len(reasons) if reasons else 0,
                error=None if not blocked else text,
                blocked=blocked,
                reasons=[_reason_text(r) for r in reasons],
                warnings=[_reason_text(r) for r in warnings],
                checked=verdict.get("checked"),
                source="quality.precheck",
            )
    else:
        fallback_reason = "未配置 table_ref"

    # ---------------- 降级实现：直接读 quality_rules 表
    if not table_ref:
        return _result(False, error="质量节点未配置 table_ref")
    found = catalog.resolve(workspace_id, table_ref)
    if not found:
        return _result(False, error=f"表不存在：{table_ref}")
    physical = f'{warehouse.physical_name(found["catalog_name"], found["schema_name"])}."{found["name"]}"'
    rules = store.rows(
        "SELECT * FROM quality_rules WHERE workspace_id = ? AND table_ref = ? AND enabled = 1",
        (workspace_id, table_ref),
    )
    if not rules:
        rules = store.rows(
            "SELECT * FROM quality_rules WHERE table_ref = ? AND enabled = 1", (table_ref,)
        )
    blocking = cfg.get("blocking", True)
    failures: list[str] = []
    checked = 0
    for rule in rules:
        template = _quality_rule_sql(rule)
        if not template:
            continue
        sql = template.replace("{tbl}", physical)
        try:
            res = warehouse.query(sql, limit=5, check=False)
            value = res["rows"][0][0] if res.get("rows") and res["rows"][0] else 0
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{rule.get('id')} 执行异常：{exc}")
            checked += 1
            continue
        checked += 1
        if float(value or 0) != 0:
            failures.append(
                f"{rule.get('rule_type')}({rule.get('column_name') or '-'}) 异常值 {value}"
            )
    if not checked:
        return _result(
            True,
            output=f"表 {table_ref} 未配置质量规则，跳过校验"
            + (f"（{fallback_reason}，已降级）" if fallback_reason else ""),
            rows=0,
            source="quality_rules(fallback)",
            degraded=bool(fallback_reason),
            degrade_reason=fallback_reason,
        )
    ok = not failures or not blocking
    return _result(
        ok,
        output=("质量校验通过" if not failures else "质量校验未通过：" + "；".join(failures))
        + (f"（{fallback_reason}，已降级）" if fallback_reason else ""),
        rows=len(failures),
        error=None if ok else "；".join(failures),
        blocked=bool(failures) and bool(blocking),
        reasons=failures,
        checked=checked,
        source="quality_rules(fallback)",
        degraded=bool(fallback_reason),
        degrade_reason=fallback_reason,
    )


def _exec_http(ctx: dict) -> dict:
    import urllib.error
    import urllib.request

    cfg = ctx["config"]
    url = str(cfg.get("url") or "").strip()
    if not url:
        return _result(False, error="HTTP 节点未配置 url")
    method = str(cfg.get("method") or "GET").upper()
    headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else {}
    body = cfg.get("body")
    payload = None
    if body not in (None, ""):
        payload = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **headers}
    timeout = int(cfg.get("timeout") or 15)
    expect = cfg.get("expect_status")
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 工作流节点就是回调外部系统
            status = getattr(resp, "status", 200)
            text = resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        text = (exc.read(4096) or b"").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return _result(False, error=f"HTTP 请求失败：{type(exc).__name__}: {exc}")
    ok = int(status) < 400
    if expect is not None and str(status) != str(expect):
        ok = False
    return _result(
        ok,
        output=f"{method} {url} -> {status}" + (f"\n{text[:500]}" if text else ""),
        error=None if ok else f"HTTP 状态码 {status}",
        status_code=status,
    )


def _exec_subflow(ctx: dict) -> dict:
    cfg = ctx["config"]
    target = str(cfg.get("flow_id") or cfg.get("flow") or "").strip()
    if not target:
        return _result(False, error="子流程节点未配置 flow_id")
    if ctx["depth"] >= MAX_DEPTH:
        return _result(False, error=f"子流程嵌套超过 {MAX_DEPTH} 层，已终止")
    child = store.row("SELECT * FROM flows WHERE id = ?", (target,))
    if not child:
        child = store.row(
            "SELECT * FROM flows WHERE name = ? AND workspace_id = ?",
            (target, ctx["workspace_id"]),
        )
    if not child:
        return _result(False, error=f"子流程不存在：{target}")
    if child["id"] == ctx["flow"].get("id"):
        return _result(False, error="子流程不能调用自身")
    child_params = cfg.get("params") if isinstance(cfg.get("params"), dict) else {}
    outcome = run_flow(
        child,
        trigger="subflow",
        params={**(ctx["params"] or {}), **child_params},
        workspace_id=ctx["workspace_id"],
        _depth=ctx["depth"] + 1,
    )
    ok = outcome.get("status") in ("success", "waiting")
    return _result(
        ok,
        output=f"子流程「{child.get('name')}」执行{ '成功' if ok else '失败' }（{outcome.get('status')}）",
        rows=outcome.get("node_count"),
        error=None if ok else f"子流程状态 {outcome.get('status')}",
        child_run_id=outcome.get("id"),
        child_status=outcome.get("status"),
    )


def _exec_notify(ctx: dict) -> dict:
    cfg = ctx["config"]
    channels = cfg.get("channels")
    if isinstance(channels, str):
        channels = [c.strip() for c in channels.replace("，", ",").split(",") if c.strip()]
    if not channels:
        channels = ["console"]
    title = str(cfg.get("title") or f"工作流 {ctx['flow'].get('name', '')} 通知")
    message = str(cfg.get("message") or cfg.get("content") or "")
    detail = {
        "channels": channels,
        "title": title,
        "message": message,
        "flow_id": ctx["flow"].get("id"),
        "run_id": ctx["run_id"],
        "node": ctx["node_id"],
    }
    try:
        store.audit(
            ctx["workspace_id"],
            None,
            "workflow_notify",
            target=f"{ctx['flow'].get('name', '')}/{ctx['node_name']}",
            detail=detail,
        )
    except Exception:  # noqa: BLE001 - 审计写失败不该阻断通知节点
        pass
    return _result(
        True,
        output=f"已通知渠道：{', '.join(str(c) for c in channels)}\n标题：{title}"
        + (f"\n内容：{message}" if message else ""),
        rows=len(channels),
        channels=channels,
        delivered=False,
    )


def _exec_approval(ctx: dict) -> dict:
    cfg = ctx["config"]
    reason = str(cfg.get("reason") or cfg.get("title") or f"节点「{ctx['node_name']}」等待人工审批")
    approvers = cfg.get("approvers")
    if isinstance(approvers, str):
        approvers = [a.strip() for a in approvers.replace("，", ",").split(",") if a.strip()]
    approval_id = store.new_id("ap")
    try:
        store.insert(
            "approvals",
            {
                "id": approval_id,
                "conversation_id": None,
                "tool": "workflow_approval",
                "args": json.dumps(
                    {
                        "flow_id": ctx["flow"].get("id"),
                        "run_id": ctx["run_id"],
                        "node_id": ctx["node_id"],
                        "node_name": ctx["node_name"],
                        "approvers": approvers or [],
                        "params": ctx["params"],
                    },
                    ensure_ascii=False,
                ),
                "reason": reason,
                "status": "pending",
                "created_at": store.now(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _result(False, error=f"创建审批单失败：{exc}")
    return _result(
        True,
        output=f"等待人工审批（审批单 {approval_id}）：{reason}",
        rows=0,
        waiting=True,
        approval_id=approval_id,
        approvers=approvers or [],
        reason=reason,
    )


NODE_TYPES: dict[str, dict] = {
    "sql": {
        "label": "SQL 查询",
        "desc": "在湖仓执行只读 SQL，返回行数与前 20 行预览",
        "icon": "explore",
        "fields": [
            {"key": "sql", "label": "SQL 语句", "type": "textarea"},
            {"key": "limit", "label": "返回行数上限", "type": "number", "placeholder": "200"},
        ],
        "executor": _exec_sql,
    },
    "python": {
        "label": "Python",
        "desc": "在受限命名空间执行 Python 代码，可读 params / nodes，结果写入 result 或 print",
        "icon": "studio",
        "fields": [
            {"key": "code", "label": "Python 代码", "type": "textarea"},
            {"key": "timeout", "label": "超时（秒）", "type": "number", "placeholder": "30"},
        ],
        "executor": _exec_python,
    },
    "shell": {
        "label": "Shell",
        "desc": "执行本地 shell 命令，捕获 stdout / stderr，默认超时 60s",
        "icon": "compute",
        "fields": [
            {"key": "command", "label": "命令", "type": "textarea"},
            {"key": "timeout", "label": "超时（秒）", "type": "number", "placeholder": "60"},
            {"key": "cwd", "label": "工作目录（可选）", "type": "text"},
        ],
        "executor": _exec_shell,
    },
    "notebook": {
        "label": "Notebook",
        "desc": "顺序执行 cell 列表，cell 间共享变量",
        "icon": "studio",
        "fields": [{"key": "cells", "label": "Notebook 单元（每行一个代码 cell）", "type": "textarea"}],
        "executor": _exec_notebook,
    },
    "quality": {
        "label": "质量校验",
        "desc": "调用质量引擎做前置阻断；模块不可用时直接跑 quality_rules 降级校验",
        "icon": "quality",
        "fields": [
            {"key": "table_ref", "label": "表（catalog.schema.table）", "type": "text"},
            {"key": "blocking", "label": "失败即阻断", "type": "checkbox"},
        ],
        "executor": _exec_quality,
    },
    "http": {
        "label": "HTTP 回调",
        "desc": "请求外部 HTTP 接口，2xx/3xx 视为成功",
        "icon": "ingest",
        "fields": [
            {"key": "url", "label": "URL", "type": "text"},
            {"key": "method", "label": "方法", "type": "select", "options": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
            {"key": "headers", "label": "请求头（JSON）", "type": "textarea"},
            {"key": "body", "label": "请求体（JSON / 文本）", "type": "textarea"},
            {"key": "timeout", "label": "超时（秒）", "type": "number", "placeholder": "15"},
        ],
        "executor": _exec_http,
    },
    "subflow": {
        "label": "子流程",
        "desc": "递归调用另一个工作流，最多 5 层嵌套",
        "icon": "flows",
        "fields": [{"key": "flow_id", "label": "子流程 ID 或名称", "type": "text"}],
        "executor": _exec_subflow,
    },
    "notify": {
        "label": "通知",
        "desc": "写审计日志并返回通知渠道列表（不真发外部请求）",
        "icon": "settings",
        "fields": [
            {"key": "channels", "label": "渠道（逗号分隔）", "type": "text", "placeholder": "email,webhook"},
            {"key": "title", "label": "标题", "type": "text"},
            {"key": "message", "label": "内容", "type": "textarea"},
        ],
        "executor": _exec_notify,
    },
    "approval": {
        "label": "人工审批",
        "desc": "创建审批单并返回 waiting，审批通过后再继续下游",
        "icon": "security",
        "fields": [
            {"key": "reason", "label": "审批事由", "type": "textarea"},
            {"key": "approvers", "label": "审批人（逗号分隔）", "type": "text"},
        ],
        "executor": _exec_approval,
    },
}


def describe_node_type(kind: str) -> dict:
    spec = NODE_TYPES.get(str(kind or "").strip()) or {}
    return {
        "key": kind,
        "label": spec.get("label", kind),
        "desc": spec.get("desc", ""),
        "icon": spec.get("icon", "flows"),
        "fields": spec.get("fields", []),
    }


def meta() -> dict:
    """供前端渲染选择器：9 种节点 + 12 种条件。"""
    return {
        "node_types": [
            {"key": key, **{k: v for k, v in spec.items() if k != "executor"}}
            for key, spec in NODE_TYPES.items()
        ],
        "conditions": [
            {"key": key, "label": spec["label"], "desc": spec["desc"], "params": spec.get("params", [])}
            for key, spec in CONDITIONS.items()
        ],
        "node_type_count": len(NODE_TYPES),
        "condition_count": len(CONDITIONS),
        "run_modes": [
            {"key": "cron", "label": "周期调度", "desc": "按 cron 表达式触发"},
            {"key": "continuous", "label": "持续运行", "desc": "上一轮结束即再跑，最小间隔 60s"},
            {"key": "manual", "label": "仅手动", "desc": "只能手动触发"},
        ],
        "max_depth": MAX_DEPTH,
        "min_continuous_seconds": MIN_CONTINUOUS_SECONDS,
    }


# ------------------------------------------------------------------ 节点执行


def execute_node(
    node: dict,
    workspace_id: str,
    flow: dict,
    run_id: str,
    scope: dict,
    depth: int = 0,
) -> dict:
    """执行单个节点：合并两层参数 → 渲染配置 → 调 executor。"""
    kind = str(node.get("type") or "sql")
    base_scope = dict(scope)

    # 节点级 params：先按工作流作用域渲染，再覆盖工作流级参数
    node_params_raw = node.get("params") if isinstance(node.get("params"), dict) else {}
    node_params = {k: render_template(v, base_scope) for k, v in node_params_raw.items()}
    merged_params = {**(base_scope.get("params") or {}), **node_params}
    node_scope = {
        **base_scope,
        "params": merged_params,
        "node": {
            "id": node.get("id", ""),
            "name": node.get("name", ""),
            "type": kind,
            "params": node_params,
        },
    }

    cfg = render_template(node.get("config") if isinstance(node.get("config"), dict) else {}, node_scope)
    exec_node = {**node, "config": cfg}
    ctx = _executor_ctx(workspace_id, flow, run_id, exec_node, node_scope, depth)

    spec = NODE_TYPES.get(kind)
    if not spec:
        return _result(False, error=f"不支持的节点类型：{kind}")
    try:
        outcome = spec["executor"](ctx)
    except Exception as exc:  # noqa: BLE001 - 节点异常统一转成失败结果
        return _result(False, error=f"{type(exc).__name__}: {exc}")
    if not isinstance(outcome, dict):
        return _result(False, error="节点执行器返回了非法结构")
    outcome.setdefault("ok", False)
    outcome.setdefault("output", "")
    outcome.setdefault("rows", None)
    outcome.setdefault("error", None)
    return outcome


def _node_status(outcome: dict) -> str:
    if outcome.get("waiting"):
        return "waiting"
    return "success" if outcome.get("ok") else "failed"


def _node_retries(node: dict, flow: dict) -> int:
    raw = node.get("retries")
    if raw is None:
        raw = flow.get("retries")
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ 运行


def _flow_row(flow: str | dict) -> dict | None:
    if isinstance(flow, dict):
        return flow
    return store.row("SELECT * FROM flows WHERE id = ?", (flow,))


def run_flow(
    flow: str | dict,
    trigger: str = "manual",
    params: dict | None = None,
    workspace_id: str | None = None,
    _depth: int = 0,
) -> dict:
    """执行工作流：拓扑排序 → 逐节点评估条件 → 执行 → 写 flow_runs / flow_node_runs。"""
    ensure_columns()
    row = _flow_row(flow)
    if not row:
        raise ValueError("工作流不存在")
    ws = workspace_id or row.get("workspace_id") or ""
    nodes = _json_load(row.get("nodes"), []) or []
    edges = _json_load(row.get("edges"), []) or []
    if not isinstance(nodes, list):
        nodes = []
    if not isinstance(edges, list):
        edges = []
    if not nodes:
        raise ValueError("工作流没有节点")

    run_id = store.new_id()
    started_at = store.now()
    store.insert(
        "flow_runs",
        {
            "id": run_id,
            "flow_id": row.get("id"),
            "status": "running",
            "trigger": trigger or "manual",
            "started_at": started_at,
            "finished_at": None,
            "log": "[]",
        },
    )

    log: list[dict] = []
    results: dict[str, dict] = {}
    base_scope = build_scope(row, params or {}, run_id=run_id, trigger=trigger or "manual")

    try:
        ordered = topo_order(nodes, edges)
    except ValueError as exc:
        store.update(
            "flow_runs",
            run_id,
            {
                "status": "failed",
                "finished_at": store.now(),
                "log": json.dumps([{"node": "-", "status": "failed", "error": str(exc)}], ensure_ascii=False),
            },
        )
        return {
            "id": run_id,
            "flow_id": row.get("id"),
            "status": "failed",
            "error": str(exc),
            "nodes": [],
            "log": [{"node": "-", "status": "failed", "error": str(exc)}],
            "started_at": started_at,
            "finished_at": store.now(),
        }

    for node in ordered:
        node_id = str(node.get("id"))
        node_name = str(node.get("name") or node_id)
        kind = str(node.get("type") or "sql")
        cond_name = str(node.get("condition") or "all_success")
        ups = [results[uid] for uid in upstream_of(node_id, edges) if uid in results]

        node_row_id = store.new_id()
        node_started = store.now()
        store.insert(
            "flow_node_runs",
            {
                "id": node_row_id,
                "run_id": run_id,
                "flow_id": row.get("id"),
                "node_id": node_id,
                "name": node_name,
                "type": kind,
                "status": "running",
                "output": "",
                "error": "",
                "started_at": node_started,
                "finished_at": None,
            },
        )

        scope = {**base_scope, "nodes": (base_scope.get("nodes") or {})}
        scope = build_scope(
            row,
            params or {},
            run_id=run_id,
            trigger=trigger or "manual",
            node_results=results,
        )

        if ups and not evaluate_condition(cond_name, ups, node, scope):
            entry = {
                "node_id": node_id,
                "name": node_name,
                "type": kind,
                "status": "skipped",
                "ok": False,
                "output": "",
                "rows": None,
                "error": "",
                "condition": cond_name,
                "reason": f"运行条件「{describe_condition(cond_name)['label']}」未满足",
                "started_at": node_started,
                "finished_at": store.now(),
            }
            store.update(
                "flow_node_runs",
                node_row_id,
                {
                    "status": "skipped",
                    "output": "",
                    "error": entry["reason"],
                    "finished_at": entry["finished_at"],
                },
            )
            results[node_id] = entry
            log.append(entry)
            continue

        attempts = _node_retries(node, row) + 1
        outcome: dict = _result(False, error="未执行")
        node_scope_params = {**(params or {})}
        for attempt in range(1, attempts + 1):
            outcome = execute_node(node, ws, row, run_id, scope, _depth)
            if outcome.get("ok") or outcome.get("waiting") or attempt >= attempts:
                break
            time.sleep(min(2, attempt))
        status = _node_status(outcome)
        finished_at = store.now()
        entry = {
            "node_id": node_id,
            "name": node_name,
            "type": kind,
            "status": status,
            "ok": bool(outcome.get("ok")),
            "output": _truncate(outcome.get("output") or ""),
            "rows": outcome.get("rows"),
            "error": _truncate(outcome.get("error") or ""),
            "condition": cond_name,
            "attempts": attempts,
            "started_at": node_started,
            "finished_at": finished_at,
        }
        for key in ("preview", "sql", "elapsed_ms", "channels", "approval_id", "child_run_id", "child_status", "blocked", "reasons", "source", "status_code", "waiting"):
            if key in outcome and outcome[key] not in (None, "", [], {}):
                entry[key] = outcome[key]
        store.update(
            "flow_node_runs",
            node_row_id,
            {
                "status": status,
                "output": _truncate(entry.get("output") or ""),
                "error": _truncate(entry.get("error") or ""),
                "finished_at": finished_at,
            },
        )
        results[node_id] = entry
        log.append(entry)

    statuses = [e["status"] for e in log]
    if "failed" in statuses:
        run_status = "failed"
    elif "waiting" in statuses:
        run_status = "waiting"
    else:
        run_status = "success"
    finished_at = store.now()
    summary = (
        f"{len(log)} 个节点：成功 {statuses.count('success')}、失败 {statuses.count('failed')}、"
        f"跳过 {statuses.count('skipped')}、等待 {statuses.count('waiting')}"
    )
    store.update(
        "flow_runs",
        run_id,
        {
            "status": run_status,
            "finished_at": finished_at,
            "log": json.dumps(log, ensure_ascii=False, default=str),
        },
    )
    run = {
        "id": run_id,
        "flow_id": row.get("id"),
        "flow_name": row.get("name"),
        "status": run_status,
        "trigger": trigger or "manual",
        "summary": summary,
        "nodes": log,
        "log": log,
        "node_count": len(log),
        "started_at": started_at,
        "finished_at": finished_at,
        "params": {**(build_scope(row, params or {})["params"])},
    }
    _fire_alerts(ws, row, run)
    return run


def _fire_alerts(workspace_id: str, flow: dict, run: dict) -> None:
    """运行结束后触发告警钩子。告警是旁路能力，任何异常都不能影响主流程。"""
    try:
        from . import alerts

        alerts.notify_flow_run(workspace_id, flow.get("id") or "", flow.get("name") or "", run)
    except Exception:  # noqa: BLE001
        pass


def run_flow_by_id(flow_id: str, trigger: str = "manual", params: dict | None = None) -> dict:
    """按 ID 运行，带同工作流并发保护。"""
    with _running_lock:
        if flow_id in _running:
            raise ValueError("该工作流正在运行中")
        _running.add(flow_id)
    try:
        return run_flow(flow_id, trigger=trigger, params=params)
    finally:
        with _running_lock:
            _running.discard(flow_id)


def run_detail(run_id: str) -> dict | None:
    run = store.row("SELECT * FROM flow_runs WHERE id = ?", (run_id,))
    if not run:
        return None
    flow = store.row("SELECT id, name, description, cron, timezone FROM flows WHERE id = ?", (run.get("flow_id"),)) or {}
    node_rows = store.rows(
        "SELECT * FROM flow_node_runs WHERE run_id = ? ORDER BY started_at, rowid", (run_id,)
    )
    nodes = []
    for r in node_rows:
        duration = None
        try:
            if r.get("started_at") and r.get("finished_at"):
                duration = int(
                    (
                        datetime.fromisoformat(str(r["finished_at"]))
                        - datetime.fromisoformat(str(r["started_at"]))
                    ).total_seconds()
                    * 1000
                )
        except (TypeError, ValueError):
            duration = None
        nodes.append({**r, "duration_ms": duration})
    return {
        **run,
        "log": _json_load(run.get("log"), []),
        "flow_name": flow.get("name", ""),
        "nodes": nodes,
    }


def list_runs(flow_id: str, limit: int = 20) -> list[dict]:
    rows = store.rows(
        "SELECT * FROM flow_runs WHERE flow_id = ? ORDER BY started_at DESC LIMIT ?",
        (flow_id, int(limit)),
    )
    out = []
    for r in rows:
        entry = {**r, "log": _json_load(r.get("log"), [])}
        entry["node_stats"] = {
            "success": sum(1 for e in entry["log"] if e.get("status") == "success"),
            "failed": sum(1 for e in entry["log"] if e.get("status") == "failed"),
            "skipped": sum(1 for e in entry["log"] if e.get("status") == "skipped"),
            "waiting": sum(1 for e in entry["log"] if e.get("status") == "waiting"),
        }
        out.append(entry)
    return out


# ------------------------------------------------------------------ 工作流 CRUD


def _normalize_node(node: dict, index: int) -> dict:
    node = dict(node or {})
    node_id = str(node.get("id") or f"n{index + 1}")
    kind = str(node.get("type") or "sql")
    if kind not in NODE_TYPES:
        kind = "sql"
    cond = str(node.get("condition") or "all_success")
    if cond not in CONDITIONS:
        cond = "all_success"
    cfg = node.get("config")
    if not isinstance(cfg, dict):
        # 兼容扁平写法（sql / url / code 直接挂在节点上）
        cfg = {
            key: node[key]
            for key in ("sql", "url", "method", "code", "command", "cells", "table_ref", "flow_id", "channels", "title", "message", "reason", "limit", "timeout", "headers", "body", "approvers")
            if key in node
        }
    return {
        "id": node_id,
        "type": kind,
        "name": str(node.get("name") or node_id),
        "config": cfg,
        "params": node.get("params") if isinstance(node.get("params"), dict) else {},
        "condition": cond,
        "condition_config": node.get("condition_config") if isinstance(node.get("condition_config"), dict) else {},
        "retries": node.get("retries"),
    }


def _normalize_nodes(nodes: list[dict] | None) -> list[dict]:
    raw = [n for n in (nodes or []) if isinstance(n, dict)]
    return [_normalize_node(n, i) for i, n in enumerate(raw)]


def _normalize_edges(edges: list | None, nodes: list[dict]) -> list[list[str]]:
    ids = {n["id"] for n in nodes}
    out: list[list[str]] = []
    for edge in edges or []:
        if isinstance(edge, dict):
            src, dst = edge.get("source") or edge.get("from"), edge.get("target") or edge.get("to")
        elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
            src, dst = edge[0], edge[1]
        else:
            continue
        src, dst = str(src), str(dst)
        if src in ids and dst in ids and src != dst and [src, dst] not in out:
            out.append([src, dst])
    return out


def list_flows(workspace_id: str) -> list[dict]:
    ensure_columns()
    rows = store.rows(
        "SELECT * FROM flows WHERE workspace_id = ? ORDER BY created_at DESC", (workspace_id,)
    )
    out = []
    for r in rows:
        nodes = _json_load(r.get("nodes"), []) or []
        last = store.row(
            "SELECT id, status, trigger, started_at, finished_at FROM flow_runs "
            "WHERE flow_id = ? ORDER BY started_at DESC LIMIT 1",
            (r["id"],),
        )
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "description": r.get("description") or "",
                "cron": r.get("cron") or "",
                "enabled": bool(r.get("enabled")),
                "retries": int(r.get("retries") or 0),
                "node_count": len(nodes),
                "edge_count": len(_json_load(r.get("edges"), []) or []),
                "schedule_text": _schedule_text(r),
                "run_mode": r.get("run_mode") or "cron",
                "interval_seconds": int(r.get("interval_seconds") or MIN_CONTINUOUS_SECONDS),
                "timezone": r.get("timezone") or "",
                "params": _json_load(r.get("params"), {}) or {},
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
                "last_run": last,
            }
        )
    return out


def _schedule_text(flow: dict) -> str:
    mode = str(flow.get("run_mode") or "cron")
    if mode == "continuous":
        interval = max(MIN_CONTINUOUS_SECONDS, int(flow.get("interval_seconds") or MIN_CONTINUOUS_SECONDS))
        return f"持续运行（最小间隔 {interval}s）"
    cron = flow.get("cron") or ""
    if not cron:
        return "仅手动触发"
    text = describe_cron(cron)
    tz = flow.get("timezone") or ""
    return f"{text}（{tz}）" if tz else text


def get_flow(flow_id: str, workspace_id: str | None = None) -> dict | None:
    ensure_columns()
    sql = "SELECT * FROM flows WHERE id = ?"
    args: list = [flow_id]
    if workspace_id:
        sql += " AND workspace_id = ?"
        args.append(workspace_id)
    row = store.row(sql, tuple(args))
    if not row:
        return None
    nodes = _normalize_nodes(_json_load(row.get("nodes"), []) or [])
    edges = _normalize_edges(_json_load(row.get("edges"), []) or [], nodes)
    return {
        **row,
        "nodes": nodes,
        "edges": edges,
        "params": _json_load(row.get("params"), {}) or {},
        "enabled": bool(row.get("enabled")),
        "schedule_text": _schedule_text(row),
        "run_mode": row.get("run_mode") or "cron",
        "interval_seconds": int(row.get("interval_seconds") or MIN_CONTINUOUS_SECONDS),
        "timezone": row.get("timezone") or "",
        "has_cycle": detect_cycle(nodes, edges),
        "runs": list_runs(flow_id, limit=10),
    }


def create_flow(
    workspace_id: str,
    name: str,
    description: str = "",
    cron: str = "",
    enabled: bool = False,
    nodes: list[dict] | None = None,
    edges: list | None = None,
    params: dict | None = None,
    retries: int = 0,
    run_mode: str = "cron",
    interval_seconds: int = MIN_CONTINUOUS_SECONDS,
    timezone: str = "",
) -> dict:
    ensure_columns()
    clean_nodes = _normalize_nodes(nodes)
    clean_edges = _normalize_edges(edges, clean_nodes)
    if clean_nodes and not clean_edges and len(clean_nodes) > 1:
        clean_edges = [[clean_nodes[i]["id"], clean_nodes[i + 1]["id"]] for i in range(len(clean_nodes) - 1)]
    mode = run_mode if run_mode in ("cron", "continuous", "manual") else "cron"
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "name": name,
        "description": description or "",
        "nodes": json.dumps(clean_nodes, ensure_ascii=False),
        "edges": json.dumps(clean_edges, ensure_ascii=False),
        "cron": cron or None,
        "enabled": 1 if (enabled and (cron or mode == "continuous")) else 0,
        "retries": max(0, int(retries or 0)),
        "params": json.dumps(params or {}, ensure_ascii=False),
        "run_mode": mode,
        "interval_seconds": max(MIN_CONTINUOUS_SECONDS, int(interval_seconds or MIN_CONTINUOUS_SECONDS)),
        "timezone": timezone or "",
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("flows", row)
    return get_flow(row["id"], workspace_id) or row


def update_flow(flow_id: str, workspace_id: str, payload: dict) -> dict | None:
    ensure_columns()
    current = store.row(
        "SELECT * FROM flows WHERE id = ? AND workspace_id = ?", (flow_id, workspace_id)
    )
    if not current:
        return None
    data: dict = {"updated_at": store.now()}
    if "name" in payload and payload["name"]:
        data["name"] = payload["name"]
    if "description" in payload:
        data["description"] = payload["description"] or ""
    if "nodes" in payload and payload["nodes"] is not None:
        clean_nodes = _normalize_nodes(payload["nodes"])
        data["nodes"] = json.dumps(clean_nodes, ensure_ascii=False)
        if "edges" in payload and payload["edges"] is not None:
            data["edges"] = json.dumps(_normalize_edges(payload["edges"], clean_nodes), ensure_ascii=False)
    elif "edges" in payload and payload["edges"] is not None:
        clean_nodes = _normalize_nodes(_json_load(current.get("nodes"), []) or [])
        data["edges"] = json.dumps(_normalize_edges(payload["edges"], clean_nodes), ensure_ascii=False)
    if "cron" in payload:
        data["cron"] = payload["cron"] or None
    if "retries" in payload:
        data["retries"] = max(0, int(payload["retries"] or 0))
    if "params" in payload:
        data["params"] = json.dumps(payload["params"] or {}, ensure_ascii=False)
    if "run_mode" in payload:
        mode = payload["run_mode"] if payload["run_mode"] in ("cron", "continuous", "manual") else "cron"
        data["run_mode"] = mode
    if "interval_seconds" in payload:
        data["interval_seconds"] = max(
            MIN_CONTINUOUS_SECONDS, int(payload["interval_seconds"] or MIN_CONTINUOUS_SECONDS)
        )
    if "timezone" in payload:
        data["timezone"] = payload["timezone"] or ""
    if "enabled" in payload and payload["enabled"] is not None:
        data["enabled"] = 1 if payload["enabled"] else 0
    store.update("flows", flow_id, data)
    return get_flow(flow_id, workspace_id)


def delete_flow(flow_id: str, workspace_id: str) -> bool:
    row = store.row("SELECT id FROM flows WHERE id = ? AND workspace_id = ?", (flow_id, workspace_id))
    if not row:
        return False
    store.execute("DELETE FROM flow_runs WHERE flow_id = ?", (flow_id,))
    try:
        store.execute("DELETE FROM flow_node_runs WHERE flow_id = ?", (flow_id,))
    except Exception:  # noqa: BLE001
        pass
    store.execute("DELETE FROM flows WHERE id = ?", (flow_id,))
    return True


def set_enabled(flow_id: str, workspace_id: str, enabled: bool) -> dict | None:
    return update_flow(flow_id, workspace_id, {"enabled": enabled})


# ------------------------------------------------------------------ 调度


class WorkflowScheduler:
    """cron 调度 + continuous 持续运行。

    * cron 按分钟去重，避免同一分钟重复触发。
    * continuous 以上一轮开始时间为基准，最小间隔 60s。
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_minute: dict[str, str] = {}
        self._last_run: dict[str, float] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="workflow-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 调度线程不能挂
                pass
            self._stop.wait(20)

    def tick(self, when: datetime | None = None) -> list[str]:
        """扫描一次到期工作流，返回触发的工作流 ID 列表。"""
        ensure_columns()
        when = when or datetime.now()
        stamp = when.strftime("%Y-%m-%d %H:%M")
        fired: list[str] = []
        flows = store.rows(
            "SELECT * FROM flows WHERE enabled = 1 AND (cron IS NOT NULL AND cron != '' OR run_mode = 'continuous')"
        )
        for flow in flows:
            mode = str(flow.get("run_mode") or "cron")
            if mode == "manual":
                continue
            if mode == "continuous":
                if not self._continuous_due(flow, when):
                    continue
            else:
                if self._last_minute.get(flow["id"]) == stamp:
                    continue
                if not cron_matches(flow.get("cron") or "", when, flow.get("timezone") or ""):
                    continue
                self._last_minute[flow["id"]] = stamp
            try:
                run_flow_by_id(flow["id"], trigger="schedule")
                fired.append(flow["id"])
            except Exception:  # noqa: BLE001
                pass
        return fired

    def _continuous_due(self, flow: dict, when: datetime) -> bool:
        flow_id = str(flow.get("id"))
        interval = max(MIN_CONTINUOUS_SECONDS, int(flow.get("interval_seconds") or MIN_CONTINUOUS_SECONDS))
        now_ts = when.timestamp()
        last = self._last_run.get(flow_id)
        if last is None:
            row = store.row(
                "SELECT started_at FROM flow_runs WHERE flow_id = ? ORDER BY started_at DESC LIMIT 1",
                (flow_id,),
            )
            if row and row.get("started_at"):
                try:
                    last = datetime.fromisoformat(str(row["started_at"])).timestamp()
                except (TypeError, ValueError):
                    last = None
            self._last_run[flow_id] = last if last is not None else 0.0
        if now_ts - float(self._last_run.get(flow_id) or 0) < interval:
            return False
        self._last_run[flow_id] = now_ts
        return True


scheduler = WorkflowScheduler()
Scheduler = WorkflowScheduler


def due_flows(when: datetime | None = None) -> list[dict]:
    """只读查询：当前时刻应触发的工作流（不执行），供前端/调试使用。"""
    ensure_columns()
    when = when or datetime.now()
    out = []
    for flow in store.rows("SELECT * FROM flows WHERE enabled = 1"):
        mode = str(flow.get("run_mode") or "cron")
        if mode == "manual":
            continue
        if mode == "continuous":
            out.append({"id": flow["id"], "name": flow["name"], "mode": mode})
            continue
        if cron_matches(flow.get("cron") or "", when, flow.get("timezone") or ""):
            out.append({"id": flow["id"], "name": flow["name"], "mode": mode, "cron": flow.get("cron")})
    return out


__all__ = [
    "NODE_TYPES",
    "CONDITIONS",
    "WorkflowScheduler",
    "scheduler",
    "Scheduler",
    "meta",
    "describe_node_type",
    "describe_condition",
    "describe_cron",
    "cron_matches",
    "resolve_tz",
    "render_template",
    "build_scope",
    "topo_order",
    "detect_cycle",
    "upstream_of",
    "evaluate_condition",
    "execute_node",
    "run_flow",
    "run_flow_by_id",
    "run_detail",
    "list_runs",
    "list_flows",
    "get_flow",
    "create_flow",
    "update_flow",
    "delete_flow",
    "set_enabled",
    "due_flows",
    "ensure_columns",
]
