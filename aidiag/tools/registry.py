"""工具注册表抽象（v4 §5）：ToolSpec + build_toolkit + 权限白名单。

- ``ToolSpec(name, description, func, read_only=True)``：加一个新集成 = 加一个 spec，
  agent 提示词零改动（可插拔证明对象）。
- ``build_toolkit`` 用 ``FunctionTool``（只读工具自动 ALLOW），并支持包一层进度
  recorder（对 FunctionTool 参数 schema 推断无损：用 ``functools.wraps`` 保留
  ``__wrapped__``，agentscope 会沿它读原签名）。
- ``build_permission_context``：DONT_ASK + 对每个注册工具加 allow 规则，allow 集合与
  toolkit 完全一致（否则工具被静默 DENY）。
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionMode,
    PermissionRule,
)
from agentscope.tool import FunctionTool, Toolkit

from .progress import NullRecorder, ToolCallRecorder

ToolFunc = Callable[..., Any]


@dataclass
class ToolSpec:
    name: str
    description: str
    func: ToolFunc
    read_only: bool = True


def _hooked(func: ToolFunc, recorder: ToolCallRecorder, name: str) -> ToolFunc:
    """给工具套一层进度记录；wraps 保留原始签名供 schema 推断。"""

    @functools.wraps(func)
    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        recorder.on_tool_start(name, kwargs)
        try:
            result = await func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— 记录后继续抛，交给框架
            recorder.on_tool_end(name, {"error": str(exc)}, ok=False)
            raise
        ok = not (isinstance(result, dict) and result.get("found") is False)
        recorder.on_tool_end(name, result if isinstance(result, dict) else {}, ok=ok)
        return result

    return _wrapper  # type: ignore[return-value]


def _filter_connected_mcps(clients: list[Any]) -> list[Any]:
    """剔除 stateful 但未 connect 的 MCP client。

    Toolkit.__init__ 对 "stateful 但未连接" 的 client 抛 ValueError（见 agentflow mcp.py）。
    stateless HTTP client 不需 connect，恒保留；stateful（stdio / 常驻）必须 is_connected。
    """
    return [c for c in clients if not c.is_stateful or c.is_connected]


def build_toolkit(
    specs: list[ToolSpec],
    recorder: ToolCallRecorder | None = None,
    mcp_clients: list[Any] | None = None,
) -> Toolkit:
    """function specs + MCP client → 单个 hybrid Toolkit（B4）。

    - function tools 每个包一层进度 recorder（_hooked，wraps 保留签名供 schema 推断）；
    - MCP client 经 _filter_connected_mcps 防御剔除后并入同组；空则退化为纯 function。
    """
    recorder = recorder or NullRecorder()
    tools: list[FunctionTool] = []
    for spec in specs:
        func = _hooked(spec.func, recorder, spec.name)
        tools.append(
            FunctionTool(
                func=func,
                name=spec.name,
                description=spec.description,
                is_read_only=spec.read_only,
            )
        )
    mcps = _filter_connected_mcps(mcp_clients or [])
    if mcps:
        return Toolkit(tools=tools, mcps=mcps)
    return Toolkit(tools=tools)


def build_permission_context(
    specs: list[ToolSpec],
    *,
    tenant_id: str = "local",
    allow_extra: list[str] | None = None,
) -> PermissionContext:
    """DONT_ASK + allow 规则与 toolkit 一致（防静默 DENY）。

    MCP 只读工具在 AgentScope 自动 ALLOW（readOnlyHint）；非只读残留靠 allow_extra 注入
    精确名（``mcp__{server}__{tool}``）兜底——DONT_ASK 下无 allow 规则的工具会被 DENY。
    """
    ctx = PermissionContext(mode=PermissionMode.DONT_ASK)
    names = [s.name for s in specs] + (allow_extra or [])
    for name in names:
        ctx.allow_rules.setdefault(name, []).append(
            PermissionRule(
                tool_name=name,
                rule_content=None,
                behavior=PermissionBehavior.ALLOW,
                source=f"tenant/{tenant_id}",
            )
        )
    return ctx


def bind_tools(env: dict[str, Any], tool_funcs: dict[str, ToolFunc]) -> list[ToolSpec]:
    """由数据源工厂输出 + 描述表生成 ToolSpec 列表（Stage 1 只暴露只读工具）。"""
    from .datasources import TOOL_DESCRIPTIONS

    specs: list[ToolSpec] = []
    for name, func in tool_funcs.items():
        specs.append(ToolSpec(name=name, description=TOOL_DESCRIPTIONS.get(name, name), func=func))
    return specs


def signature_summary(func: ToolFunc) -> str:
    return str(inspect.signature(func))
