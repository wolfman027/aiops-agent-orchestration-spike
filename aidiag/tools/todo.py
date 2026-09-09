"""自规划工具（Holmes TodoWrite 等价物，v4 §7.2）。

``plan_investigation`` 创建排查任务清单，``complete_task`` 逐步标记完成。
清单实时反映到 session.tasks → ``/status`` 当进度。工具是异步、只读（不改环境，
只更新会话内任务状态），故仍是 read_only=True。
"""

from __future__ import annotations

from typing import Any

from ..diag.session import InvestigationTask, Session
from .registry import ToolSpec


def build_todo_specs(session: Session) -> list[ToolSpec]:
    """构造绑定到某 session 的 todo 工具（带显式签名闭包）。"""

    async def plan_investigation(title: str, steps: list[str]) -> dict[str, Any]:
        """排查前必须调用：给本次调查起个标题并列出 ≥2 的待办步骤，建立任务清单。"""
        session.tasks.clear()
        for i, step in enumerate(steps, start=1):
            session.tasks.append(InvestigationTask(id=f"t{i}", title=step, status="todo"))
        session.touch()
        return {
            "ok": True,
            "planned": len(session.tasks),
            "tasks": [t.model_dump() for t in session.tasks],
            "note": "每完成一步请调用 complete_task 标记。",
        }

    async def complete_task(task_id: str) -> dict[str, Any]:
        """标记一个计划任务为已完成（task_id 来自 plan_investigation 返回）。"""
        for t in session.tasks:
            if t.id == task_id:
                t.status = "done"
                session.touch()
                return {"ok": True, "task_id": task_id, "status": "done"}
        return {
            "ok": False,
            "error": f"未知任务 {task_id}；当前任务: {[t.id for t in session.tasks]}",
            "query": {"task_id": task_id},
        }

    return [
        ToolSpec(
            name="plan_investigation",
            description=(
                "【调查启动必调】为本次诊断创建任务清单（title + steps: 2-8 个字符串步骤）。"
                "凡是需要 ≥2 次工具查询的调查，先用它规划，再逐项执行。"
            ),
            func=plan_investigation,
        ),
        ToolSpec(
            name="complete_task",
            description="把 plan_investigation 建出的某任务标记为已完成（task_id 必填）。",
            func=complete_task,
        ),
    ]
