"""Stage2 计划级审批的状态机（离线、无 LLM）。

覆盖 TRACE_CODE_DESIGN §1 计划审批模型：
- /remediate 前置（需 completed + 非空 recommended_fix）；
- /approve approve → 终态签章 approved（**零执行**，不产生 executions）；
- /approve reject+feedback → feedback 并入 + reanalyze_count+=1 → 重跑；
- reject 达 max_reanalyze → closed_manual（终态）；
- HTTP 层路由/错误码冒烟。

机制测试不真跑 AgentScope：把 api._schedule_run 替换成确定性同步 fake，
把 session 直接置回 completed 并换一个新结论（approve 内是同步调用它）。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import aidiag.api as api_mod
from aidiag.config import get_settings
from aidiag.diag.session import Session, SessionStore
from aidiag.domain import DiagnosticConclusion, EvidenceItem, FixOption, Issue, RemediationStep


def _conclusion() -> DiagnosticConclusion:
    return DiagnosticConclusion(
        root_cause="payment-checkout-db-conn-timeout-e9f2 checkout 启动连接 DB 超时",
        confidence="high",
        summary="describe_pod 见 exit_code=1；query_logs 见连接超时原文",
        evidence=[
            EvidenceItem(
                source="query_logs",
                query="payment/payment-checkout-6f9c8d7b5-xvz2p",
                finding="启动期数据库连接超时",
                supporting_text="code=payment-checkout-db-conn-timeout-e9f2 "
                "host=checkout-db.payment.svc.cluster.local:5432",
            )
        ],
        recommended_fix=[
            FixOption(
                title="改依赖地址",
                applies_when="日志与配置一致、依赖本身健康",
                recommended=True,
                reason="证据指向地址配错",
                steps=[
                    RemediationStep(
                        action="config_change",
                        target="Deployment checkout",
                        expected_effect="恢复启动",
                        risk="low",
                        rollback="回滚配置",
                    )
                ],
            )
        ],
    )


def _conclusion_multi(*, mark_second: bool = False, mark_none: bool = False) -> DiagnosticConclusion:
    """两个互斥方案：方案1 target=Deployment checkout / 方案2 target=Deployment payment-db。

    默认方案1 recommended；mark_second 反向标记；mark_none 两个都不标（测兜底）。
    """
    c = _conclusion()
    opt1 = FixOption(
        title="改 checkout 配置",
        applies_when="checkout 侧地址配错",
        recommended=not (mark_second or mark_none),
        reason="日志原文指向 checkout",
        steps=[
            RemediationStep(
                action="config_change", target="Deployment checkout", risk="low", rollback="回滚配置"
            )
        ],
    )
    opt2 = FixOption(
        title="重启依赖 Db",
        applies_when="依赖 Db 本身不健康",
        recommended=mark_second,
        reason="依赖方不健康则重启依赖",
        steps=[
            RemediationStep(
                action="restart", target="Deployment payment-db", risk="medium", rollback="无"
            )
        ],
    )
    return c.model_copy(update={"recommended_fix": [opt1, opt2]})


def _seed(store: SessionStore) -> Session:
    """造一个已完成且有 recommended_fix 的 session（等价于刚诊断完）。"""
    s = store.create(
        Issue(
            title="[P0] payment/checkout 反复 CrashLoopBackOff",
            description="Deployment checkout 0/1 可用",
            namespace="payment",
            strategy="crashloop",
        )
    )
    s.status = "completed"
    s.conclusion = _conclusion()
    s.touch()
    return s


def _fake_rerun_sync(session: Session) -> None:
    """确定性『重跑』：直接产出新结论并回到 completed。

    approve() 内以同步方式调用 _schedule_run；替换成同步函数即可在不真跑
    AgentScope 的情况下把 session 拨回可再次 /remediate 的状态。
    """
    session.status = "completed"
    session.error = ""
    session.conclusion = _conclusion()
    session.touch()


@pytest.fixture
def store(monkeypatch):
    st = SessionStore()
    monkeypatch.setattr(api_mod, "STORE", st)
    return st


@pytest.fixture
def sync_rerun(monkeypatch):
    """让驳回后的『重跑』走确定性 fake，而不是真实后台诊断任务。"""
    monkeypatch.setattr(api_mod, "_schedule_run", _fake_rerun_sync)


# ======================================================================
# /remediate 前置条件
# ======================================================================
@pytest.mark.asyncio
async def test_remediate_requires_completed_session(store):
    s = store.create(Issue(title="t", description="d", namespace="n"))
    s.status = "analyzing"  # 未完成
    with pytest.raises(HTTPException) as ei:
        await api_mod.remediate(s.id)
    assert ei.value.status_code == 409
    assert store.get(s.id).remediation is None


@pytest.mark.asyncio
async def test_remediate_requires_nonempty_fix(store):
    s = _seed(store)
    s.conclusion = _conclusion().model_copy(update={"recommended_fix": []})
    with pytest.raises(HTTPException) as ei:
        await api_mod.remediate(s.id)
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_remediate_submits_pending_review(store):
    s = _seed(store)
    out = await api_mod.remediate(s.id)
    assert out["remediation_status"] == "pending_review"
    assert out["option_index"] == 1
    assert len(out["steps"]) == 1
    assert out["steps"][0]["target"] == "Deployment checkout"
    # 计划记录选中的方案，供 /status 回显
    plan = store.get(s.id).remediation
    assert plan.option_index == 1
    assert plan.option_title == "改依赖地址"


# ======================================================================
# /remediate 选方案（默认 recommended / 显式 option_index / 边界）
# ======================================================================
@pytest.mark.asyncio
async def test_remediate_selects_recommended_by_default(store):
    s = _seed(store)
    s.conclusion = _conclusion_multi()  # 方案1 标记 recommended
    out = await api_mod.remediate(s.id)
    assert out["option_index"] == 1
    assert out["option_title"] == "改 checkout 配置"
    assert [st["target"] for st in out["steps"]] == ["Deployment checkout"]


@pytest.mark.asyncio
async def test_remediate_default_prefers_marked_second(store):
    s = _seed(store)
    s.conclusion = _conclusion_multi(mark_second=True)  # 方案2 标记 recommended
    out = await api_mod.remediate(s.id)
    assert out["option_index"] == 2
    assert [st["target"] for st in out["steps"]] == ["Deployment payment-db"]


@pytest.mark.asyncio
async def test_remediate_default_falls_back_to_first_when_none_marked(store):
    s = _seed(store)
    s.conclusion = _conclusion_multi(mark_none=True)  # 两个都未标记 → 兜底方案1
    out = await api_mod.remediate(s.id)
    assert out["option_index"] == 1


@pytest.mark.asyncio
async def test_remediate_selects_by_option_index(store):
    s = _seed(store)
    s.conclusion = _conclusion_multi()
    out = await api_mod.remediate(s.id, api_mod.RemediateRequest(option_index=2))
    assert out["option_index"] == 2
    assert out["option_title"] == "重启依赖 Db"
    assert [st["target"] for st in out["steps"]] == ["Deployment payment-db"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, 5])
async def test_remediate_invalid_option_index_4xx(store, bad):
    s = _seed(store)
    s.conclusion = _conclusion_multi()
    with pytest.raises(HTTPException) as ei:
        await api_mod.remediate(s.id, api_mod.RemediateRequest(option_index=bad))
    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert detail["requested"] == bad
    assert [o["index"] for o in detail["available"]] == [1, 2]
    assert all("title" in o and "recommended" in o for o in detail["available"])
    assert store.get(s.id).remediation is None  # 未落计划


@pytest.mark.asyncio
async def test_remediate_option_with_empty_steps_conflicts(store):
    s = _seed(store)
    empty = FixOption(title="空方案", recommended=True, steps=[])
    s.conclusion = _conclusion().model_copy(update={"recommended_fix": [empty]})
    with pytest.raises(HTTPException) as ei:
        await api_mod.remediate(s.id)
    assert ei.value.status_code == 409
    assert store.get(s.id).remediation is None


# ======================================================================
# approve → 终态签章 approved（零执行）
# ======================================================================
@pytest.mark.asyncio
async def test_approve_marks_terminal_signoff(store):
    s = _seed(store)
    await api_mod.remediate(s.id)
    out = await api_mod.approve(s.id, api_mod.ApproveRequest(decision="approve"))
    assert out["session_status"] == "approved"
    assert out["remediation_status"] == "approved"
    assert out["reanalyze_count"] == 0
    assert "executions" not in out  # 无执行器：不产生 execution 记录
    # 审批作用在 plan，不真改环境/代码
    s = store.get(s.id)
    assert s.status == "approved"
    assert s.remediation.status == "approved"
    assert s.remediation.steps  # 计划本身保留，供人查看


@pytest.mark.asyncio
async def test_approve_again_is_conflict(store):
    s = _seed(store)
    await api_mod.remediate(s.id)
    await api_mod.approve(s.id, api_mod.ApproveRequest(decision="approve"))
    with pytest.raises(HTTPException) as ei:
        await api_mod.approve(s.id, api_mod.ApproveRequest(decision="approve"))
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_approve_without_plan_is_conflict(store):
    s = _seed(store)
    with pytest.raises(HTTPException) as ei:
        await api_mod.approve(s.id, api_mod.ApproveRequest(decision="approve"))
    assert ei.value.status_code == 409


# ======================================================================
# approve reject：feedback 校验 + 并入 + 重跑
# ======================================================================
@pytest.mark.asyncio
async def test_reject_requires_feedback(store):
    s = _seed(store)
    await api_mod.remediate(s.id)
    with pytest.raises(HTTPException) as ei:
        await api_mod.approve(s.id, api_mod.ApproveRequest(decision="reject", feedback="  "))
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_reject_reruns_and_can_approve_later(store, sync_rerun):
    s = _seed(store)
    await api_mod.remediate(s.id)
    out = await api_mod.approve(
        s.id,
        api_mod.ApproveRequest(
            decision="reject", feedback="DB 运维确认 checkout-db 正常，请复核配置指向"
        ),
    )
    assert out["remediation_status"] == "rejected"
    assert out["reanalyze_count"] == 1
    s = store.get(s.id)
    assert s.feedbacks and "DB 运维确认" in s.feedbacks[-1]
    # 重跑把 session 拨回 completed → 可再次 /remediate
    assert s.status == "completed"
    assert s.conclusion is not None

    # 第 2 次驳回仍 < max_reanalyze → 继续重跑
    await api_mod.remediate(s.id)
    out2 = await api_mod.approve(
        s.id, api_mod.ApproveRequest(decision="reject", feedback="仍未覆盖 xxx")
    )
    assert out2["reanalyze_count"] == 2
    assert store.get(s.id).status == "completed"

    # 驳回 2 次后第 3 次提交直接 approve → 终态签章
    await api_mod.remediate(s.id)
    out3 = await api_mod.approve(s.id, api_mod.ApproveRequest(decision="approve"))
    assert out3["session_status"] == "approved"
    assert store.get(s.id).remediation.status == "approved"


@pytest.mark.asyncio
async def test_closed_manual_at_max_reanalyze(store, sync_rerun):
    max_rz = get_settings().max_reanalyze
    assert max_rz >= 1
    s = _seed(store)
    for i in range(max_rz - 1):
        await api_mod.remediate(s.id)
        out = await api_mod.approve(
            s.id, api_mod.ApproveRequest(decision="reject", feedback=f"第 {i + 1} 次驳回")
        )
        assert out["reanalyze_count"] == i + 1
        assert store.get(s.id).status == "completed"  # 还在重跑窗口内

    # 最后一次驳回把 count 推到 == max_reanalyze → closed_manual（终态，不再重跑）
    await api_mod.remediate(s.id)
    out = await api_mod.approve(
        s.id, api_mod.ApproveRequest(decision="reject", feedback=f"第 {max_rz} 次驳回")
    )
    assert out["reanalyze_count"] == max_rz
    assert out["session_status"] == "closed_manual"
    assert out["remediation_status"] == "closed_manual"
    s = store.get(s.id)
    assert "上限" in s.error
    assert len(s.feedbacks) == max_rz
    # 终态：不得再重跑或审批
    with pytest.raises(HTTPException) as ei:
        await api_mod.approve(s.id, api_mod.ApproveRequest(decision="reject", feedback="x"))
    assert ei.value.status_code == 409


# ======================================================================
# HTTP 层：路由/错误码冒烟（不启 uvicorn，用 TestClient 直连 app）
# ======================================================================
def test_http_remediate_approve_round_trip(store):
    s = _seed(store)
    with TestClient(api_mod.app) as client:
        r = client.post(f"/remediate/{s.id}")
        assert r.status_code == 200
        assert r.json()["remediation_status"] == "pending_review"

        r2 = client.post(f"/approve/{s.id}", json={"decision": "approve"})
        assert r2.status_code == 200
        body = r2.json()
        assert body["session_status"] == "approved"
        assert body["remediation_status"] == "approved"
        assert "executions" not in body

        # 已决策 → 409
        r3 = client.post(f"/approve/{s.id}", json={"decision": "approve"})
        assert r3.status_code == 409

        # 未知 session → 404
        assert client.get("/status/nope").status_code == 404
        assert client.post("/remediate/nope").status_code == 404


def test_http_remediate_select_option_round_trip(store):
    s = _seed(store)
    s.conclusion = _conclusion_multi()
    with TestClient(api_mod.app) as client:
        r = client.post(f"/remediate/{s.id}", json={"option_index": 2})
        assert r.status_code == 200
        assert r.json()["option_index"] == 2
        assert r.json()["option_title"] == "重启依赖 Db"

        # 越界 → 400（detail 带 requested + available）
        r2 = client.post(f"/remediate/{s.id}", json={"option_index": 9})
        assert r2.status_code == 400
        assert r2.json()["detail"]["requested"] == 9

    # /status 回显选中方案
    snap = store.snapshot(s.id)
    assert snap["remediation_option"] == {"index": 2, "title": "重启依赖 Db"}
