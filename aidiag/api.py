"""FastAPI：Stage1 只读诊断 + Stage2 计划级审批。

Stage1（只读，终点 = 结构化结论含 recommended_fix）：
- ``POST /diagnose``：立即返回 ``{session_id, status: analyzing}``，后台 asyncio 任务跑诊断。
- ``GET /status/{id}``：返回 ``{status, tasks, tool_calls, conclusion?, error?, ...}``（轮询用）。
- ``POST /stop/{id}``（可选）：取消仍在跑的后台诊断任务。

Stage2（计划级审批：approve 是终态签章，**零执行**）：
- ``POST /remediate/{id}``：把结论的 recommended_fix 提交为待审批计划。
- ``POST /approve/{id}``：approve → plan/session 置 approved（无下游动作，不真改代码/环境）；
  reject+feedback → 并入上下文重跑诊断；重跑次数 ≥ max → closed_manual（终态）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import get_settings
from .diag.runner import run_diagnose
from .diag.session import Session, SessionStore
from .domain import DeployedRef, Issue
from .llm import build_model
from .mcp.manager import MCPClientManager
from .mcp.profiles import build_seed_rows, servers_for_case_type
from .mcp.store import MCPStore
from .tools.datasources import build_datasources, crashloop_env
from .tools.registry import bind_tools
from .tools.repo_state import build_repo_state_spec
from .tools.strategies import build_strategy_spec

log = logging.getLogger("aidiag.api")

# ---- 应用级状态（单进程；spike 用模块全局） ----
STORE = SessionStore(ttl_seconds=get_settings().session_ttl_seconds)
_tasks: dict[str, asyncio.Task] = {}


async def _resolve_specs_for(issue: Issue):
    """按 case_type 解析本 issue 的 function specs + MCP client + allow 名单（B4/B5）。

    - k8s（legacy）：mock crashloop 数据源绑定（PROFILES["k8s"]=空，不绑 MCP）；
    - trace_code：fetch_strategy(case_type) + repo_state（绑定默认工作区 = 与 git mock 同一
      仓库），并拉起该 profile 命中的 MCP client（git/app-log）及其工具精确名（allow 注入）。
    repo_state 在 resolve 期绑定固定 cwd，agent 拿不到任意路径（不给注入面）。
    """
    settings = get_settings()
    if issue.case_type == "k8s":
        env = crashloop_env()
        return bind_tools(env, build_datasources(env)), [], []
    # 角色 → 实际 seed 行名（env 给 url → 真 server 名，否则 mock 角色名 git/app-log）
    names = servers_for_case_type(issue.case_type, settings)
    func_specs = [build_strategy_spec(case_type=issue.case_type)]
    # app→repo 收敛单仓库（spike）；issue.repo 保留为语义标识
    func_specs.append(build_repo_state_spec(Path(settings.repo_cwd)))
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


def _issue_from(req: DiagnoseRequest) -> Issue:
    deployed = None
    if req.deployed is not None and req.deployed.value.strip():
        deployed = DeployedRef(
            kind=req.deployed.kind,
            value=req.deployed.value.strip(),
            source=req.deployed.source,
        )
    return Issue(
        case_type=req.case_type,
        title=req.title,
        description=req.description,
        app=req.app,
        repo=req.repo,
        trace_id=req.trace_id,
        environment=req.environment,
        time_window=req.time_window,
        deployed=deployed,
        namespace=req.namespace,
        strategy=req.strategy,
    )


@app.post("/diagnose")
async def diagnose(req: DiagnoseRequest) -> dict[str, Any]:
    session = STORE.create(_issue_from(req))
    _schedule_run(session)
    return {"session_id": session.id, "status": session.status}


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
            session.touch()
        finally:
            _tasks.pop(session.id, None)

    _tasks[session.id] = asyncio.create_task(_run())


@app.post("/remediate/{session_id}")
async def remediate(session_id: str) -> dict[str, Any]:
    session = STORE.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found or expired")
    if session.status != "completed" or session.conclusion is None:
        raise HTTPException(status_code=409, detail="诊断未完成，无法提交修复计划")
    if not session.conclusion.recommended_fix:
        raise HTTPException(status_code=409, detail="结论没有 recommended_fix，无法提交")
    from .diag.session import RemediationPlan

    session.remediation = RemediationPlan(
        steps=session.conclusion.recommended_fix,
        status="pending_review",
    )
    session.touch()
    return {
        "session_id": session_id,
        "remediation_status": session.remediation.status,
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
