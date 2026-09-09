"""提示词渲染（v4 §7 诊断引导三层 + TRACE_CODE_DESIGN §6 M3 runbook 机制）。

- 7.1 默认方法论（常驻）：``methodology.j2``
- 7.3 场景策略 runbook：M3 = **默认不预烤**，跑动中经 ``fetch_strategy`` 取（证据驱动）；
  M1 显式覆盖路径（``issue.strategy`` / golden）仍预烤。
- 输出 JSON 契约：``conclusion.j2``（常驻，约定最终结论 schema）
- runbook 单一来源：``runbook_registry.py``（name → .j2 → case_type 归属）

7.1 + 契约永远在场；runbook 只在显式覆盖时预烤，否则靠 fetch_strategy。
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .runbook_registry import RUNBOOKS, Runbook

_PROMPT_DIR = Path(__file__).resolve().parent

_env = Environment(
    loader=FileSystemLoader(str(_PROMPT_DIR)),
    undefined=StrictUndefined,
    autoescape=False,
)


def render(template_name: str, **ctx: object) -> str:
    """渲染 prompts/ 下的 .j2 模板。"""
    return _env.get_template(template_name).render(**ctx)


def build_system_prompt(strategy: str | None = None, planning: bool = False) -> str:
    """组装常驻方法论 + 输出契约 + 自规划 + （仅显式时）runbook 预烤。

    M3：``strategy`` 为 None（trace_code 默认）→ 不预烤任何 runbook，靠跑动中
    ``fetch_strategy``；只有显式 ``issue.strategy``（golden / 确定场景）才预烤。
    """
    parts = [render("methodology.j2"), render("conclusion.j2")]
    if planning:
        parts.append(render("planning.j2"))
    if strategy:
        spec = RUNBOOKS.get(strategy)
        if spec is None:
            raise KeyError(
                f"未知 runbook {strategy!r}；可用: {sorted(RUNBOOKS)}"
            )
        parts.append(render(spec.template))
    return "\n\n".join(parts)


def render_runbook(runbook: Runbook, **ctx: object) -> str:
    """渲染单个 runbook 正文（fetch_strategy 工具用；校验已由调用方做）。"""
    return render(runbook.template, **ctx)

