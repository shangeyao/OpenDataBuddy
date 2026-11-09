"""MCP Server（stdio JSON-RPC 2.0）。

    python -m databuddy.mcp

把平台已有的 20 个工具原样暴露成 MCP 工具，让外部 Agent（Claude Desktop、Cursor、
自研 Agent）走与人相同的调用链路与同一套护栏。

安全约束：
* 必须提供 DATABUDDY_TOKEN（在控制台登录后从 localStorage 取），否则拒绝启动；
* 写操作若命中「三要素」，stdio 场景无法人工确认，直接拒绝并提示到控制台操作；
* 所有调用落 traces 与 audit_logs，和 Web 端共用同一份审计。
"""
from __future__ import annotations

import json
import os
import sys
import traceback

from . import security, store, tools  # noqa: F401  tools 负责注册工具
from .harness import guardrails, providers, skills

PROTOCOL = "2024-11-05"


def _resolve_identity() -> tuple[dict, str]:
    token = os.environ.get("DATABUDDY_TOKEN", "").strip()
    user = security.user_by_token(token) if token else None
    if not user:
        raise SystemExit(
            "缺少有效身份：请设置 DATABUDDY_TOKEN（控制台登录后 localStorage 里的 odb.token）。"
        )
    user.pop("password_hash", None)
    ws = os.environ.get("DATABUDDY_WORKSPACE_ID", "").strip()
    if not ws:
        ws = store.scalar(
            "SELECT workspace_id FROM workspace_members WHERE user_id = ? ORDER BY workspace_id LIMIT 1",
            (user["id"],),
        ) or store.scalar("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
    if not ws:
        raise SystemExit("没有可用的工作空间")
    return user, ws


def _tool_schema(spec) -> dict:
    required = spec.parameters.get("required") or []
    props = spec.parameters.get("properties") or {}
    return {
        "name": spec.name,
        "description": spec.description,
        "inputSchema": {"type": "object", "properties": props, "required": required},
    }


class Server:
    def __init__(self) -> None:
        self.user, self.workspace_id = _resolve_identity()
        self.model = providers.resolve_config().model

    # ------------------------------------------------------------ 分发
    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return self._ok(
                mid,
                {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "opendatabuddy", "version": "0.2.0"},
                },
            )
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            groups = tools.TOOL_GROUPS["engineering"]
            specs = [skills.get(n) for n in skills.REGISTRY]
            visible = [s for s in specs if s and s.group in groups]
            return self._ok(mid, {"tools": [_tool_schema(s) for s in visible]})
        if method == "tools/call":
            return self._call(mid, params)
        return self._err(mid, -32601, f"unsupported method: {method}")

    def _call(self, mid, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = skills.get(name)
        if not spec:
            return self._err(mid, -32602, f"unknown tool: {name}")

        verdict = guardrails.assess(name, args, self.workspace_id)
        if verdict["require_approval"]:
            return self._ok(
                mid,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "该操作会修改平台资产并触及敏感数据，需要人工确认。"
                            "请到 OpenDataBuddy 控制台（Buddy 对话）执行这一步。\n"
                            f"判定依据：{verdict['reason']}",
                        }
                    ],
                    "isError": True,
                },
            )
        try:
            result = skills.invoke(
                name, args, {"workspace_id": self.workspace_id, "user": self.user, "conversation_id": "mcp"}
            )
            store.audit(self.workspace_id, self.user["id"], "mcp_call", name, {"args": args})
            text = json.dumps(result, ensure_ascii=False, default=str)
            if len(text) > 20000:
                text = text[:20000] + "…（已截断）"
            return self._ok(mid, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # noqa: BLE001
            return self._ok(
                mid,
                {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            )

    @staticmethod
    def _ok(mid, result) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main() -> int:
    store.init_db()
    try:
        server = Server()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(
        f"[opendatabuddy-mcp] ready · workspace={server.workspace_id} · user={server.user['email']}",
        file=sys.stderr,
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}), flush=True)
            continue
        try:
            out = server.handle(msg)
        except Exception:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            out = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": "internal error"}}
        if out is not None:
            print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
