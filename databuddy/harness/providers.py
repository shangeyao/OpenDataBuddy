"""模型接入层：DeepSeek 为默认底座，同时兼容任意 OpenAI 协议端点。

DeepSeek / Qwen / Kimi / 硅基流动 / vLLM / Ollama / LM Studio 都是同一套
`/chat/completions` + `tools` 协议，所以这里只做一层薄适配，不引第三方 SDK。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import store

DEFAULTS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "label": "DeepSeek",
    },
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "label": "OpenAI"},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5-coder:7b", "label": "Ollama 本地"},
}


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.2
    timeout: int = 120
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return DEFAULTS.get(self.provider, {}).get("label", self.provider)

    def endpoint(self) -> str:
        base = (self.base_url or "").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


def resolve_config() -> LLMConfig:
    provider = store.get_setting("llm.provider") or os.environ.get("LLM_PROVIDER", "deepseek")
    provider = provider if provider in DEFAULTS else "deepseek"
    default = DEFAULTS[provider]

    base_url = (
        store.get_setting("llm.base_url")
        or os.environ.get("LLM_BASE_URL")
        or os.environ.get("DEEPSEEK_BASE_URL")
        or default["base_url"]
    )
    api_key = (
        store.get_setting("llm.api_key")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or ""
    )
    model = (
        store.get_setting("llm.model")
        or os.environ.get("LLM_MODEL")
        or os.environ.get("DEEPSEEK_MODEL")
        or default["model"]
    )
    try:
        temperature = float(store.get_setting("llm.temperature") or 0.2)
    except ValueError:
        temperature = 0.2
    return LLMConfig(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
    )


def is_configured(cfg: LLMConfig | None = None) -> bool:
    cfg = cfg or resolve_config()
    if cfg.provider == "ollama" or "127.0.0.1" in cfg.base_url or "localhost" in cfg.base_url:
        return bool(cfg.base_url and cfg.model)
    return bool(cfg.api_key and cfg.model)


def chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    cfg: LLMConfig | None = None,
    tool_choice: str = "auto",
) -> dict:
    """调用一次对话补全，返回 {content, tool_calls, usage, raw}。"""
    cfg = cfg or resolve_config()
    if not is_configured(cfg):
        raise LLMError("未配置大模型 API Key")
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    try:
        with httpx.Client(timeout=cfg.timeout) as client:
            resp = client.post(cfg.endpoint(), headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise LLMError(f"模型响应超时（{cfg.timeout}s），可换更快的模型或缩小上下文") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"无法连接模型服务：{exc}") from exc

    if resp.status_code == 401:
        raise LLMError("API Key 无效或已过期（401）")
    if resp.status_code == 404:
        raise LLMError(f"接口路径不存在（404）：{cfg.endpoint()}，请检查 Base URL 是否包含 /v1")
    if resp.status_code >= 400:
        snippet = resp.text[:300]
        raise LLMError(f"模型服务返回 {resp.status_code}：{snippet}")

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise LLMError("模型服务返回的不是 JSON") from exc

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {"__raw__": raw_args}
        tool_calls.append({"id": call.get("id"), "name": fn.get("name"), "args": args})

    return {
        "content": message.get("content") or "",
        "reasoning": message.get("reasoning_content") or "",
        "tool_calls": tool_calls,
        "usage": data.get("usage") or {},
        "model": data.get("model") or cfg.model,
    }


def ping(cfg: LLMConfig | None = None) -> dict:
    """设置页「测试连接」。"""
    cfg = cfg or resolve_config()
    try:
        out = chat(
            [
                {"role": "system", "content": "你是一个连通性探测器，只回复 OK。"},
                {"role": "user", "content": "ping"},
            ],
            cfg=cfg,
        )
        return {"ok": True, "model": out.get("model"), "reply": (out.get("content") or "")[:80]}
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
