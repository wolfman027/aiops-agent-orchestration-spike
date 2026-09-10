"""诊断 runner：后台跑一次只读诊断，把进度/tasks/tool_calls/结论写回 session。

``Agent.reply`` 一次调用跑完整 ReAct 循环 → 无法中途流式取状态；故 recorder（SessionRecorder，
见 recorder.py）在每个工具调用前后写 session.tool_calls，plan_investigation/complete_task
同步更新 session.tasks，REST ``/status`` 轮询读取。

B6 recorder 分两路汇到同一个 SessionRecorder：
- function 工具：registry ``_hooked`` 在每个 FunctionTool.func 上回调；
- MCP 工具（``mcp__`` 前缀）：MCPRecorderMiddleware.on_acting 记录（function 已记，跳过防重）。
本 runner 在 ``mcp_clients`` 非空时把该 middleware 挂上 agent。

驳回重跑（Stage2）：把 feedback 并入下一轮 user 上下文重新诊断，``reanalyze_count`` 计入
session；达上限由 API 层置 closed_manual。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from agentscope.model import ChatModelBase

from ..agents import build_agent, run_agent
from ..domain import Issue, conclusion_from_dict, sanitize_hallucinated_shas
from ..prompts import build_system_prompt
from ..tools.registry import ToolSpec
from ..tools.todo import build_todo_specs
from .recorder import MCPRecorderMiddleware, SessionRecorder
from .session import Session, finalize_terminal_tasks

log = logging.getLogger("aidiag.runner")


def compose_user_content(issue: Issue, feedbacks: list[str] | None = None) -> str:
    """把 Issue 渲染成 user 上下文。默认（manual）只带定位键，绝不带根因方向。

    输出"去哪查 + 基线"：app/repo/trace_id/environment/time_window/deployed；线上基线
    （deployed commit/image）是给 agent 的对齐锚，仍不是答案。

    例外：log/metric 门会把触发源的证据（日志摘录 / 指标告警）一并带上——那是**触发源给的
    提示，须查证**，不是结论。渲染时显式标明"仅供参考 + 须用工具核对 + 出处记 trigger_log"，
    避免模型复述摘录收口（丢掉定位 file:line / 读代码这跳）。
    """
    lines: list[str] = [f"场景(case_type): {issue.case_type}"]
    if issue.namespace:
        lines.append(f"命名空间: {issue.namespace}")
    if issue.app:
        lines.append(f"应用(app): {issue.app}")
    if issue.repo:
        lines.append(
            f"仓库(repo): {issue.repo}"
            "（git 只读工具若需要仓库定位参数——如真 server 的 repo_path——一律用此值）"
        )
    if issue.trace_id:
        lines.append(f"trace_id: {issue.trace_id}")
    if issue.environment:
        lines.append(f"环境: {issue.environment}")
    if issue.time_window:
        lines.append(f"时间窗: {issue.time_window}")
    if issue.deployed is not None:
        d = issue.deployed
        lines.append(f"线上基线(deployed): {d.kind}={d.value}（来源={d.source}）")
    if issue.log_excerpt:
        lines.append(
            "触发日志摘录（来自监控系统，**仅供参考**）：\n"
            f"{issue.log_excerpt}\n"
            "注意：这是摘录、不是结论。请用日志工具取该 trace 的完整日志与栈帧核对，定位到确切 "
            "file:line 并阅读代码后再下结论；结论若引用此摘录，evidence.source 记 trigger_log。"
        )
    if issue.metric_alert is not None:
        m = issue.metric_alert
        lines.append(
            f"指标告警: resource={m.resource} metric={m.metric} "
            f"value={m.value} threshold={m.threshold}"
        )
        if m.description:
            lines.append(f"告警原文: {m.description}")
        lines.append("请据此在时间窗内查该服务的日志与代码，定位指标异常的成因。")
    lines.append(f"标题: {issue.title}")
    if issue.description:
        lines.append(f"详情: {issue.description}")
    feedbacks = feedbacks or []
    if feedbacks:
        lines.append("\n以下是你**上一轮**结论被驳回的意见，请据此修正诊断与修复建议：")
        for i, fb in enumerate(feedbacks, start=1):
            lines.append(f"{i}. {fb}")
    lines.append("\n请诊断。")
    return "\n".join(lines)


async def run_diagnose(
    session: Session,
    model: ChatModelBase,
    specs: list[ToolSpec],
    *,
    strategy: str | None = None,
    planning: bool = True,
    max_iters: int = 10,
    name: str = "diagnostician",
    mcp_clients: list[Any] | None = None,
    allow_extra: list[str] | None = None,
    middlewares: Sequence[Any] = (),
) -> Session:
    """在传入 session 上执行一次诊断（可被 Stage2 驳回重跑复用）。

    - ``specs`` 是 resolve_specs 已解析出的 function 工具（含 fetch_strategy 绑定）；
    - ``mcp_clients``/``allow_extra``：API 层按 case_type profile 解析出的 MCP client 与其工具
      精确名（allow 注入），透传给 build_agent；
    - ``middlewares``：调用方额外挂的 AgentScope 中间件；若 mcp_clients 非空则补挂
      B6 ``MCPRecorderMiddleware``（function 工具已由 _hooked 记录，此中间件只记 mcp__ 工具）。

    成功 → status=completed + conclusion（结论过 sha 幻觉消毒，B7）；失败 → failed + error。
    """
    session.status = "analyzing"
    session.error = ""
    session.raw_reply = ""
    session.tasks.clear()
    session.tool_calls.clear()
    session.conclusion = None
    session.remediation = None  # 重跑后面向新 conclusion 的方案，旧 plan/选项不再适用

    all_specs = list(specs)
    if planning:
        all_specs.extend(build_todo_specs(session))

    recorder = SessionRecorder(session)
    mw: list[Any] = list(middlewares)
    if mcp_clients:
        mw.append(MCPRecorderMiddleware(recorder))
    system_prompt = build_system_prompt(
        strategy=strategy or session.issue.strategy, planning=planning
    )
    agent = build_agent(
        system_prompt,
        model,
        all_specs,
        name=name,
        recorder=recorder,
        max_iters=max_iters,
        mcp_clients=mcp_clients,
        allow_extra=allow_extra,
        middlewares=mw,
    )
    user = compose_user_content(session.issue, session.feedbacks)
    try:
        parsed, raw = await run_agent(agent, user)
        # 自动纠一次：未解析出 JSON 时，请模型只输出 JSON
        if not parsed:
            followup = (
                "你上一轮没有输出可解析的严格 JSON 结论。请不要调用任何工具、不要解释，"
                "只输出一个严格 JSON 对象（结构见结论契约）。"
            )
            parsed, raw = await run_agent(agent, followup)
        session.raw_reply = raw
        if not parsed:
            raise ValueError("模型回复未能解析出 JSON")
        session.conclusion = conclusion_from_dict(parsed)
        # B7：plan 里出现的任何 sha 必须来自工具返回；清掉幻觉 sha 并降 confidence
        sanitize_hallucinated_shas(session.conclusion, recorder.known_shas)
        session.status = "completed"
    except Exception as exc:  # noqa: BLE001
        log.exception("diagnose failed session=%s", session.id)
        session.error = str(exc)[:2000]
        session.status = "failed"
    # 终态归一：模型自报 tasks 与 session 终态对齐（结论走另一条通道，收尾步骤不会被
    # complete_task 标记；这里确定性收敛。失败态绝不置 done → cancelled）。
    finalize_terminal_tasks(session, ok=session.status == "completed")
    session.touch()
    return session
