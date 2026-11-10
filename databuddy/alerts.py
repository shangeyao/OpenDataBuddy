"""多渠道告警：5 种渠道 + 4 类事件 + 5 分钟去重抑制。

设计要点
--------
* **离线优先**：`send()` 一律不发起真实外部请求，只把「本应发出去的报文」落到
  `alert_deliveries` 表并返回 `simulated: true`。这样在无外网、无凭据的环境里
  整条链路（规则匹配 → 渲染 → 投递 → 审计 → 前端展示）依然可以完整跑通并被验证。
* **三钩子模型**：每种渠道只实现 `validate_config / format_payload / send` 三个钩子，
  新增渠道（飞书、钉钉…）只要往 `CHANNELS` 里加一项，其余逻辑全部复用。
* **去重抑制**：同一 (渠道, 事件去重键) 在 5 分钟窗口内只真正投递一次，
  重复的记为 `suppressed` 行 —— 抑制本身也要留痕，否则排障时看不到「为什么没收到」。
* 表结构在本模块内 `CREATE TABLE IF NOT EXISTS` 自建，不改 `store.SCHEMA`。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import store

# ---------------------------------------------------------------- 常量与表结构

#: 告警抑制窗口（秒）—— 同一事件 5 分钟内只发一次
SUPPRESS_WINDOW_SECONDS = 300

#: 4 类可绑定事件
EVENTS: dict[str, dict] = {
    "flow_run_failed": {
        "label": "工作流运行失败",
        "desc": "工作流实例状态变为 failed 时触发",
        "default_severity": "critical",
        "icon": "flow",
    },
    "quality_rule_failed": {
        "label": "质量规则失败",
        "desc": "数据质量规则校验未通过时触发",
        "default_severity": "warning",
        "icon": "quality",
    },
    "drift_detected": {
        "label": "数据漂移",
        "desc": "基线对比发现分布 / Schema 漂移时触发",
        "default_severity": "warning",
        "icon": "drift",
    },
    "anomaly_detected": {
        "label": "异常检测",
        "desc": "零配置异常检测发现行数突变 / 新鲜度异常时触发",
        "default_severity": "critical",
        "icon": "anomaly",
    },
}

SEVERITIES = {"info": "提示", "warning": "警告", "critical": "严重"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_channels (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    type TEXT NOT NULL,                           -- email / webhook / teams / wecom / slack
    name TEXT DEFAULT '',
    config TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    status TEXT DEFAULT 'unknown',                -- unknown / ok / failed
    last_test_at TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    event TEXT NOT NULL,                          -- flow_run_failed / quality_rule_failed / drift_detected / anomaly_detected
    channels TEXT DEFAULT '[]',                   -- JSON: 渠道 id 列表
    condition TEXT DEFAULT '{}',                  -- JSON: {min_severity, table_ref, flow_id, keywords}
    severity TEXT DEFAULT 'warning',
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS alert_deliveries (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    rule_id TEXT,
    rule_name TEXT DEFAULT '',
    channel_id TEXT,
    channel_type TEXT DEFAULT '',
    channel_name TEXT DEFAULT '',
    event TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    severity TEXT DEFAULT 'warning',
    dedup_key TEXT DEFAULT '',
    status TEXT DEFAULT 'sent',                   -- sent / suppressed / failed
    simulated INTEGER DEFAULT 1,
    payload TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_alert_deliveries_ws ON alert_deliveries(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_alert_deliveries_dedup ON alert_deliveries(channel_id, dedup_key, created_at);
"""

_ensured = False


def ensure_tables() -> None:
    """幂等建表（首次调用时执行一次，之后只查内存标记）。"""
    global _ensured
    if _ensured:
        return
    with store.db() as conn:
        conn.executescript(SCHEMA)
    _ensured = True


def reset_cache() -> None:
    """测试用：切换数据目录后重置建表标记。"""
    global _ensured
    _ensured = False


# ------------------------------------------------------------------ 渠道定义


def _as_config(config: Any) -> dict:
    if isinstance(config, dict):
        return dict(config)
    if isinstance(config, str) and config.strip():
        try:
            parsed = json.loads(config)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,;\s]+", value) if p.strip()]
    return []


def _is_url(value: str) -> bool:
    return bool(re.match(r"^https?://[^\s]+$", (value or "").strip()))


def _text_of(subject: str, body: str, detail: Any = None) -> str:
    parts = [subject or "", body or ""]
    if detail:
        parts.append(detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


# ---- email -----------------------------------------------------------------


def _email_validate(config: dict) -> list[str]:
    issues = []
    to = _str_list(config.get("to"))
    if not to:
        issues.append("缺少收件人 to（可填多个，逗号分隔）")
    bad = [a for a in to if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a)]
    if bad:
        issues.append("收件人邮箱格式不正确：" + "、".join(bad[:3]))
    sender = str(config.get("from") or "").strip()
    if sender and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", sender):
        issues.append("发件人邮箱格式不正确：" + sender)
    return issues


def _email_format(subject: str, body: str, detail: Any, config: dict) -> dict:
    to = _str_list(config.get("to"))
    sender = str(config.get("from") or "alert@databuddy.local")
    text = _text_of(subject, body, detail)
    return {
        "channel": "email",
        "from": sender,
        "to": to,
        "cc": _str_list(config.get("cc")),
        "subject": f"[OpenDataBuddy 告警] {subject}",
        "text": text,
        "html": "<h3>" + _html_escape(subject) + "</h3><pre>" + _html_escape(text) + "</pre>",
    }


# ---- webhook ---------------------------------------------------------------


def _webhook_validate(config: dict) -> list[str]:
    issues = []
    url = str(config.get("url") or "").strip()
    if not url:
        issues.append("缺少回调地址 url")
    elif not _is_url(url):
        issues.append("回调地址必须是 http(s):// 开头的完整 URL")
    method = str(config.get("method") or "POST").upper()
    if method not in ("POST", "PUT", "PATCH"):
        issues.append("method 仅支持 POST / PUT / PATCH")
    return issues


def _webhook_format(subject: str, body: str, detail: Any, config: dict) -> dict:
    return {
        "channel": "webhook",
        "url": str(config.get("url") or "").strip(),
        "method": str(config.get("method") or "POST").upper(),
        "headers": {
            "Content-Type": "application/json",
            **{str(k): str(v) for k, v in (config.get("headers") or {}).items()},
        },
        "body": {
            "event": "databuddy.alert",
            "subject": subject,
            "text": body,
            "detail": detail,
            "source": "OpenDataBuddy",
        },
    }


# ---- teams -----------------------------------------------------------------


def _teams_validate(config: dict) -> list[str]:
    issues = []
    url = str(config.get("webhook_url") or config.get("url") or "").strip()
    if not url:
        issues.append("缺少 Microsoft Teams 连接器地址 webhook_url")
    elif not _is_url(url):
        issues.append("webhook_url 必须是 http(s):// 开头的完整 URL")
    return issues


def _teams_format(subject: str, body: str, detail: Any, config: dict) -> dict:
    theme = str(config.get("theme_color") or "D14343")
    facts = []
    if isinstance(detail, dict):
        for k, v in list(detail.items())[:8]:
            facts.append({"name": str(k), "value": _short(v)})
    return {
        "channel": "teams",
        "url": str(config.get("webhook_url") or config.get("url") or "").strip(),
        "body": {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": subject,
            "themeColor": theme,
            "title": f"OpenDataBuddy · {subject}",
            "text": body or subject,
            "sections": [{"facts": facts}] if facts else [],
        },
    }


# ---- wecom（企业微信群机器人）------------------------------------------------


def _wecom_validate(config: dict) -> list[str]:
    issues = []
    key = str(config.get("key") or config.get("webhook_key") or "").strip()
    url = str(config.get("webhook_url") or "").strip()
    if not key and not url:
        issues.append("缺少企业微信机器人 key（或完整 webhook_url）")
    if url and not _is_url(url):
        issues.append("webhook_url 必须是 http(s):// 开头的完整 URL")
    if key and not re.match(r"^[A-Za-z0-9\-_]{10,}$", key):
        issues.append("企业微信 key 格式不正确（应为 10 位以上字母数字）")
    if str(config.get("msgtype") or "markdown") not in ("markdown", "text"):
        issues.append("msgtype 仅支持 markdown / text")
    return issues


def _wecom_format(subject: str, body: str, detail: Any, config: dict) -> dict:
    key = str(config.get("key") or config.get("webhook_key") or "").strip()
    url = str(config.get("webhook_url") or "").strip()
    if not url:
        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    msgtype = str(config.get("msgtype") or "markdown")
    text = _text_of(subject, body, detail)
    if msgtype == "markdown":
        content = f"## {subject}\n" + "\n".join(f"> {line}" for line in text.splitlines() if line.strip())
        return {"channel": "wecom", "url": url, "body": {"msgtype": "markdown", "markdown": {"content": content}}}
    return {"channel": "wecom", "url": url, "body": {"msgtype": "text", "text": {"content": text}}}


# ---- slack -----------------------------------------------------------------


def _slack_validate(config: dict) -> list[str]:
    issues = []
    url = str(config.get("webhook_url") or config.get("url") or "").strip()
    if not url:
        issues.append("缺少 Slack Incoming Webhook 地址 webhook_url")
    elif not _is_url(url):
        issues.append("webhook_url 必须是 http(s):// 开头的完整 URL")
    channel = str(config.get("channel") or "").strip()
    if channel and not channel.startswith(("#", "@")):
        issues.append("channel 建议以 # 或 @ 开头，例如 #data-alerts")
    return issues


def _slack_format(subject: str, body: str, detail: Any, config: dict) -> dict:
    text = _text_of(subject, body, detail)
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"OpenDataBuddy · {subject}"[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2800] or subject}},
    ]
    if isinstance(detail, dict) and detail:
        fields = [
            {"type": "mrkdwn", "text": f"*{k}*\n{_short(v)}"}
            for k, v in list(detail.items())[:6]
        ]
        blocks.append({"type": "section", "fields": fields})
    payload: dict[str, Any] = {
        "channel": "slack",
        "url": str(config.get("webhook_url") or config.get("url") or "").strip(),
        "body": {"text": f"OpenDataBuddy 告警 · {subject}", "blocks": blocks},
    }
    channel = str(config.get("channel") or "").strip()
    if channel:
        payload["body"]["channel"] = channel
    return payload


def _html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _short(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------- 渠道注册表

#: 恰好 5 种渠道，每种三钩子
CHANNELS: dict[str, dict] = {
    "email": {
        "label": "邮件",
        "icon": "✉",
        "desc": "SMTP 邮件通知，适合值班邮箱与周报",
        "fields": [
            {"key": "to", "label": "收件人", "type": "text", "required": True, "hint": "多个用逗号分隔"},
            {"key": "from", "label": "发件人", "type": "text", "required": False, "hint": "默认 alert@databuddy.local"},
            {"key": "cc", "label": "抄送", "type": "text", "required": False},
        ],
        "validate_config": _email_validate,
        "format_payload": _email_format,
    },
    "webhook": {
        "label": "Webhook",
        "icon": "⇄",
        "desc": "通用 HTTP 回调，可对接自建告警中心",
        "fields": [
            {"key": "url", "label": "回调地址", "type": "text", "required": True, "hint": "https://..."},
            {"key": "method", "label": "请求方法", "type": "select", "required": False, "options": ["POST", "PUT", "PATCH"]},
            {"key": "headers", "label": "自定义 Header", "type": "json", "required": False},
        ],
        "validate_config": _webhook_validate,
        "format_payload": _webhook_format,
    },
    "teams": {
        "label": "Microsoft Teams",
        "icon": "T",
        "desc": "Teams 频道连接器（MessageCard）",
        "fields": [
            {"key": "webhook_url", "label": "连接器地址", "type": "text", "required": True},
            {"key": "theme_color", "label": "主题色", "type": "text", "required": False, "hint": "十六进制，如 D14343"},
        ],
        "validate_config": _teams_validate,
        "format_payload": _teams_format,
    },
    "wecom": {
        "label": "企业微信",
        "icon": "企",
        "desc": "企业微信群机器人，支持 markdown 卡片",
        "fields": [
            {"key": "key", "label": "机器人 Key", "type": "text", "required": True, "hint": "webhook 地址中的 key 参数"},
            {"key": "msgtype", "label": "消息类型", "type": "select", "required": False, "options": ["markdown", "text"]},
        ],
        "validate_config": _wecom_validate,
        "format_payload": _wecom_format,
    },
    "slack": {
        "label": "Slack",
        "icon": "#",
        "desc": "Slack Incoming Webhook（Block Kit）",
        "fields": [
            {"key": "webhook_url", "label": "Webhook 地址", "type": "text", "required": True},
            {"key": "channel", "label": "目标频道", "type": "text", "required": False, "hint": "如 #data-alerts"},
        ],
        "validate_config": _slack_validate,
        "format_payload": _slack_format,
    },
}


def channel_spec(channel_type: str) -> dict:
    return CHANNELS.get(channel_type or "", {})


def describe_channels() -> list[dict]:
    """供前端渲染渠道选择器：5 种类型的元数据 + 必填字段。"""
    return [
        {
            "type": key,
            "label": spec["label"],
            "icon": spec["icon"],
            "desc": spec["desc"],
            "fields": spec["fields"],
        }
        for key, spec in CHANNELS.items()
    ]


def describe_events() -> list[dict]:
    return [{"event": key, **spec} for key, spec in EVENTS.items()]


# ------------------------------------------------------------------ 渠道 CRUD


def list_channels(workspace_id: str) -> list[dict]:
    ensure_tables()
    rows = store.rows(
        "SELECT * FROM alert_channels WHERE workspace_id = ? ORDER BY created_at", (workspace_id,)
    )
    out = []
    for r in rows:
        cfg = _as_config(r.get("config"))
        spec = channel_spec(r["type"])
        issues = spec["validate_config"](cfg) if spec else ["未知渠道类型"]
        out.append(
            {
                **r,
                "config": cfg,
                "enabled": bool(r.get("enabled")),
                "label": spec.get("label", r["type"]),
                "icon": spec.get("icon", "?"),
                "desc": spec.get("desc", ""),
                "valid": not issues,
                "issues": issues,
                # 只回显打码后的配置，避免前端把凭据明文展示
                "config_preview": _preview_config(r["type"], cfg),
            }
        )
    return out


def _preview_config(channel_type: str, config: dict) -> dict:
    out = {}
    for key, value in config.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if re.search(r"key|secret|token|password|url", key, re.IGNORECASE) and len(text) > 8:
            out[key] = text[:6] + "…" + text[-4:]
        else:
            out[key] = text if len(text) <= 60 else text[:60] + "…"
    return out


def get_channel(workspace_id: str, channel_id: str) -> dict | None:
    ensure_tables()
    r = store.row(
        "SELECT * FROM alert_channels WHERE id = ? AND workspace_id = ?", (channel_id, workspace_id)
    )
    if not r:
        return None
    r["config"] = _as_config(r.get("config"))
    r["enabled"] = bool(r.get("enabled"))
    return r


def create_channel(
    workspace_id: str, channel_type: str, name: str = "", config: dict | None = None, enabled: bool = True
) -> dict:
    ensure_tables()
    spec = channel_spec(channel_type)
    if not spec:
        raise ValueError(f"不支持的渠道类型：{channel_type}（可选：{'/'.join(CHANNELS)}）")
    cfg = _as_config(config)
    issues = spec["validate_config"](cfg)
    row = {
        "id": store.new_id("ch_"),
        "workspace_id": workspace_id,
        "type": channel_type,
        "name": name or spec["label"],
        "config": json.dumps(cfg, ensure_ascii=False),
        "enabled": 1 if enabled else 0,
        "status": "failed" if issues else "ok",
        "last_test_at": store.now() if not issues else None,
        "created_at": store.now(),
    }
    store.insert("alert_channels", row)
    row["config"] = cfg
    row["issues"] = issues
    return row


def update_channel(workspace_id: str, channel_id: str, patch: dict) -> dict:
    ensure_tables()
    current = get_channel(workspace_id, channel_id)
    if not current:
        raise ValueError("渠道不存在")
    data: dict[str, Any] = {}
    if "name" in patch and patch["name"] is not None:
        data["name"] = str(patch["name"])
    if "enabled" in patch and patch["enabled"] is not None:
        data["enabled"] = 1 if patch["enabled"] else 0
    if "config" in patch and patch["config"] is not None:
        cfg = _as_config(patch["config"])
        spec = channel_spec(current["type"])
        issues = spec["validate_config"](cfg) if spec else ["未知渠道类型"]
        data["config"] = json.dumps(cfg, ensure_ascii=False)
        data["status"] = "failed" if issues else "ok"
    if data:
        store.update("alert_channels", channel_id, data)
    return get_channel(workspace_id, channel_id) or {}


def delete_channel(workspace_id: str, channel_id: str) -> None:
    ensure_tables()
    store.execute(
        "DELETE FROM alert_channels WHERE id = ? AND workspace_id = ?", (channel_id, workspace_id)
    )


# ------------------------------------------------------------------ 投递核心


def _now_dt() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _parse_dt(text: str) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _suppressed_recently(workspace_id: str, channel_id: str, dedup_key: str) -> dict | None:
    """5 分钟窗口内同渠道同事件已发过则返回那条记录。"""
    since = (_now_dt() - timedelta(seconds=SUPPRESS_WINDOW_SECONDS)).isoformat(timespec="seconds")
    return store.row(
        """
        SELECT * FROM alert_deliveries
        WHERE workspace_id = ? AND channel_id = ? AND dedup_key = ?
          AND status = 'sent' AND created_at >= ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (workspace_id, channel_id, dedup_key, since),
    )


def _record_delivery(**fields: Any) -> dict:
    payload = {
        "id": store.new_id("dl_"),
        "workspace_id": fields.get("workspace_id"),
        "rule_id": fields.get("rule_id"),
        "rule_name": fields.get("rule_name", ""),
        "channel_id": fields.get("channel_id"),
        "channel_type": fields.get("channel_type", ""),
        "channel_name": fields.get("channel_name", ""),
        "event": fields.get("event", ""),
        "subject": fields.get("subject", ""),
        "severity": fields.get("severity", "warning"),
        "dedup_key": fields.get("dedup_key", ""),
        "status": fields.get("status", "sent"),
        "simulated": 1 if fields.get("simulated", True) else 0,
        "payload": json.dumps(fields.get("payload") or {}, ensure_ascii=False),
        "error": fields.get("error", ""),
        "created_at": store.now(),
    }
    store.insert("alert_deliveries", payload)
    return payload


def send(
    channel: dict,
    subject: str,
    body: str = "",
    detail: Any = None,
    *,
    workspace_id: str = "",
    event: str = "manual",
    severity: str = "warning",
    rule_id: str | None = None,
    rule_name: str = "",
    dedup_key: str = "",
    force: bool = False,
) -> dict:
    """投递一条告警。**不发起任何外部网络请求**，只落库并回显应发报文。

    返回 {ok, simulated, suppressed, payload, delivery_id, issues}
    """
    ensure_tables()
    channel_type = channel.get("type") or ""
    spec = channel_spec(channel_type)
    cfg = _as_config(channel.get("config"))
    if not spec:
        return {"ok": False, "simulated": True, "suppressed": False, "payload": {},
                "error": f"不支持的渠道类型：{channel_type}"}

    issues = spec["validate_config"](cfg)
    if issues:
        delivery = _record_delivery(
            workspace_id=workspace_id or channel.get("workspace_id"),
            rule_id=rule_id, rule_name=rule_name,
            channel_id=channel.get("id"), channel_type=channel_type,
            channel_name=channel.get("name", ""), event=event, subject=subject,
            severity=severity, dedup_key=dedup_key, status="failed",
            payload={"issues": issues}, error="; ".join(issues),
        )
        return {"ok": False, "simulated": True, "suppressed": False, "payload": {"issues": issues},
                "delivery_id": delivery["id"], "issues": issues}

    payload = spec["format_payload"](subject, body, detail, cfg)

    key = dedup_key or f"{event}:{subject}"
    if not force and _suppressed_recently(workspace_id or channel.get("workspace_id", ""), channel.get("id", ""), key):
        delivery = _record_delivery(
            workspace_id=workspace_id or channel.get("workspace_id"),
            rule_id=rule_id, rule_name=rule_name,
            channel_id=channel.get("id"), channel_type=channel_type,
            channel_name=channel.get("name", ""), event=event, subject=subject,
            severity=severity, dedup_key=key, status="suppressed",
            payload=payload, error="",
        )
        return {
            "ok": True, "simulated": True, "suppressed": True, "payload": payload,
            "delivery_id": delivery["id"], "issues": [],
            "reason": f"{SUPPRESS_WINDOW_SECONDS // 60} 分钟内已投递过相同事件，已抑制",
        }

    delivery = _record_delivery(
        workspace_id=workspace_id or channel.get("workspace_id"),
        rule_id=rule_id, rule_name=rule_name,
        channel_id=channel.get("id"), channel_type=channel_type,
        channel_name=channel.get("name", ""), event=event, subject=subject,
        severity=severity, dedup_key=key, status="sent", payload=payload,
    )
    return {"ok": True, "simulated": True, "suppressed": False, "payload": payload,
            "delivery_id": delivery["id"], "issues": []}


def test_channel(workspace_id: str, channel_id: str) -> dict:
    """发一条测试告警，验证配置是否可用。"""
    ensure_tables()
    channel = get_channel(workspace_id, channel_id)
    if not channel:
        raise ValueError("渠道不存在")
    result = send(
        channel,
        subject="告警渠道连通性测试",
        body="这是一条来自 OpenDataBuddy 的测试告警。开源版本地模拟投递，不发起真实外部请求。",
        detail={"渠道": channel["name"], "类型": channel["type"], "测试时间": store.now()},
        workspace_id=workspace_id,
        event="channel_test",
        severity="info",
        dedup_key=f"channel_test:{channel_id}:{store.new_id()}",
        force=True,
    )
    store.update(
        "alert_channels",
        channel_id,
        {"status": "ok" if result["ok"] else "failed", "last_test_at": store.now()},
    )
    store.audit(workspace_id, None, "test_alert_channel", channel["name"], {"ok": result["ok"]})
    return {"ok": result["ok"], "simulated": True, "payload": result["payload"],
            "delivery_id": result["delivery_id"], "issues": result.get("issues", [])}


# ------------------------------------------------------------------ 规则 CRUD


def list_rules(workspace_id: str) -> list[dict]:
    ensure_tables()
    rows = store.rows(
        "SELECT * FROM alert_rules WHERE workspace_id = ? ORDER BY created_at DESC", (workspace_id,)
    )
    channels = {c["id"]: c for c in list_channels(workspace_id)}
    out = []
    for r in rows:
        ids = _as_json_list(r.get("channels"))
        out.append(
            {
                **r,
                "channels": ids,
                "condition": _as_config(r.get("condition")),
                "enabled": bool(r.get("enabled")),
                "event_label": EVENTS.get(r["event"], {}).get("label", r["event"]),
                "severity_label": SEVERITIES.get(r.get("severity"), r.get("severity")),
                "channel_names": [
                    channels[cid]["name"] for cid in ids if cid in channels
                ],
                "delivery_count": store.scalar(
                    "SELECT count(*) FROM alert_deliveries WHERE rule_id = ? AND status = 'sent'",
                    (r["id"],),
                    0,
                ),
            }
        )
    return out


def _as_json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def create_rule(
    workspace_id: str,
    name: str,
    event: str,
    channels: list[str] | None = None,
    condition: dict | None = None,
    severity: str = "warning",
    enabled: bool = True,
) -> dict:
    ensure_tables()
    if event not in EVENTS:
        raise ValueError(f"不支持的事件类型：{event}（可选：{'/'.join(EVENTS)}）")
    row = {
        "id": store.new_id("rule_"),
        "workspace_id": workspace_id,
        "name": name,
        "event": event,
        "channels": json.dumps(list(channels or []), ensure_ascii=False),
        "condition": json.dumps(_as_config(condition), ensure_ascii=False),
        "severity": severity if severity in SEVERITIES else "warning",
        "enabled": 1 if enabled else 0,
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("alert_rules", row)
    row["channels"] = _as_json_list(row["channels"])
    row["condition"] = _as_config(row["condition"])
    return row


def update_rule(workspace_id: str, rule_id: str, patch: dict) -> dict:
    ensure_tables()
    existing = store.row(
        "SELECT * FROM alert_rules WHERE id = ? AND workspace_id = ?", (rule_id, workspace_id)
    )
    if not existing:
        raise ValueError("规则不存在")
    data: dict[str, Any] = {"updated_at": store.now()}
    if patch.get("name") is not None:
        data["name"] = str(patch["name"])
    if patch.get("event") is not None:
        if patch["event"] not in EVENTS:
            raise ValueError(f"不支持的事件类型：{patch['event']}")
        data["event"] = patch["event"]
    if patch.get("channels") is not None:
        data["channels"] = json.dumps(_as_json_list(patch["channels"]), ensure_ascii=False)
    if patch.get("condition") is not None:
        data["condition"] = json.dumps(_as_config(patch["condition"]), ensure_ascii=False)
    if patch.get("severity") is not None:
        data["severity"] = patch["severity"] if patch["severity"] in SEVERITIES else "warning"
    if patch.get("enabled") is not None:
        data["enabled"] = 1 if patch["enabled"] else 0
    store.update("alert_rules", rule_id, data)
    row = store.row("SELECT * FROM alert_rules WHERE id = ?", (rule_id,)) or {}
    row["channels"] = _as_json_list(row.get("channels"))
    row["condition"] = _as_config(row.get("condition"))
    return row


def delete_rule(workspace_id: str, rule_id: str) -> None:
    ensure_tables()
    store.execute(
        "DELETE FROM alert_rules WHERE id = ? AND workspace_id = ?", (rule_id, workspace_id)
    )


# --------------------------------------------------------------- 事件分发入口

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _condition_matches(condition: dict, severity: str, detail: Any, subject: str) -> tuple[bool, str]:
    """规则条件匹配。支持 min_severity / keywords / flow_id / table_ref / status。"""
    if not condition:
        return True, ""
    min_sev = str(condition.get("min_severity") or "").strip()
    if min_sev and _SEVERITY_RANK.get(severity, 1) < _SEVERITY_RANK.get(min_sev, 0):
        return False, f"严重级别 {severity} 低于规则要求 {min_sev}"

    blob = (subject or "") + " " + (detail if isinstance(detail, str) else json.dumps(detail or {}, ensure_ascii=False))
    keywords = _str_list(condition.get("keywords"))
    if keywords and not any(k.lower() in blob.lower() for k in keywords):
        return False, "未命中关键词：" + "/".join(keywords)

    if condition.get("flow_id") and isinstance(detail, dict):
        if str(detail.get("flow_id") or "") != str(condition["flow_id"]):
            return False, "flow_id 不匹配"
    if condition.get("table_ref") and isinstance(detail, dict):
        ref = str(detail.get("table_ref") or detail.get("table") or "")
        if condition["table_ref"] not in ref:
            return False, "table_ref 不匹配"
    return True, ""


def emit(
    workspace_id: str,
    event: str,
    subject: str,
    body: str = "",
    detail: Any = None,
    *,
    severity: str = "",
    dedup_key: str = "",
    force: bool = False,
) -> dict:
    """事件入口：匹配规则 → 逐渠道渲染投递 → 去重抑制 → 返回明细。

    四类事件（flow_run_failed / quality_rule_failed / drift_detected /
    anomaly_detected）都走这里；没有匹配规则时返回 `matched: 0`，不报错。
    """
    ensure_tables()
    if event not in EVENTS:
        raise ValueError(f"不支持的事件类型：{event}（可选：{'/'.join(EVENTS)}）")
    severity = severity or EVENTS[event]["default_severity"]
    rules = store.rows(
        "SELECT * FROM alert_rules WHERE workspace_id = ? AND event = ? AND enabled = 1",
        (workspace_id, event),
    )
    channels = {c["id"]: c for c in list_channels(workspace_id)}
    sent, suppressed, failed, skipped = [], [], [], []
    key = dedup_key or f"{event}:{subject}"

    for rule in rules:
        condition = _as_config(rule.get("condition"))
        ok, reason = _condition_matches(condition, severity, detail, subject)
        if not ok:
            skipped.append({"rule": rule["name"], "reason": reason})
            continue
        target_ids = _as_json_list(rule.get("channels"))
        if not target_ids:
            skipped.append({"rule": rule["name"], "reason": "规则未绑定任何渠道"})
            continue
        for cid in target_ids:
            channel = channels.get(cid)
            if not channel:
                skipped.append({"rule": rule["name"], "reason": f"渠道 {cid} 不存在"})
                continue
            if not channel["enabled"]:
                skipped.append({"rule": rule["name"], "channel": channel["name"], "reason": "渠道已停用"})
                continue
            result = send(
                channel, subject, body, detail,
                workspace_id=workspace_id, event=event, severity=severity,
                rule_id=rule["id"], rule_name=rule["name"], dedup_key=key, force=force,
            )
            item = {
                "rule": rule["name"],
                "channel": channel["name"],
                "channel_type": channel["type"],
                "delivery_id": result.get("delivery_id"),
            }
            if result.get("suppressed"):
                suppressed.append({**item, "reason": result.get("reason", "")})
            elif result.get("ok"):
                sent.append(item)
            else:
                failed.append({**item, "error": result.get("error") or "; ".join(result.get("issues") or [])})

    if rules:
        store.audit(
            workspace_id, None, f"alert_{event}", subject,
            {"sent": len(sent), "suppressed": len(suppressed), "failed": len(failed)},
        )
    return {
        "event": event,
        "event_label": EVENTS[event]["label"],
        "subject": subject,
        "severity": severity,
        "matched": len(rules),
        "sent": sent,
        "suppressed": suppressed,
        "failed": failed,
        "skipped": skipped,
        "dedup_key": key,
    }


# ------------------------------------------------------------------ 查询接口


def list_deliveries(
    workspace_id: str, limit: int = 100, status: str | None = None, event: str | None = None
) -> list[dict]:
    ensure_tables()
    sql = "SELECT * FROM alert_deliveries WHERE workspace_id = ?"
    params: list[Any] = [workspace_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if event:
        sql += " AND event = ?"
        params.append(event)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(int(limit or 100), 500)))
    rows = store.rows(sql, params)
    for r in rows:
        try:
            r["payload"] = json.loads(r.get("payload") or "{}")
        except json.JSONDecodeError:
            r["payload"] = {}
        r["simulated"] = bool(r.get("simulated"))
    return rows


def stats(workspace_id: str) -> dict:
    """告警总览：渠道数、规则数、24h 投递量、抑制量。"""
    ensure_tables()
    since = (_now_dt() - timedelta(hours=24)).isoformat(timespec="seconds")
    total = store.scalar(
        "SELECT count(*) FROM alert_deliveries WHERE workspace_id = ?", (workspace_id,), 0
    )
    last24 = store.scalar(
        "SELECT count(*) FROM alert_deliveries WHERE workspace_id = ? AND created_at >= ?",
        (workspace_id, since), 0,
    )
    sent = store.scalar(
        "SELECT count(*) FROM alert_deliveries WHERE workspace_id = ? AND status = 'sent'",
        (workspace_id,), 0,
    )
    suppressed = store.scalar(
        "SELECT count(*) FROM alert_deliveries WHERE workspace_id = ? AND status = 'suppressed'",
        (workspace_id,), 0,
    )
    failed = store.scalar(
        "SELECT count(*) FROM alert_deliveries WHERE workspace_id = ? AND status = 'failed'",
        (workspace_id,), 0,
    )
    return {
        "channels": store.scalar(
            "SELECT count(*) FROM alert_channels WHERE workspace_id = ?", (workspace_id,), 0
        ),
        "enabled_channels": store.scalar(
            "SELECT count(*) FROM alert_channels WHERE workspace_id = ? AND enabled = 1",
            (workspace_id,), 0,
        ),
        "rules": store.scalar(
            "SELECT count(*) FROM alert_rules WHERE workspace_id = ?", (workspace_id,), 0
        ),
        "enabled_rules": store.scalar(
            "SELECT count(*) FROM alert_rules WHERE workspace_id = ? AND enabled = 1",
            (workspace_id,), 0,
        ),
        "deliveries": total,
        "last_24h": last24,
        "sent": sent,
        "suppressed": suppressed,
        "failed": failed,
        "suppress_window_seconds": SUPPRESS_WINDOW_SECONDS,
        "channel_types": len(CHANNELS),
        "event_types": len(EVENTS),
    }


# ------------------------------------------------- 与平台事件源对接（可选钩子）


def notify_flow_run(workspace_id: str, flow_id: str, flow_name: str, run: dict) -> dict:
    """工作流运行结束后调用：失败才发告警。"""
    if (run or {}).get("status") != "failed":
        return {"matched": 0, "skipped_all": True, "reason": "运行成功，不触发告警"}
    return emit(
        workspace_id,
        "flow_run_failed",
        f"工作流「{flow_name}」运行失败",
        f"实例 {run.get('id', '')} 于 {run.get('started_at', '')} 触发，状态 failed。",
        {"flow_id": flow_id, "flow_name": flow_name, "run_id": run.get("id"), "status": "failed"},
        severity="critical",
        dedup_key=f"flow_run_failed:{flow_id}",
    )


def notify_quality_failure(workspace_id: str, table_ref: str, rule_name: str, detail: str) -> dict:
    return emit(
        workspace_id,
        "quality_rule_failed",
        f"质量规则「{rule_name}」未通过",
        detail,
        {"table_ref": table_ref, "rule": rule_name, "detail": detail},
        severity="warning",
        dedup_key=f"quality_rule_failed:{table_ref}:{rule_name}",
    )


def notify_drift(workspace_id: str, table_ref: str, column: str, metric: str, score: float) -> dict:
    return emit(
        workspace_id,
        "drift_detected",
        f"{table_ref}.{column} 检测到数据漂移",
        f"指标 {metric} 得分 {score}，超过阈值。",
        {"table_ref": table_ref, "column": column, "metric": metric, "score": score},
        severity="warning",
        dedup_key=f"drift:{table_ref}:{column}:{metric}",
    )


def notify_anomaly(workspace_id: str, table_ref: str, kind: str, detail: str) -> dict:
    return emit(
        workspace_id,
        "anomaly_detected",
        f"{table_ref} 异常检测命中：{kind}",
        detail,
        {"table_ref": table_ref, "kind": kind, "detail": detail},
        severity="critical",
        dedup_key=f"anomaly:{table_ref}:{kind}",
    )


#: 事件 → 平台侧对接函数（供 Lead 接线时按需调用）
EVENT_HOOKS: dict[str, Callable[..., dict]] = {
    "flow_run_failed": notify_flow_run,
    "quality_rule_failed": notify_quality_failure,
    "drift_detected": notify_drift,
    "anomaly_detected": notify_anomaly,
}
