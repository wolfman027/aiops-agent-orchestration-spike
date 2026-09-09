"""Agent 构建与单跑（移植 agentflow ``agents/scopes.py`` 惯用法，AgentScope 2.0.3）。

- ``build_agent``：system_prompt = 渲染好的方法论 + 契约（+ 策略）；toolkit 由 spec 列表构建；
  权限 DONT_ASK + allow 白名单（与 toolkit 完全一致）。可选并入 MCP client（hybrid toolkit）、
  由 API 层解析出的 MCP 工具精确名（allow_extra 兜底非只读 MCP 工具）与自定义 middleware
  （B6 进度 recorder 走 ``MCPRecorderMiddleware``）。
- ``run_agent``：喂入 user 文本，跑完整 ReAct 循环，宽容 ``extract_json`` 取结论。
  失败时把原始回复一并返回，便于复盘。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agentscope.agent import Agent, ReActConfig
from agentscope.message import UserMsg
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from .json_utils import extract_json
from .tools.progress import ToolCallRecorder
from .tools.registry import ToolSpec, build_permission_context, build_toolkit


def build_agent(
    system_prompt: str,
    model: ChatModelBase,
    specs: list[ToolSpec],
    *,
    name: str = "diagnostician",
    recorder: ToolCallRecorder | None = None,
    max_iters: int = 10,
    mcp_clients: list[Any] | None = None,
    allow_extra: list[str] | None = None,
    middlewares: Sequence[Any] = (),
) -> Agent:
    """构建单跑 agent（可 hybrid：function specs + MCP clients）。

    - ``allow_extra``：MCP 工具里非只读（无 readOnlyHint → 不会自动 ALLOW）的精确名，
      与 spec 名单合并进 DONT_ASK 白名单，防静默 DENY。
    - ``middlewares``：AgentScope 中间件（如 B6 ``MCPRecorderMiddleware``），原样透传给 Agent。
    """
    toolkit: Toolkit = build_toolkit(specs, recorder=recorder, mcp_clients=mcp_clients)
    ctx = build_permission_context(specs, allow_extra=allow_extra)
    return Agent(
        name=name,
        system_prompt=system_prompt,
        model=model,
        toolkit=toolkit,
        state=AgentState(permission_context=ctx),
        react_config=ReActConfig(max_iters=max_iters),
        middlewares=list(middlewares),
    )


async def run_agent(agent: Agent, user_content: str) -> tuple[dict[str, Any], str]:
    """喂入 user 文本 → (解析出的 dict, 原始文本)。解析失败时 dict 为 {}。"""
    final = await agent.reply(UserMsg(name="user", content=user_content))
    text = "".join(b.text for b in final.get_content_blocks("text") if b.text)
    return extract_json(text), text
