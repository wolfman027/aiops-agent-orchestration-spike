"""工具调用进度记录（供 /status 轮询）。

诊断跑在后台任务，``Agent.reply`` 内部跑完整 ReAct 循环，无法中途流式取状态；
因此 registry 在每个工具调用前后回调 ``ToolCallRecorder``，把事件写入 session sink，
REST ``/status`` 再从 sink 读。同步接口（在 async wrapper 内调用，开销小）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ToolCallEvent(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str = "started"  # started | ok | error
    result_summary: str = ""


@runtime_checkable
class ToolCallRecorder(Protocol):
    def on_tool_start(self, tool: str, args: dict[str, Any]) -> None: ...

    def on_tool_end(self, tool: str, result: dict[str, Any], ok: bool) -> None: ...


class NullRecorder:
    """无操作 recorder（不接 session 时用）。"""

    def on_tool_start(self, tool: str, args: dict[str, Any]) -> None:
        pass

    def on_tool_end(self, tool: str, result: dict[str, Any], ok: bool) -> None:
        pass
