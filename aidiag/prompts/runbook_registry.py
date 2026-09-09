"""M3 runbook registry（TRACE_CODE_DESIGN §6/§7）：单一事实源，name → .j2 → case_type 归属。

同时服务三处，避免名字/模板漂移：
- ``build_system_prompt`` 的 M1 显式覆盖路径（``issue.strategy``）查 template 预烤；
- ``fetch_strategy(name)`` 工具（tools/strategies.py，Phase 2）校验 name + 渲染正文；
- ``fetch_strategy`` 的 tool description 当"目录"：按 case_type 过滤后的可用清单
  （name + 触发签名），模型靠它决定何时取哪个 runbook。

本模块**纯数据**（不 import prompts/__init__ 的 env/render，避免循环依赖）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Runbook:
    name: str
    template: str  # prompts/ 下相对模板文件名
    case_types: frozenset[str]
    trigger: str  # 触发证据签名（进 tool description 目录）
    summary: str = ""  # 一行简介（进 tool description 目录）


RUNBOOKS: dict[str, Runbook] = {
    # ---- k8s（M1 显式覆盖路径；trace_code 上下文不广告）----
    "crashloop": Runbook(
        name="crashloop",
        template="strategy_crashloop.j2",
        case_types=frozenset({"k8s"}),
        trigger="Pod CrashLoopBackOff / restart_count 持续上升 / 就绪失败",
        summary="Pod 反复重启的容器级排查（describe_pod + query_logs）",
    ),
    # ---- trace_code 首批（DESIGN §7；trace_bug 为兜底）----
    "trace_bug": Runbook(
        name="trace_bug",
        template="strategy_trace_bug.j2",
        case_types=frozenset({"trace_code"}),
        trigger="trace 有异常签名，不属依赖/启动特类",
        summary="（兜底）一条失败 trace → 日志异常原文 → 代码定位 → code_fix + 示意 diff",
    ),
    "trace_dependency": Runbook(
        name="trace_dependency",
        template="strategy_trace_dependency.j2",
        case_types=frozenset({"trace_code"}),
        trigger="connect/socket/pool 超时、connection refused、handshake 到 host 失败",
        summary="依赖连接失败（host:port 锚点 → 依赖挂 vs 配置错；target 可离开仓库）",
    ),
    "trace_startup": Runbook(
        name="trace_startup",
        template="strategy_trace_startup.j2",
        case_types=frozenset({"trace_code"}),
        trigger="应用起不来 / 反复重启 / 初始化即失败（用 app + 时间窗，可能无 trace）",
        summary="启动期失败（init/配置/连接池）→ 错误原文 → 启动代码定位",
    ),
}


def template_for(name: str) -> str | None:
    """runbook 名 → 模板文件名；未知返回 None（由调用方决定报错还是跳过）。"""
    r = RUNBOOKS.get(name)
    return r.template if r is not None else None


def names_for_case_type(case_type: str | None) -> list[str]:
    """按 case_type 过滤的可用 runbook 名（None → 全量）。"""
    return [r.name for r in RUNBOOKS.values() if not case_type or case_type in r.case_types]


def catalog_text(case_type: str | None = None) -> str:
    """给 fetch_strategy 的 tool description 当目录：name —— trigger（按 case_type 过滤）。"""
    lines = []
    for name in names_for_case_type(case_type):
        r = RUNBOOKS[name]
        lines.append(f"- {name}：{r.trigger}")
    return "\n".join(lines) if lines else "（无可用 runbook）"
