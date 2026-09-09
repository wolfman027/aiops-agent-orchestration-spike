"""进度 recorder：把工具调用事件写进 session（/status 轮询）+ 收集 sha（B6/B7）。

两条采集路径汇到同一个 ``SessionRecorder``：
- function tools：registry ``_hooked`` 在每个 ``FunctionTool.func`` 上调用 on_tool_start/end；
- **MCP 工具**：不走 FunctionTool.func —— 靠 ``MCPRecorderMiddleware.on_acting``（AgentScope
  middleware，包住 toolkit.call_tool 的**统一分发**，FunctionTool 与 MCP 共走）识别 ``mcp__``
  前缀工具后转同一 recorder（B6 首选路径；function 工具在 _hooked 已记，middleware 跳过防重复）。

sha 纪律（B7）：每条工具返回都过 ``add_shas`` 收集 40 位 hex（full sha）→ ``known_shas``。
run_diagnose 在 parse 结论后以 ``known_shas`` 跑 ``sanitize_hallucinated_shas``：plan 里出现的
任何 sha 必须是某次工具返回里出现过的，否则字段清空 + 降 confidence。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncGenerator
from typing import Any

from agentscope.message import ToolCallBlock
from agentscope.middleware import MiddlewareBase

from ..tools.progress import ToolCallEvent
from .session import Session

log = logging.getLogger("aidiag.diag.recorder")

# full sha（40 hex）：git rev-parse/log 的完整 commit id 权威格式
_FULL_SHA_RE = re.compile(r"\b[0-9a-fA-F]{40}\b")


class SessionRecorder:
    """把工具调用事件写进 session.tool_calls + 收集工具返回中的 full sha。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._open: dict[str, ToolCallEvent] = {}
        self.known_shas: set[str] = set()

    def on_tool_start(self, tool: str, args: dict[str, Any]) -> None:
        self._open[tool] = ToolCallEvent(tool=tool, args=args or {}, status="started")

    def on_tool_end(self, tool: str, result: Any, ok: bool) -> None:
        self.add_shas(result)
        ev = self._open.pop(tool, ToolCallEvent(tool=tool, status="error"))
        ev.status = "ok" if ok else "error"
        ev.result_summary = _summarize(result, 300)
        self.session.tool_calls.append(ev)
        self.session.touch()

    def add_shas(self, result: Any) -> None:
        """递归收集工具返回里的 40 位 hex（git commit full sha 的权威来源）。"""
        found: list[str] = []
        stack = [result]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, (list, tuple, set)):
                stack.extend(node)
            elif isinstance(node, str):
                found.extend(re.findall(_FULL_SHA_RE, node))
        self.known_shas.update(found)


def _summarize(result: Any, limit: int) -> str:
    """把工具返回压成一小段摘要，避免 /status 无限膨胀。"""
    if result is None:
        return ""
    if isinstance(result, dict):
        parts: list[str] = []
        keys = ("error", "log_lines", "pods", "events", "series", "latest", "result", "content")
        for key in keys:
            if key in result:
                v = result[key]
                parts.append(f"{key}={v if isinstance(v, str) else str(v)[:limit]}")
        return "; ".join(parts)[:limit] if parts else str(result)[:limit]
    return str(result)[:limit]


def _parse_input(input_val: Any) -> dict[str, Any]:
    """tool_call.input 可能是 dict 或 JSON 字符串，宽容归一为 dict。"""
    if isinstance(input_val, dict):
        return input_val
    if isinstance(input_val, str) and input_val.strip():
        try:
            parsed = json.loads(input_val)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _result_text(last: Any) -> dict[str, Any]:
    """从工具执行末对象（ToolResponse）content blocks 提取文本，供摘要/sha 收集。"""
    parts: list[str] = []
    for block in getattr(last, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return {"result": "\n".join(parts)}


class MCPRecorderMiddleware(MiddlewareBase):
    """on_acting 中间件：只记 MCP 工具（``mcp__`` 前缀）到 SessionRecorder。

    function 工具已在 registry._hooked 记录；这里跳过它们避免双记。对 MCP 工具在放行执行
    前后回调同一 recorder，使原生 MCP 调用出现在 /status 且其返回里的 sha 进入 known_shas。
    """

    def __init__(self, recorder: SessionRecorder) -> None:
        self.recorder = recorder

    async def on_acting(
        self, agent: Any, input_kwargs: dict, next_handler: Any
    ) -> AsyncGenerator:
        tc: ToolCallBlock | None = input_kwargs.get("tool_call")
        name = getattr(tc, "name", None)
        if not (name and str(name).startswith("mcp__")):
            async for item in next_handler():
                yield item
            return
        args = _parse_input(getattr(tc, "input", None))
        last = None
        self.recorder.on_tool_start(name, args)
        try:
            async for item in next_handler():
                last = item
                yield item
        finally:
            state = getattr(last, "state", None)
            state_str = str(state).split(".")[-1].lower() if state is not None else "success"
            ok = state_str not in ("error", "denied", "exception")
            self.recorder.on_tool_end(name, _result_text(last), ok)

    def get_middleware_key(self) -> str:
        return "MCPRecorderMiddleware"
