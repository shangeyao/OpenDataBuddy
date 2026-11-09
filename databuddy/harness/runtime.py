"""Agent 运行时：DeepSeek function calling 循环 + 护栏中断 + 恢复执行。

一次「回合」的完整链路：

    用户消息 → 装配上下文 → 模型思考 → 调工具（可能多次）→ 汇总结论
                      ↑                ↓
                  轨迹留痕 ←──── 护栏判定（高危则挂起等人工确认）

挂起状态整个序列化进 approvals 表，确认后原样恢复 —— 用户不会丢掉已跑完的步骤。
"""
from __future__ import annotations

import json
import time
from typing import Any

from .. import catalog, charts, heuristic, store, tools  # noqa: F401  触发工具注册
from . import context, guardrails, providers, skills, trace

MAX_STEPS = 10
LLM_ROWS = 20


class HarnessRuntime:
    def __init__(self, workspace_id: str, user: dict, conversation_id: str, mode: str = "engineering",
                 chat_mode: str = "agent", model: str | None = None):
        self.workspace_id = workspace_id
        self.user = user
        self.conversation_id = conversation_id
        self.mode = mode if mode in tools.TOOL_GROUPS else "engineering"
        self.chat_mode = chat_mode
        self.model = model

    # ------------------------------------------------------------- 对外入口
    def run(self, question: str) -> dict:
        cfg = providers.resolve_config()
        if self.model:
            cfg.model = self.model

        store.insert(
            "messages",
            {
                "id": store.new_id(),
                "conversation_id": self.conversation_id,
                "role": "user",
                "content": question,
                "meta": "{}",
                "created_at": store.now(),
            },
        )
        self._touch_title(question)
        guard = guardrails.scan_user_input(question)

        if not providers.is_configured(cfg):
            return self._finish_rule_based(question, guard)

        state = self._new_state(question)
        if guard["suspicious"]:
            state["artifacts"].append(
                {
                    "type": "notice",
                    "level": "warning",
                    "title": "检测到疑似指令注入",
                    "text": "输入中包含试图覆盖系统设定的表述。我仍然只把它当数据看待。",
                }
            )
        try:
            return self._loop(state, cfg)
        except providers.LLMError as exc:
            return self._finish_llm_error(question, str(exc))

    def resume(self, approval: dict) -> dict:
        payload = json.loads(approval.get("args") or "{}")
        state = payload.get("state") or {}
        call = payload.get("call") or {}
        cfg = providers.resolve_config()
        cfg.model = state.get("model") or cfg.model

        result = self._execute(call["name"], call.get("args") or {}, state)
        state["messages"].append(
            {
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": context.summarize_tool_result(call["name"], result),
            }
        )
        return self._loop(state, cfg)

    # ------------------------------------------------------------- 内部实现
    def _new_state(self, question: str) -> dict:
        history = store.rows(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (self.conversation_id,),
        )
        # 最后一条就是刚写入的用户消息，避免重复
        history = history[:-1] if history else []
        llm_history = context.trim_history(context.to_llm_messages(history))
        system = context.system_prompt(self.workspace_id, self.user, self.mode, question, self.chat_mode)
        return {
            "messages": [{"role": "system", "content": system}, *llm_history, {"role": "user", "content": question}],
            "step": 0,
            "mode": self.mode,
            "chat_mode": self.chat_mode,
            "model": self.model,
            "artifacts": [],
            "trace": [],
            "workspace_id": self.workspace_id,
            "user_id": self.user["id"],
        }

    def _loop(self, state: dict, cfg: providers.LLMConfig) -> dict:
        groups = tools.TOOL_GROUPS.get(state.get("mode") or self.mode, tools.TOOL_GROUPS["engineering"])
        if state.get("chat_mode") == "ask":
            tool_specs = None
        else:
            tool_specs = skills.specs(groups=groups)

        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}

        while state["step"] < MAX_STEPS:
            state["step"] += 1
            out = providers.chat(state["messages"], tools=tool_specs, cfg=cfg)
            usage = out.get("usage") or {}
            usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)

            if out["tool_calls"]:
                state["messages"].append(
                    {
                        "role": "assistant",
                        "content": out["content"] or "",
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": json.dumps(call["args"], ensure_ascii=False),
                                },
                            }
                            for call in out["tool_calls"]
                        ],
                    }
                )
                for call in out["tool_calls"]:
                    verdict = guardrails.assess(call["name"], call["args"], self.workspace_id)
                    if verdict["require_approval"]:
                        approval = self._save_approval(call, state, verdict)
                        return {
                            "status": "awaiting_approval",
                            "content": out["content"] or "这一步会修改平台资产，需要你确认后继续。",
                            "approval": approval,
                            "meta": self._meta(state, cfg, usage_total),
                        }
                    result = self._execute(call["name"], call["args"], state)
                    state["messages"].append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": context.summarize_tool_result(call["name"], result),
                        }
                    )
                continue

            content = out["content"] or "（模型没有返回内容）"
            self._persist_assistant(content, state, cfg, usage_total)
            return {
                "status": "ok",
                "content": content,
                "approval": None,
                "meta": self._meta(state, cfg, usage_total),
            }

        content = (
            f"已连续执行 {MAX_STEPS} 步仍未收敛，先在这里停下。\n\n"
            "已完成的操作见下方轨迹；建议把问题拆小，或直接告我「只做某一步」。"
        )
        self._persist_assistant(content, state, cfg, usage_total)
        return {"status": "ok", "content": content, "approval": None, "meta": self._meta(state, cfg, usage_total)}

    def _execute(self, name: str, args: dict, state: dict) -> Any:
        started = time.perf_counter()
        ok = True
        try:
            result = skills.invoke(
                name,
                args,
                {"workspace_id": self.workspace_id, "user": self.user, "conversation_id": self.conversation_id},
            )
        except Exception as exc:  # noqa: BLE001
            ok = False
            result = {"error": f"{type(exc).__name__}: {exc}"}
        duration = int((time.perf_counter() - started) * 1000)
        if isinstance(result, dict):
            ok = ok and not result.get("error")
            artifact = result.pop("artifact", None)
            if artifact:
                state["artifacts"].append(artifact)
        else:
            artifact = None

        state["trace"].append(
            {
                "tool": name,
                "args": args,
                "ok": ok,
                "duration_ms": duration,
                "summary": self._summary(result),
            }
        )
        trace.record(self.conversation_id, self.workspace_id, state["step"], name, args, result, ok, duration)
        return self._trim_for_llm(result)

    @staticmethod
    def _trim_for_llm(result: Any) -> Any:
        if isinstance(result, dict) and isinstance(result.get("rows"), list):
            trimmed = dict(result)
            trimmed["rows"] = result["rows"][:LLM_ROWS]
            if len(result["rows"]) > LLM_ROWS:
                trimmed["note"] = f"仅展示前 {LLM_ROWS} 行，完整结果在右侧产物里"
            return trimmed
        return result

    @staticmethod
    def _summary(result: Any) -> str:
        if isinstance(result, dict):
            if result.get("error"):
                return f"失败：{result['error']}"
            for key in ("row_count", "count", "total", "applied", "created", "hits", "flows"):
                if key in result:
                    return f"{key}={result[key]}"
            return ",".join(list(result.keys())[:5])
        return str(result)[:120]

    def _save_approval(self, call: dict, state: dict, verdict: dict) -> dict:
        payload = {
            "state": state,
            "call": {
                "id": call["id"],
                "name": call["name"],
                "args": call["args"],
            },
            "verdict": verdict,
        }
        row = {
            "id": store.new_id(),
            "conversation_id": self.conversation_id,
            "tool": call["name"],
            "args": json.dumps(payload, ensure_ascii=False),
            "reason": verdict["reason"],
            "status": "pending",
            "created_at": store.now(),
        }
        store.insert("approvals", row)
        return {
            "id": row["id"],
            "tool": call["name"],
            "args": call["args"],
            "reason": verdict["reason"],
            "factors": verdict["factors"],
        }

    def _persist_assistant(self, content: str, state: dict, cfg, usage: dict) -> None:
        store.insert(
            "messages",
            {
                "id": store.new_id(),
                "conversation_id": self.conversation_id,
                "role": "assistant",
                "content": content,
                "meta": json.dumps(self._meta(state, cfg, usage), ensure_ascii=False),
                "created_at": store.now(),
            },
        )

    def _meta(self, state: dict, cfg=None, usage: dict | None = None) -> dict:
        return {
            "engine": "llm",
            "model": getattr(cfg, "model", None) or self.model,
            "provider": getattr(cfg, "provider", None),
            "trace": state.get("trace", []),
            "artifacts": state.get("artifacts", []),
            "usage": usage or {},
            "steps": state.get("step", 0),
        }

    def _touch_title(self, question: str) -> None:
        conv = store.row("SELECT title FROM conversations WHERE id = ?", (self.conversation_id,))
        if conv and (conv["title"] or "新会话") in ("新会话", "", None):
            title = question.strip().replace("\n", " ")[:24] or "新会话"
            store.update("conversations", self.conversation_id, {"title": title, "updated_at": store.now()})

    # ------------------------------------------------------- 无模型 / 失败回退
    def _finish_rule_based(self, question: str, guard: dict) -> dict:
        out = heuristic.respond(self.workspace_id, self.user, question, self.mode)
        meta = {
            "engine": "rule",
            "model": None,
            "trace": out.get("trace", []),
            "artifacts": out.get("artifacts", []),
            "usage": {},
            "note": "未配置大模型，已使用内置规则引擎。到「设置 → 模型」填入 DeepSeek API Key 可解锁完整能力。",
        }
        if guard.get("suspicious"):
            meta["artifacts"].append(
                {"type": "notice", "level": "warning", "title": "检测到疑似指令注入", "text": "输入含覆盖系统设定的表述，已按数据处理。"}
            )
        store.insert(
            "messages",
            {
                "id": store.new_id(),
                "conversation_id": self.conversation_id,
                "role": "assistant",
                "content": out["content"],
                "meta": json.dumps(meta, ensure_ascii=False),
                "created_at": store.now(),
            },
        )
        return {"status": "ok", "content": out["content"], "approval": None, "meta": meta}

    def _finish_llm_error(self, question: str, error: str) -> dict:
        out = heuristic.respond(self.workspace_id, self.user, question, self.mode)
        content = (
            f"模型调用失败：{error}\n\n"
            "已自动切换到内置规则引擎，下面是我用本地数据算出来的结果：\n\n"
            f"---\n\n{out['content']}"
        )
        meta = {
            "engine": "rule",
            "model": None,
            "trace": out.get("trace", []),
            "artifacts": out.get("artifacts", []),
            "usage": {},
            "note": error,
        }
        store.insert(
            "messages",
            {
                "id": store.new_id(),
                "conversation_id": self.conversation_id,
                "role": "assistant",
                "content": content,
                "meta": json.dumps(meta, ensure_ascii=False),
                "created_at": store.now(),
            },
        )
        return {"status": "ok", "content": content, "approval": None, "meta": meta}
