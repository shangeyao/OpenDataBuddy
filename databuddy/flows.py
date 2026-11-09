"""数据工程：DAG 工作流 + cron 调度 + 运行历史。

节点类型：sql（只读查询）、quality（断言结果必须为 0）、http（回调外部系统）、
notebook（占位，执行内联 Python 片段在 v0.2 提供）。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any

from . import catalog, store

_running: set[str] = set()
_lock = threading.Lock()


# ------------------------------------------------------------------ cron 解析


def _field_match(field: str, value: int, lo: int, hi: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:] or 1)
            if (value - lo) % step == 0:
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


def cron_matches(expr: str, when: datetime | None = None) -> bool:
    when = when or datetime.now()
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
    if h.isdigit() and dom == "*" and mon == "*" and dow.isdigit():
        names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
        return f"每{names[int(dow) % 7]} {int(h):02d}:{int(m):02d}"
    return expr


# --------------------------------------------------------------------- DAG


def topo_order(nodes: list[dict], edges: list[list[str]]) -> list[dict]:
    by_id = {n["id"]: n for n in nodes}
    indeg = {n["id"]: 0 for n in nodes}
    children: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for src, dst in edges:
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
    if len(ordered) != len(nodes):
        raise ValueError("工作流存在环，无法执行")
    return ordered


def _run_node(workspace_id: str, node: dict) -> dict:
    cfg = node.get("config") or {}
    kind = node.get("type", "sql")
    started = store.now()
    if kind == "sql":
        sql = cfg.get("sql", "")
        if not sql.strip():
            raise ValueError("SQL 节点未配置语句")
        res = catalog.run_workspace_sql(workspace_id, sql, limit=int(cfg.get("limit", 200)))
        return {
            "node": node["id"],
            "name": node.get("name") or node["id"],
            "type": kind,
            "status": "success",
            "started_at": started,
            "finished_at": store.now(),
            "rows": res["row_count"],
            "elapsed_ms": res["elapsed_ms"],
            "sql": res["sql"],
            "preview": {"columns": res["columns"], "rows": res["rows"][:20]},
        }
    if kind == "quality":
        sql = cfg.get("sql", "")
        res = catalog.run_workspace_sql(workspace_id, sql, limit=5)
        value = res["rows"][0][0] if res["rows"] and res["rows"][0] else 0
        ok = float(value or 0) == 0
        return {
            "node": node["id"],
            "name": node.get("name") or node["id"],
            "type": kind,
            "status": "success" if ok else "failed",
            "started_at": started,
            "finished_at": store.now(),
            "rows": 1,
            "elapsed_ms": res["elapsed_ms"],
            "sql": res["sql"],
            "message": f"断言值 {value}（期望 0）",
        }
    if kind == "http":
        import httpx

        url = cfg.get("url")
        if not url:
            raise ValueError("HTTP 节点未配置 url")
        method = (cfg.get("method") or "GET").upper()
        with httpx.Client(timeout=15) as client:
            resp = client.request(method, url, json=cfg.get("body") or None)
        ok = resp.status_code < 400
        return {
            "node": node["id"],
            "name": node.get("name") or node["id"],
            "type": kind,
            "status": "success" if ok else "failed",
            "started_at": started,
            "finished_at": store.now(),
            "rows": 0,
            "message": f"{method} {url} -> {resp.status_code}",
        }
    return {
        "node": node["id"],
        "name": node.get("name") or node["id"],
        "type": kind,
        "status": "skipped",
        "started_at": started,
        "finished_at": store.now(),
        "rows": 0,
        "message": f"暂不支持的节点类型 {kind}",
    }


def run_flow(flow: dict, trigger: str = "manual", workspace_id: str | None = None) -> dict:
    ws = workspace_id or flow["workspace_id"]
    nodes = json.loads(flow.get("nodes") or "[]")
    edges = json.loads(flow.get("edges") or "[]")
    run_id = store.new_id()
    store.insert(
        "flow_runs",
        {
            "id": run_id,
            "flow_id": flow["id"],
            "status": "running",
            "trigger": trigger,
            "started_at": store.now(),
            "log": "[]",
        },
    )

    log: list[dict] = []
    status = "success"
    try:
        ordered = topo_order(nodes, edges)
    except ValueError as exc:
        log.append({"node": "-", "status": "failed", "message": str(exc)})
        store.update(
            "flow_runs",
            run_id,
            {"status": "failed", "finished_at": store.now(), "log": json.dumps(log, ensure_ascii=False)},
        )
        return {"id": run_id, "status": "failed", "log": log}

    retries = int(flow.get("retries") or 0)
    for node in ordered:
        attempt = 0
        while True:
            try:
                entry = _run_node(ws, node)
                break
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt > retries:
                    entry = {
                        "node": node["id"],
                        "name": node.get("name") or node["id"],
                        "type": node.get("type", "sql"),
                        "status": "failed",
                        "message": str(exc),
                        "started_at": store.now(),
                        "finished_at": store.now(),
                    }
                    break
                time.sleep(1)
        log.append(entry)
        if entry["status"] == "failed":
            status = "failed"
            break

    store.update(
        "flow_runs",
        run_id,
        {"status": status, "finished_at": store.now(), "log": json.dumps(log, ensure_ascii=False)},
    )
    return {"id": run_id, "status": status, "log": log}


def run_flow_by_id(flow_id: str, trigger: str = "manual") -> dict:
    flow = store.row("SELECT * FROM flows WHERE id = ?", (flow_id,))
    if not flow:
        raise ValueError("工作流不存在")
    with _lock:
        if flow_id in _running:
            raise ValueError("该工作流正在运行中")
        _running.add(flow_id)
    try:
        return run_flow(flow, trigger)
    finally:
        with _lock:
            _running.discard(flow_id)


# ------------------------------------------------------------------ 调度线程


class Scheduler:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_minute: dict[str, str] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="flow-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(20)

    def tick(self, when: datetime | None = None) -> None:
        when = when or datetime.now()
        stamp = when.strftime("%Y-%m-%d %H:%M")
        flows = store.rows("SELECT * FROM flows WHERE enabled = 1 AND cron IS NOT NULL AND cron != ''")
        for flow in flows:
            if self._last_minute.get(flow["id"]) == stamp:
                continue
            if not cron_matches(flow["cron"], when):
                continue
            self._last_minute[flow["id"]] = stamp
            try:
                run_flow_by_id(flow["id"], trigger="schedule")
            except Exception:  # noqa: BLE001
                pass


scheduler = Scheduler()


def flow_detail(flow: dict) -> dict:
    out = dict(flow)
    out["nodes"] = json.loads(flow.get("nodes") or "[]")
    out["edges"] = json.loads(flow.get("edges") or "[]")
    out["schedule_text"] = describe_cron(flow.get("cron") or "")
    out["runs"] = [
        {**r, "log": json.loads(r.get("log") or "[]")}
        for r in store.rows(
            "SELECT * FROM flow_runs WHERE flow_id = ? ORDER BY started_at DESC LIMIT 10",
            (flow["id"],),
        )
    ]
    return out
