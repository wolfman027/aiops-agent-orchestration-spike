"""fetch_strategy 工具（M3 证据驱动取 runbook，DESIGN §6）。

runbook 默认**不预烤**进 system prompt（build_system_prompt strategy=None）；agent 取到首条
证据、判定错误类别后，命中某触发签名才调 ``fetch_strategy(name)`` 把对应 .j2 正文拉进来当
工具结果。名字即目录：本工具的 description 用 ``catalog_text(case_type)`` 按 case_type 过滤
后的「name —— 触发签名」清单；allowlist 同步按 case_type 过滤，trace_code 上下文取不到 k8s
的 crashloop。非法名 → 错误返回 + 可用清单（工具错误纪律：模型编名字能自纠）。
"""

from __future__ import annotations

from typing import Any

from ..prompts import render_runbook
from ..prompts.runbook_registry import RUNBOOKS, catalog_text, names_for_case_type
from .registry import ToolSpec


def build_strategy_spec(case_type: str | None = None) -> ToolSpec:
    """构造绑定到某 case_type 的 fetch_strategy 工具。

    case_type=None → 全量 allowlist（测试 / 明确跨场景时用）；trace_code / k8s → 只暴露
    各自候选 runbook，description 与 allowlist 一致。
    """
    allow = names_for_case_type(case_type)

    async def fetch_strategy(name: str) -> dict[str, Any]:
        """取一份排查策略 runbook：传 name（可用清单见工具描述）。命中则正文当本工具结果。"""
        runbook = RUNBOOKS.get(name)
        if runbook is None or name not in allow:
            return {
                "found": False,
                "error": f"未知或不适用于当前场景的 runbook {name!r}",
                "available": allow,
                "query": {"name": name, "case_type": case_type},
            }
        return {
            "found": True,
            "strategy": runbook.name,
            "summary": runbook.summary,
            "runbook": render_runbook(runbook),
            "note": "按这份 runbook 推进排查；取到的正文只在本次调查有效。",
        }

    return ToolSpec(
        name="fetch_strategy",
        description=(
            "【证据驱动取排查 runbook】拿到首条证据、判定错误类别后，若命中某个 runbook 的"
            "触发签名就调它，把对应排查步骤拉进来照着执行。不要凭想象硬拉，未命中就不取。"
            "可用清单：\n" + catalog_text(case_type)
        ),
        func=fetch_strategy,
    )
