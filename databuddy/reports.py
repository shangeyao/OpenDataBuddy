"""W8 报告与定时推送：报告 CRUD / 快照生成 / 定时推送 / 嵌出配置。

表全部在本模块内 ``CREATE TABLE IF NOT EXISTS`` 自建，不动 store.py 的 SCHEMA：

* ``reports``            —— 报告定义（话题 + 指标 + 周期 + 章节快照）
* ``report_runs``        —— 每次生成的历史快照（含章节内容与数据摘要）
* ``report_schedules``   —— 定时推送（cron + 渠道 + 启用状态）
* ``analysis_embeds``    —— 分析空间嵌出 token

推送渠道**复用 databuddy.alerts**（W7 并行开发）。该模块可能还不存在，
因此统一走 ``_load_alerts()`` 做 try/except ImportError 降级：拿不到渠道实现时
只落 ``report_runs`` 并把「未真实发送」写进日志，绝不抛异常打断报告生成。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from . import analysis, semantic, store

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    topic TEXT DEFAULT '',
    description TEXT DEFAULT '',
    metrics TEXT DEFAULT '[]',                    -- JSON: 指标名列表
    period TEXT DEFAULT 'last_month',
    sections TEXT DEFAULT '[]',                   -- JSON: 最近一次章节快照
    summary TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',                  -- draft / ready
    owner TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS report_runs (
    id TEXT PRIMARY KEY,
    report_id TEXT,
    workspace_id TEXT,
    status TEXT DEFAULT 'success',                -- success / failed
    trigger TEXT DEFAULT 'manual',                -- manual / schedule
    title TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    sections TEXT DEFAULT '[]',
    snapshot TEXT DEFAULT '{}',                   -- JSON: 关键数值快照
    channels TEXT DEFAULT '[]',                   -- JSON: 推送渠道与结果
    error TEXT DEFAULT '',
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS report_schedules (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    report_id TEXT,
    cron TEXT DEFAULT '0 9 * * *',
    channels TEXT DEFAULT '[]',                   -- JSON: [{type, target}]
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    last_status TEXT,
    next_run_hint TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS analysis_embeds (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    space_id TEXT,
    token TEXT NOT NULL,
    readonly_url TEXT DEFAULT '',
    scope TEXT DEFAULT 'readonly',                -- readonly / interactive
    expires_at TEXT,
    revoked INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_report_runs_report ON report_runs(report_id, started_at);
CREATE INDEX IF NOT EXISTS idx_report_sched_ws ON report_schedules(workspace_id);
CREATE INDEX IF NOT EXISTS idx_reports_ws ON reports(workspace_id);
CREATE INDEX IF NOT EXISTS idx_embeds_token ON analysis_embeds(token);
"""

_READY = False


def ensure_tables() -> None:
    """幂等建表。首次调用后打标记，避免每条 SQL 前重复执行 DDL。"""
    global _READY
    if _READY:
        return
    with store.db() as conn:
        conn.executescript(SCHEMA)
    _READY = True


# ------------------------------------------------------------------ 渠道降级

_ALERTS_CACHE: list[Any] = []
_ALERTS_LOADED = False


def _load_alerts() -> Any | None:
    """尝试加载 W7 的告警模块。

    两个降级条件都会返回 None（调用方降级为只落库）：
    1. ``ImportError`` —— W7 模块还没落地 / 依赖缺失；
    2. 模块存在但接口不齐（``send`` / ``get_channel`` 缺失）—— W7 改了 API。
    """
    global _ALERTS_LOADED
    if _ALERTS_LOADED:
        return _ALERTS_CACHE[0] if _ALERTS_CACHE else None
    _ALERTS_LOADED = True
    try:
        from . import alerts
    except Exception:  # noqa: BLE001 - ImportError 及其依赖错误都降级
        return None
    if not (callable(getattr(alerts, "send", None)) and callable(getattr(alerts, "get_channel", None))):
        return None
    _ALERTS_CACHE.append(alerts)
    return alerts


def _normalize_channels(channels: Any) -> list[dict]:
    """归一化渠道配置为 ``[{type, config, channel_id, name}]``。

    支持三种写法（前端可任选其一，历史数据也不会炸）：
    * ``{"channel_id": "..."}``                 —— 引用 W7 已配置的告警渠道
    * ``{"type": "email", "config": {...}}``     —— 直接给渠道类型 + 完整配置
    * ``{"type": "email", "target": "a@b.com"}`` —— 简写，target 映射到该类型的必填字段
    """
    if not channels:
        return []
    if isinstance(channels, str):
        try:
            channels = json.loads(channels)
        except json.JSONDecodeError:
            channels = [c.strip() for c in channels.split(",") if c.strip()]
    # 该类型必填字段，用于把简写 target 落到正确的 config key 上
    target_key = {
        "email": "to",
        "webhook": "url",
        "teams": "webhook_url",
        "wecom": "key",
        "slack": "webhook_url",
    }
    out: list[dict] = []
    for c in channels or []:
        if isinstance(c, str):
            c = {"type": c}
        if not isinstance(c, dict):
            continue
        if c.get("channel_id"):
            out.append(
                {
                    "channel_id": c["channel_id"],
                    "type": c.get("type") or "",
                    "config": _loads(c.get("config"), {}) or {},
                    "name": c.get("name") or "",
                }
            )
            continue
        kind = (c.get("type") or c.get("channel") or "webhook").strip()
        config = _loads(c.get("config"), {}) or {}
        target = c.get("target") or c.get("to") or ""
        key = target_key.get(kind, "url")
        if target and key not in config:
            config[key] = target
        out.append({"channel_id": "", "type": kind, "config": config, "name": c.get("name") or target})
    return out


def deliver(channels: Any, subject: str, body: str, meta: dict | None = None) -> list[dict]:
    """把报告投递到渠道。

    W7 的 ``databuddy.alerts`` 可用时走它的 ``send``（同样不真发外部请求、只落
    alert_deliveries）；模块缺失时**降级**为只落 report_runs 并如实标注
    ``simulated`` + 原因，绝不因为渠道不可用而让报告生成失败。
    """
    targets = _normalize_channels(channels)
    if not targets:
        return []
    alerts = _load_alerts()
    workspace_id = (meta or {}).get("workspace_id") or ""
    results: list[dict] = []
    for target in targets:
        kind = target["type"]
        label = target.get("name") or kind
        if alerts is None:
            results.append(
                {
                    "channel": kind,
                    "target": label,
                    "ok": True,
                    "simulated": True,
                    "detail": "告警模块（databuddy.alerts）尚不可用，已降级为只落库，未真实投递",
                }
            )
            continue
        try:
            channel = None
            if target.get("channel_id"):
                channel = alerts.get_channel(workspace_id, target["channel_id"])
                if not channel:
                    results.append(
                        {
                            "channel": kind,
                            "target": label,
                            "ok": False,
                            "simulated": True,
                            "detail": f"告警渠道 {target['channel_id']} 不存在",
                        }
                    )
                    continue
            else:
                channel = {
                    "id": "",
                    "type": kind,
                    "name": label,
                    "config": target.get("config") or {},
                    "workspace_id": workspace_id,
                }
            sent = alerts.send(
                channel,
                subject,
                body,
                meta or {},
                workspace_id=workspace_id,
                event="report",
                severity="info",
                dedup_key=f"report:{subject}:{label}",
            )
            issues = sent.get("issues") or []
            ok = bool(sent.get("ok", True)) and not issues
            detail = "已投递（模拟，仅落库）"
            if sent.get("suppressed"):
                detail = sent.get("reason") or "5 分钟内已投递过相同内容，已抑制"
            elif issues:
                detail = "渠道配置有问题：" + "; ".join(issues)
            results.append(
                {
                    "channel": kind,
                    "target": label,
                    "ok": ok,
                    "simulated": bool(sent.get("simulated", True)),
                    "suppressed": bool(sent.get("suppressed")),
                    "detail": detail,
                    "delivery_id": sent.get("delivery_id", ""),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单个渠道失败不能拖垮整份报告
            results.append(
                {
                    "channel": kind,
                    "target": label,
                    "ok": False,
                    "simulated": False,
                    "detail": f"投递失败：{exc}",
                }
            )
    return results


# ------------------------------------------------------------------ 报告 CRUD

def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _decorate(row: dict) -> dict:
    out = dict(row)
    out["metrics"] = _loads(out.get("metrics"), [])
    out["sections"] = _loads(out.get("sections"), [])
    return out


def list_reports(workspace_id: str) -> list[dict]:
    ensure_tables()
    rows = store.rows(
        "SELECT * FROM reports WHERE workspace_id = ? ORDER BY created_at DESC", (workspace_id,)
    )
    for r in rows:
        r["metrics"] = _loads(r.get("metrics"), [])
        r["sections"] = _loads(r.get("sections"), [])
        r["run_count"] = store.scalar(
            "SELECT count(*) FROM report_runs WHERE report_id = ?", (r["id"],), 0
        )
        last = store.row(
            "SELECT status, started_at, trigger FROM report_runs WHERE report_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (r["id"],),
        )
        r["last_run"] = last
        sched = store.row(
            "SELECT id, cron, channels, enabled FROM report_schedules WHERE report_id = ? LIMIT 1",
            (r["id"],),
        )
        # 与 get_report / list_schedules 保持一致：channels 必须是解析后的数组，
        # 否则前端拿到的是一段 JSON 字符串，渲染会错位。
        if sched:
            sched["channels"] = _loads(sched.get("channels"), [])
            sched["enabled"] = bool(sched.get("enabled"))
            sched["schedule_text"] = describe_cron(sched.get("cron") or "")
        r["schedule"] = sched
    return rows


def get_report(workspace_id: str, report_id: str) -> dict | None:
    ensure_tables()
    row = store.row(
        "SELECT * FROM reports WHERE id = ? AND workspace_id = ?", (report_id, workspace_id)
    )
    if not row:
        return None
    out = _decorate(row)
    out["runs"] = [
        {**r, "sections": _loads(r.get("sections"), []), "channels": _loads(r.get("channels"), []),
         "snapshot": _loads(r.get("snapshot"), {})}
        for r in store.rows(
            "SELECT * FROM report_runs WHERE report_id = ? ORDER BY started_at DESC LIMIT 20",
            (report_id,),
        )
    ]
    out["schedules"] = [
        _decorate_schedule(s)
        for s in store.rows(
            "SELECT * FROM report_schedules WHERE report_id = ? ORDER BY created_at DESC", (report_id,)
        )
    ]
    return out


def create_report(workspace_id: str, payload: dict, owner: str = "") -> dict:
    ensure_tables()
    name = (payload.get("name") or payload.get("topic") or "").strip()
    if not name:
        raise ValueError("报告名称不能为空")
    metrics = payload.get("metrics") or []
    if isinstance(metrics, str):
        metrics = [s.strip() for s in metrics.replace("、", ",").split(",") if s.strip()]
    # 校验指标存在，避免存一个跑不出来的报告
    for m in metrics:
        if not semantic.find_metric(workspace_id, m):
            raise ValueError(f"指标 {m} 不存在")
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "name": name,
        "topic": payload.get("topic") or name,
        "description": payload.get("description") or "",
        "metrics": json.dumps(metrics, ensure_ascii=False),
        "period": payload.get("period") or "last_month",
        "sections": json.dumps(payload.get("sections") or [], ensure_ascii=False),
        "summary": payload.get("summary") or "",
        "status": "ready" if payload.get("sections") else "draft",
        "owner": owner,
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("reports", row)
    return _decorate(row)


def update_report(workspace_id: str, report_id: str, payload: dict) -> dict | None:
    ensure_tables()
    row = store.row(
        "SELECT * FROM reports WHERE id = ? AND workspace_id = ?", (report_id, workspace_id)
    )
    if not row:
        return None
    patch: dict[str, Any] = {}
    for key in ("name", "topic", "description", "period", "summary", "status"):
        if payload.get(key) is not None:
            patch[key] = payload[key]
    if payload.get("metrics") is not None:
        metrics = payload["metrics"]
        if isinstance(metrics, str):
            metrics = [s.strip() for s in metrics.replace("、", ",").split(",") if s.strip()]
        patch["metrics"] = json.dumps(metrics, ensure_ascii=False)
    if payload.get("sections") is not None:
        patch["sections"] = json.dumps(payload["sections"], ensure_ascii=False)
        patch["status"] = "ready"
    patch["updated_at"] = store.now()
    store.update("reports", report_id, patch)
    return get_report(workspace_id, report_id)


def delete_report(workspace_id: str, report_id: str) -> bool:
    ensure_tables()
    row = store.row(
        "SELECT id FROM reports WHERE id = ? AND workspace_id = ?", (report_id, workspace_id)
    )
    if not row:
        return False
    store.execute("DELETE FROM report_runs WHERE report_id = ?", (report_id,))
    store.execute("DELETE FROM report_schedules WHERE report_id = ?", (report_id,))
    store.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    return True


# ------------------------------------------------------------------ 生成与快照

def run_report(
    workspace_id: str,
    report_id: str,
    trigger: str = "manual",
    channels: Any = None,
    user_id: str | None = None,
) -> dict:
    """生成一次报告快照并落 report_runs；有渠道则尝试推送（失败不影响落库）。"""
    ensure_tables()
    report = store.row(
        "SELECT * FROM reports WHERE id = ? AND workspace_id = ?", (report_id, workspace_id)
    )
    if not report:
        raise ValueError("报告不存在")
    started = store.now()
    run_id = store.new_id()
    metrics = _loads(report.get("metrics"), [])
    period = report.get("period") or "last_month"

    store.insert(
        "report_runs",
        {
            "id": run_id,
            "report_id": report_id,
            "workspace_id": workspace_id,
            "status": "running",
            "trigger": trigger,
            "title": report["name"],
            "started_at": started,
            "sections": "[]",
            "snapshot": "{}",
            "channels": "[]",
        },
    )

    try:
        generated = analysis.generate_report(
            workspace_id,
            topic=report.get("topic") or report["name"],
            metrics=metrics,
            period=period,
        )
    except Exception as exc:  # noqa: BLE001 - 失败也要留痕
        store.update(
            "report_runs",
            run_id,
            {
                "status": "failed",
                "error": str(exc),
                "finished_at": store.now(),
            },
        )
        return {
            "id": run_id,
            "report_id": report_id,
            "status": "failed",
            "error": str(exc),
            "title": report["name"],
        }

    snapshot = {
        "period": generated.get("period"),
        "summary": generated.get("summary"),
        "metrics": generated.get("metrics"),
        "section_count": len(generated.get("sections") or []),
        "attribution": (generated.get("attribution") or {}).get("contributions", [])[:5],
    }

    # 渠道：显式传入优先，其次报告绑定的定时任务
    send_channels = channels
    if send_channels is None:
        sched = store.row(
            "SELECT channels FROM report_schedules WHERE report_id = ? AND enabled = 1 LIMIT 1",
            (report_id,),
        )
        send_channels = _loads((sched or {}).get("channels"), [])
    deliveries = deliver(
        send_channels,
        subject=generated["title"],
        body=generated.get("summary") or "",
        meta={"report_id": report_id, "period": generated.get("period"), "workspace_id": workspace_id},
    )

    store.update(
        "report_runs",
        run_id,
        {
            "status": "success",
            "title": generated["title"],
            "summary": generated.get("summary") or "",
            "sections": json.dumps(generated.get("sections") or [], ensure_ascii=False),
            "snapshot": json.dumps(snapshot, ensure_ascii=False),
            "channels": json.dumps(deliveries, ensure_ascii=False),
            "finished_at": store.now(),
        },
    )
    store.update(
        "reports",
        report_id,
        {
            "sections": json.dumps(generated.get("sections") or [], ensure_ascii=False),
            "summary": generated.get("summary") or "",
            "status": "ready",
            "updated_at": store.now(),
        },
    )
    # 更新定时任务的上次运行时间
    for sched in store.rows("SELECT id FROM report_schedules WHERE report_id = ?", (report_id,)):
        store.update(
            "report_schedules",
            sched["id"],
            {"last_run_at": store.now(), "last_status": "success", "updated_at": store.now()},
        )
    store.audit(
        workspace_id, user_id, "run_report", report["name"], {"run_id": run_id, "channels": len(deliveries)}
    )
    return {
        "id": run_id,
        "report_id": report_id,
        "status": "success",
        "title": generated["title"],
        "summary": generated.get("summary") or "",
        "sections": generated.get("sections") or [],
        "snapshot": snapshot,
        "channels": deliveries,
        "finished_at": store.now(),
    }


def list_runs(workspace_id: str, report_id: str, limit: int = 30) -> list[dict]:
    ensure_tables()
    rows = store.rows(
        "SELECT * FROM report_runs WHERE report_id = ? AND workspace_id = ? "
        "ORDER BY started_at DESC LIMIT ?",
        (report_id, workspace_id, limit),
    )
    for r in rows:
        r["sections"] = _loads(r.get("sections"), [])
        r["channels"] = _loads(r.get("channels"), [])
        r["snapshot"] = _loads(r.get("snapshot"), {})
    return rows


# ------------------------------------------------------------------ 定时推送

def _decorate_schedule(row: dict) -> dict:
    out = dict(row)
    out["channels"] = _loads(out.get("channels"), [])
    out["enabled"] = bool(out.get("enabled"))
    out["schedule_text"] = describe_cron(out.get("cron") or "")
    return out


def describe_cron(expr: str) -> str:
    """给前端展示的中文调度说明（复用 flows 的描述能力，缺失时降级）。"""
    try:
        from .flows import describe_cron as _dc

        return _dc(expr)
    except Exception:  # noqa: BLE001
        return expr or "未配置调度"


def list_schedules(workspace_id: str) -> list[dict]:
    ensure_tables()
    rows = store.rows(
        "SELECT * FROM report_schedules WHERE workspace_id = ? ORDER BY created_at DESC",
        (workspace_id,),
    )
    out = []
    for r in rows:
        item = _decorate_schedule(r)
        report = store.row("SELECT name FROM reports WHERE id = ?", (r["report_id"],))
        item["report_name"] = (report or {}).get("name") or "-"
        out.append(item)
    return out


def create_schedule(workspace_id: str, payload: dict) -> dict:
    ensure_tables()
    report_id = payload.get("report_id") or ""
    if not report_id:
        raise ValueError("必须绑定一个报告")
    if not store.row(
        "SELECT id FROM reports WHERE id = ? AND workspace_id = ?", (report_id, workspace_id)
    ):
        raise ValueError("报告不存在")
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "report_id": report_id,
        "cron": payload.get("cron") or "0 9 * * *",
        "channels": json.dumps(_normalize_channels(payload.get("channels")), ensure_ascii=False),
        "enabled": 1 if payload.get("enabled", True) else 0,
        "next_run_hint": describe_cron(payload.get("cron") or "0 9 * * *"),
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("report_schedules", row)
    return _decorate_schedule(row)


def update_schedule(workspace_id: str, schedule_id: str, payload: dict) -> dict | None:
    ensure_tables()
    row = store.row(
        "SELECT * FROM report_schedules WHERE id = ? AND workspace_id = ?",
        (schedule_id, workspace_id),
    )
    if not row:
        return None
    patch: dict[str, Any] = {"updated_at": store.now()}
    if payload.get("cron") is not None:
        patch["cron"] = payload["cron"]
        patch["next_run_hint"] = describe_cron(payload["cron"])
    if payload.get("channels") is not None:
        patch["channels"] = json.dumps(_normalize_channels(payload["channels"]), ensure_ascii=False)
    if payload.get("enabled") is not None:
        patch["enabled"] = 1 if payload["enabled"] else 0
    store.update("report_schedules", schedule_id, patch)
    fresh = store.row("SELECT * FROM report_schedules WHERE id = ?", (schedule_id,))
    return _decorate_schedule(fresh) if fresh else None


def delete_schedule(workspace_id: str, schedule_id: str) -> bool:
    ensure_tables()
    row = store.row(
        "SELECT id FROM report_schedules WHERE id = ? AND workspace_id = ?",
        (schedule_id, workspace_id),
    )
    if not row:
        return False
    store.execute("DELETE FROM report_schedules WHERE id = ?", (schedule_id,))
    return True


def run_due(when: datetime | None = None) -> list[dict]:
    """调度入口：给 flows.scheduler 的 tick 调用，跑所有到点的启用任务。"""
    ensure_tables()
    when = when or datetime.now()
    try:
        from .flows import cron_matches
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for sched in store.rows(
        "SELECT * FROM report_schedules WHERE enabled = 1 AND cron IS NOT NULL AND cron != ''"
    ):
        if not cron_matches(sched["cron"], when):
            continue
        report = store.row("SELECT * FROM reports WHERE id = ?", (sched["report_id"],))
        if not report:
            continue
        try:
            out.append(
                run_report(
                    report["workspace_id"] or sched["workspace_id"],
                    report["id"],
                    trigger="schedule",
                    channels=_loads(sched.get("channels"), []),
                )
            )
        except Exception as exc:  # noqa: BLE001
            store.update(
                "report_schedules",
                sched["id"],
                {"last_status": "failed", "last_run_at": store.now(), "updated_at": store.now()},
            )
            out.append({"report_id": report["id"], "status": "failed", "error": str(exc)})
    return out


# ------------------------------------------------------------------ 嵌出

def create_embed(workspace_id: str, space_id: str, payload: dict | None = None) -> dict:
    """为分析空间生成嵌出 token（落库，幂等复用未撤销的）。"""
    ensure_tables()
    payload = payload or {}
    existing = store.row(
        "SELECT * FROM analysis_embeds WHERE workspace_id = ? AND space_id = ? AND revoked = 0 "
        "ORDER BY created_at DESC LIMIT 1",
        (workspace_id, space_id),
    )
    if existing and not payload.get("rotate"):
        return _decorate_embed(existing)
    token = secrets.token_urlsafe(32)
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "space_id": space_id,
        "token": token,
        "readonly_url": payload.get("readonly_url") or f"/embed/analysis/{space_id}?token={token}",
        "scope": payload.get("scope") or "readonly",
        "expires_at": payload.get("expires_at") or "",
        "revoked": 0,
        "created_at": store.now(),
    }
    store.insert("analysis_embeds", row)
    return _decorate_embed(row)


def _decorate_embed(row: dict) -> dict:
    out = dict(row)
    out["revoked"] = bool(out.get("revoked"))
    out["iframe"] = (
        f'<iframe src="{out.get("readonly_url") or ""}" width="100%" height="720" '
        f'frameborder="0" allowfullscreen></iframe>'
    )
    out["markdown"] = f"![分析空间]({out.get('readonly_url') or ''})"
    return out


def get_embed(workspace_id: str, space_id: str) -> dict:
    """读取（没有就创建）分析空间的嵌出配置。"""
    ensure_tables()
    space = store.row(
        "SELECT * FROM analysis_spaces WHERE id = ? AND workspace_id = ?", (space_id, workspace_id)
    )
    if not space:
        raise ValueError("分析空间不存在")
    embed = create_embed(workspace_id, space_id)
    embed["space"] = {
        "id": space["id"],
        "name": space["name"],
        "description": space.get("description") or "",
    }
    return embed


def resolve_embed_token(token: str) -> dict | None:
    """只读访问：用 token 换空间配置（不校验登录，供 iframe 内匿名只读渲染）。"""
    ensure_tables()
    row = store.row(
        "SELECT * FROM analysis_embeds WHERE token = ? AND revoked = 0 LIMIT 1", (token,)
    )
    if not row:
        return None
    space = store.row("SELECT * FROM analysis_spaces WHERE id = ?", (row["space_id"],))
    out = _decorate_embed(row)
    out["space"] = {
        "id": (space or {}).get("id"),
        "name": (space or {}).get("name"),
        "tables": _loads((space or {}).get("tables"), []),
        "metrics": _loads((space or {}).get("metrics"), []),
    }
    return out


def revoke_embed(workspace_id: str, space_id: str) -> bool:
    ensure_tables()
    row = store.row(
        "SELECT id FROM analysis_embeds WHERE workspace_id = ? AND space_id = ? AND revoked = 0",
        (workspace_id, space_id),
    )
    if not row:
        return False
    store.update("analysis_embeds", row["id"], {"revoked": 1})
    return True


# ------------------------------------------------------------------ 汇总

def summary(workspace_id: str) -> dict:
    """报告页顶部统计卡。"""
    ensure_tables()
    reports = store.scalar("SELECT count(*) FROM reports WHERE workspace_id = ?", (workspace_id,), 0)
    schedules = store.scalar(
        "SELECT count(*) FROM report_schedules WHERE workspace_id = ? AND enabled = 1",
        (workspace_id,),
        0,
    )
    runs = store.scalar(
        "SELECT count(*) FROM report_runs WHERE workspace_id = ?", (workspace_id,), 0
    )
    ok_runs = store.scalar(
        "SELECT count(*) FROM report_runs WHERE workspace_id = ? AND status = 'success'",
        (workspace_id,),
        0,
    )
    return {
        "reports": reports,
        "schedules": schedules,
        "runs": runs,
        "success_runs": ok_runs,
        "success_rate": round(ok_runs / runs, 4) if runs else None,
        "alerts_module": _load_alerts() is not None,
    }
