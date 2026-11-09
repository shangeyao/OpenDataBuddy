"""Skills / Tools 注册表：统一暴露成 OpenAI function calling 协议。

工具声明里带 `risk`，护栏据此决定是否要人工确认；
执行结果里带 `artifact` 字段的，会被前端渲染成表格/图表/卡片。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

REGISTRY: dict[str, "ToolSpec"] = {}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]
    risk: str = "read"
    group: str = "general"
    meta: dict = field(default_factory=dict)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool(name: str, description: str, parameters: dict, risk: str = "read", group: str = "general"):
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            fn=fn,
            risk=risk,
            group=group,
        )
        return fn

    return decorator


def specs(groups: list[str] | None = None, names: list[str] | None = None) -> list[dict]:
    out = []
    for spec in REGISTRY.values():
        if names is not None and spec.name not in names:
            continue
        if groups is not None and spec.group not in groups:
            continue
        out.append(spec.schema())
    return out


def get(name: str) -> ToolSpec | None:
    return REGISTRY.get(name)


def invoke(name: str, args: dict, ctx: dict) -> Any:
    spec = REGISTRY.get(name)
    if not spec:
        return {"error": f"未知工具 {name}"}
    kwargs = dict(args or {})
    kwargs.pop("__raw__", None)
    sig = inspect.signature(spec.fn)
    accepts_ctx = "ctx" in sig.parameters
    if accepts_ctx:
        kwargs["ctx"] = ctx
    allowed = {k: v for k, v in kwargs.items() if k == "ctx" or k in sig.parameters}
    return spec.fn(**allowed)


def catalog_of_tools() -> list[dict]:
    return [
        {"name": s.name, "description": s.description, "risk": s.risk, "group": s.group}
        for s in REGISTRY.values()
    ]
