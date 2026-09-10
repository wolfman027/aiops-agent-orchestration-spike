"""FastAPI：Stage1 只读诊断 + Stage2 计划级审批。

Stage1（只读，终点 = 结构化结论含 recommended_fix = 有序的互斥修复方案）：
- ``POST /diagnose``：通用/人工门（来源=manual）。立即返回 ``{session_id, status: analyzing}``。
- ``POST /diagnose/logs``：日志采集门（来源=log）。体给"日志摘录 + 定位键"，title 自动生成。
- ``POST /diagnose/metrics``：指标采集门（来源=metric）。体给"指标告警 + 定位键"。
  三个门都汇入同一 ``_start()`` 脊柱，后端诊断流程完全一致（按 case_type 装配）。
- ``GET /status/{id}``：返回 ``{status, trigger, tasks, tool_calls, conclusion?, error?, ...}``。
- ``POST /stop/{id}``（可选）：取消仍在跑的后台诊断任务。

Stage2（计划级审批：approve 是终态签章，**零执行**）：
- ``POST /remediate/{id}``：选定一个方案（可选体 ``{"option_index": N}``，1-based；省略 → 默认
  recommended 方案），把该方案的 steps 快照为待审批计划。
- ``POST /approve/{id}``：approve → plan/session 置 approved（无下游动作，不真改代码/环境）；
  reject+feedback → 并入上下文重跑诊断；重跑次数 ≥ max → closed_manual（终态）。
- ``POST /dismiss/{id}``：忽略/误报 → 会话签为终态 ``dismissed``（不改 conclusion、不重跑、不碰 tasks）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import get_settings
from .diag.runner import run_diagnose
from .diag.session import (
    RemediationPlan,
    Session,
    SessionStore,
    finalize_terminal_tasks,
)
from .domain import DeployedRef, Issue, MetricAlert, preferred_option_index
from .llm import build_model
from .mcp.manager import MCPClientManager
from .mcp.profiles import build_seed_rows, servers_for_case_type
from .mcp.store import MCPStore
from .tools.datasources import build_datasources, crashloop_env
from .tools.registry import bind_tools
from .tools.strategies import build_strategy_spec

log = logging.getLogger("aidiag.api")

# ---- 应用级状态（单进程；spike 用模块全局） ----
STORE = SessionStore(ttl_seconds=get_settings().session_ttl_seconds)
_tasks: dict[str, asyncio.Task] = {}


async def _resolve_specs_for(issue: Issue):
    """按 case_type 解析本 issue 的 function specs + MCP client + allow 名单（B4/B5）。

    - k8s（legacy）：mock crashloop 数据源绑定（PROFILES["k8s"]=空，不绑 MCP）；
    - trace_code：fetch_strategy(case_type)，并拉起该 profile 命中的 MCP client（git/app-log）
      及其工具精确名（allow 注入）。仓库 HEAD 不再由本地 function 工具提供——统一走 git MCP 的
      仓库状态工具（真 server ``get_repo_status`` / mock ``git_status``），仓库定位参数用
      ``issue.repo``（见 runner.compose_user_content 的提示）。
    """
    settings = get_settings()
    if issue.case_type == "k8s":
        env = crashloop_env()
        return bind_tools(env, build_datasources(env)), [], []
    # 角色 → 实际 seed 行名（env 给 url → 真 server 名，否则 mock 角色名 git/app-log）
    names = servers_for_case_type(issue.case_type, settings)
    func_specs = [build_strategy_spec(case_type=issue.case_type)]
    manager: MCPClientManager = app.state.mcp
    mcp_clients = await manager.clients_for(names)
    allow_extra = await manager.allow_names_for(names)
    return func_specs, mcp_clients, allow_extra


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.model = build_model(settings)
    # MCP 控制面：seed（真 http url 优先，否则本地 stdio mock）+ manager（建 client 不预连接）
    store = MCPStore()
    await store.seed_if_empty(build_seed_rows(settings))
    manager = MCPClientManager(store)
    await manager.load()
    app.state.mcp_store = store
    app.state.mcp = manager
    app.state.store = STORE
    app.state.tasks = _tasks
    yield
    for t in _tasks.values():
        t.cancel()
    await manager.close_all()


app = FastAPI(title="aidiag", version="0.1.0", lifespan=lifespan)


class DeployedRefIn(BaseModel):
    """线上基线（deployed commit/image），与 domain.DeployedRef 同构。"""

    kind: Literal["commit", "image"] = "commit"
    value: str = ""
    source: Literal["platform", "log_meta", "git_tag"] = "platform"


class DiagnoseRequest(BaseModel):
    title: str
    description: str = ""
    namespace: str = ""
    strategy: str | None = None
    # trace_code 定位键（只带"去哪查 + 基线"，绝无 bug 签名/根因方向）
    case_type: Literal["trace_code", "k8s"] = "k8s"
    app: str | None = None  # 日志源身份（agent 第一跳）
    repo: str | None = None  # git 仓库语义标识
    trace_id: str | None = None  # 日志关联键（强过滤）
    environment: str | None = None
    time_window: str | None = None
    deployed: DeployedRefIn | None = None


class LogTriggerRequest(BaseModel):
    """日志采集门。调用方只给"日志摘录 + 去哪查 + 基线"，title 由系统生成。

    ``log_excerpt`` 只给异常 message，**不含 ``at`` 栈帧**——保留 agent 自己 git grep/git show
    定位 file:line 的价值。它是"提示、须查证"，不是结论（见 runner.compose_user_content）。
    """

    log_excerpt: str  # 异常 message（建议只给消息、不含 at 栈帧）
    app: str  # 日志源身份（必填：不给不知道查谁）
    repo: str | None = None
    trace_id: str | None = None
    environment: str | None = None
    time_window: str | None = None
    deployed: DeployedRefIn | None = None


class MetricTriggerRequest(BaseModel):
    """指标采集门。告警内容 + 去哪查 + 基线。"""

    metric: str
    resource: str  # 资源（pod/服务/deployment）
    value: str = ""
    threshold: str = ""
    description: str = ""  # 告警原文（可选）
    app: str | None = None  # 省略时回退 resource
    repo: str | None = None
    environment: str | None = None
    time_window: str | None = None
    deployed: DeployedRefIn | None = None


def _deployed_from(inp: DeployedRefIn | None) -> DeployedRef | None:
    """线上基线统一构造：value 为空白 → None（其他门复用，避免三处重复）。"""
    if inp is not None and inp.value.strip():
        return DeployedRef(kind=inp.kind, value=inp.value.strip(), source=inp.source)
    return None


def _issue_from(req: DiagnoseRequest) -> Issue:
    return Issue(
        case_type=req.case_type,
        title=req.title,
        description=req.description,
        app=req.app,
        repo=req.repo,
        trace_id=req.trace_id,
        environment=req.environment,
        time_window=req.time_window,
        deployed=_deployed_from(req.deployed),
        namespace=req.namespace,
        strategy=req.strategy,
    )


def _issue_from_log(req: LogTriggerRequest) -> Issue:
    """日志采集门 → Issue。门即来源，case_type 固定 trace_code（要 k8s 走 /diagnose）。"""
    locator = req.trace_id or req.time_window or ""
    return Issue(
        case_type="trace_code",
        trigger="log",
        title=f"[log] {req.app} {locator}".strip(),
        app=req.app,
        repo=req.repo,
        trace_id=req.trace_id,
        environment=req.environment,
        time_window=req.time_window,
        deployed=_deployed_from(req.deployed),
        log_excerpt=req.log_excerpt.strip(),
    )


def _issue_from_metric(req: MetricTriggerRequest) -> Issue:
    """指标采集门 → Issue。app 省略时回退 resource（服务身份即日志源）。"""
    return Issue(
        case_type="trace_code",
        trigger="metric",
        title=f"[metric] {req.resource} {req.metric}".strip(),
        app=req.app or req.resource,
        repo=req.repo,
        environment=req.environment,
        time_window=req.time_window,
        deployed=_deployed_from(req.deployed),
        metric_alert=MetricAlert(
            metric=req.metric,
            value=req.value,
            threshold=req.threshold,
            resource=req.resource,
            description=req.description,
        ),
    )


def _start(issue: Issue) -> dict[str, Any]:
    """三个门的共享脊柱：落 session + 投后台诊断，返回轮询入口。"""
    session = STORE.create(issue)
    _schedule_run(session)
    return {"session_id": session.id, "status": session.status}


@app.post("/diagnose")
async def diagnose(req: DiagnoseRequest) -> dict[str, Any]:
    """通用/人工门（来源=manual）：保留既有字段形状，服务测试/golden/手工。"""
    return _start(_issue_from(req))


@app.post("/diagnose/logs")
async def diagnose_logs(req: LogTriggerRequest) -> dict[str, Any]:
    """日志采集门（来源=log）。"""
    return _start(_issue_from_log(req))


@app.post("/diagnose/metrics")
async def diagnose_metrics(req: MetricTriggerRequest) -> dict[str, Any]:
    """指标采集门（来源=metric）。"""
    return _start(_issue_from_metric(req))


@app.get("/status/{session_id}")
async def status(session_id: str) -> dict[str, Any]:
    snap = STORE.snapshot(session_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="session not found or expired")
    return snap


@app.post("/stop/{session_id}")
async def stop(session_id: str) -> dict[str, Any]:
    task = _tasks.get(session_id)
    session = STORE.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found or expired")
    if task is not None and not task.done():
        task.cancel()
    if session.status == "analyzing":
        session.status = "failed"
        session.error = "stopped by user"
        finalize_terminal_tasks(session, ok=False)
        session.touch()
    return {"session_id": session_id, "status": session.status}


# ======================================================================
# Stage2：计划级审批（v4 §8/§13）
# ======================================================================
def _schedule_run(session: Session) -> None:
    """把一次诊断投到后台任务（首次 diagnose / 驳回重跑共用）。

    spec/MCP 解析（含 stateful client 拉起）在后台做，不阻塞 POST；解析失败也要把 session
    置 failed，避免永远卡在 analyzing。
    """

    async def _run() -> None:
        try:
            func_specs, mcp_clients, allow_extra = await _resolve_specs_for(session.issue)
            await run_diagnose(
                session,
                app.state.model,
                func_specs,
                strategy=session.issue.strategy,
                max_iters=get_settings().max_iters,
                mcp_clients=mcp_clients,
                allow_extra=allow_extra,
            )
        except Exception as exc:  # noqa: BLE001 —— 解析/拉起失败也要落 failed 态
            log.warning("resolve/run failed session=%s: %s", session.id, exc)
            session.status = "failed"
            session.error = str(exc)[:2000]
            finalize_terminal_tasks(session, ok=False)
            session.touch()
        finally:
            _tasks.pop(session.id, None)

    _tasks[session.id] = asyncio.create_task(_run())


class RemediateRequest(BaseModel):
    """方案选择器。option_index 为 1-based（= 方案1/方案2）；省略 → 默认方案。"""

    option_index: int | None = None


@app.post("/remediate/{session_id}")
async def remediate(session_id: str, req: RemediateRequest | None = None) -> dict[str, Any]:
    """把**选中的**一个方案快照为待审批计划（approve 仍是对整份 plan 的终态签章）。

    选方案：显式 option_index（1-based）优先；省略 → 默认（recommended，再退方案1）。
    """
    session = STORE.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found or expired")
    if session.status != "completed" or session.conclusion is None:
        raise HTTPException(status_code=409, detail="诊断未完成，无法提交修复计划")
    options = session.conclusion.recommended_fix
    if not options:
        raise HTTPException(status_code=409, detail="结论没有 recommended_fix，无法提交")

    if req is not None and req.option_index is not None:
        sel = req.option_index
        if sel < 1 or sel > len(options):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "option_index out of range",
                    "requested": sel,
                    "available": [
                        {"index": i + 1, "title": o.title, "recommended": o.recommended}
                        for i, o in enumerate(options)
                    ],
                },
            )
        idx = sel - 1
    else:
        idx = preferred_option_index(options)
        assert idx is not None  # options 非空 → 必非 None

    chosen = options[idx]
    if not chosen.steps:
        raise HTTPException(status_code=409, detail=f"方案{idx + 1} 没有 steps，无法提交")

    session.remediation = RemediationPlan(
        steps=list(chosen.steps),  # 只快照选中方案的 steps
        status="pending_review",
        option_index=idx + 1,
        option_title=chosen.title,
    )
    session.touch()
    return {
        "session_id": session_id,
        "remediation_status": session.remediation.status,
        "option_index": session.remediation.option_index,
        "option_title": session.remediation.option_title,
        "steps": [s.model_dump() for s in session.remediation.steps],
    }


class ApproveRequest(BaseModel):
    decision: Literal["approve", "reject"] = "approve"
    feedback: str = ""


@app.post("/approve/{session_id}")
async def approve(session_id: str, req: ApproveRequest) -> dict[str, Any]:
    session = STORE.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found or expired")
    plan = session.remediation
    if plan is None:
        raise HTTPException(status_code=409, detail="没有待审批计划，请先 POST /remediate")
    if plan.status != "pending_review":
        raise HTTPException(status_code=409, detail=f"计划已处于 {plan.status}")

    if req.decision == "approve":
        plan.status = "approved"
        plan.decided_at = time.time()
        session.status = "approved"  # 终态签章：无下游动作（不执行 plan.steps）
    else:
        if not req.feedback.strip():
            raise HTTPException(status_code=422, detail="驳回必须带 feedback")
        plan.status = "rejected"
        plan.last_feedback = req.feedback
        plan.decided_at = time.time()
        session.feedbacks.append(req.feedback)
        session.reanalyze_count += 1
        if session.reanalyze_count >= get_settings().max_reanalyze:
            plan.status = "closed_manual"
            session.status = "closed_manual"
            session.error = (
                f"驳回重跑已达上限（{session.reanalyze_count}/{get_settings().max_reanalyze}），"
                "需人工处理"
            )
        else:
            _schedule_run(session)  # feedback 已并入 → 后台重跑诊断
    session.touch()
    return {
        "session_id": session_id,
        "session_status": session.status,
        "remediation_status": plan.status,
        "reanalyze_count": session.reanalyze_count,
        "max_reanalyze": get_settings().max_reanalyze,
        "note": "approve 为终态签章，不执行任何改动",
    }


class DismissRequest(BaseModel):
    reason: str = ""


@app.post("/dismiss/{session_id}")
async def dismiss(session_id: str, req: DismissRequest | None = None) -> dict[str, Any]:
    """忽略/误报：把会话签为终态 ``dismissed``（不改 conclusion、不重跑、不碰 tasks）。

    与 ``/approve`` 的 approve 不同——它是"人已判定这条不用做"的**终态归档**：
    - 只接受已收敛的会话（``completed``/``failed``/``closed_manual``）；``analyzing`` 正在跑 → 409
      （先 ``/stop``）；已 ``dismissed`` → 409（语义明确，不静默成功）。
    - **不调 ``finalize_terminal_tasks``**：dismiss 不重跑，``tasks`` 在 ``completed`` 时已归一
      （或本就是 ``failed`` 的 cancelled），再归一反而会把 cancelled 弄脏。
    - reason 复用现成的 ``session.feedbacks`` 记录（不为一个字符串新加字段）。
    """
    session = STORE.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found or expired")
    if session.status not in ("completed", "failed", "closed_manual"):
        raise HTTPException(
            status_code=409,
            detail=f"会话处于 {session.status}，不可忽略（仅 completed/failed/closed_manual 可忽略）",
        )
    if req and req.reason.strip():
        session.feedbacks.append(req.reason.strip())
    session.status = "dismissed"
    session.touch()
    return {"session_id": session_id, "status": session.status}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
