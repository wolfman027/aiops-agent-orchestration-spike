"""终态"忽略/误报"门：``POST /dismiss/{id}``（离线，无 LLM）。

与 ``/approve`` 的 approve 不同——dismiss 是"人已判定这条不用做"的**终态归档**：
- 只接受已收敛的会话（completed/failed/closed_manual）；analyzing → 409；已 dismissed → 409；
- 不改 conclusion、不重跑、**不碰 tasks**（completed 的 derived-done 保持原样、
  failed 的 cancelled 不被改写）；reason 追加进现成的 ``session.feedbacks``。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import aidiag.api as api_mod
from aidiag.diag.session import InvestigationTask, Session, SessionStore, finalize_terminal_tasks
from aidiag.domain import DiagnosticConclusion, Issue

OK_CONCLUSION = {"root_cause": "db conn timeout", "confidence": "medium"}


def _seed(store: SessionStore, *, status: str) -> Session:
    s = store.create(Issue(title="t", case_type="trace_code"))
    s.conclusion = DiagnosticConclusion(**OK_CONCLUSION)
    s.status = status
    s.touch()
    return s


@pytest.fixture
def store(monkeypatch):
    st = SessionStore()
    monkeypatch.setattr(api_mod, "STORE", st)
    return st


# ======================================================================
# 允许的终态前置：completed / failed / closed_manual
# ======================================================================
@pytest.mark.asyncio
async def test_dismiss_completed(store):
    s = _seed(store, status="completed")
    out = await api_mod.dismiss(s.id, api_mod.DismissRequest(reason="不需要做"))
    assert out == {"session_id": s.id, "status": "dismissed"}
    got = store.get(s.id)
    assert got.status == "dismissed"
    assert got.conclusion is not None  # 结论保留，供人回看
    assert got.feedbacks and got.feedbacks[-1] == "不需要做"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "closed_manual"])
async def test_dismiss_terminal_ok(store, status):
    s = _seed(store, status=status)
    out = await api_mod.dismiss(s.id)
    assert out["status"] == "dismissed"
    assert store.get(s.id).status == "dismissed"


@pytest.mark.asyncio
async def test_dismiss_blank_reason_not_recorded(store):
    s = _seed(store, status="completed")
    await api_mod.dismiss(s.id, api_mod.DismissRequest(reason="   "))
    assert store.get(s.id).feedbacks == []


# ======================================================================
# 非法前置：analyzing / 重复 dismiss / 未知 id
# ======================================================================
@pytest.mark.asyncio
async def test_dismiss_analyzing_conflicts(store):
    s = _seed(store, status="analyzing")
    with pytest.raises(HTTPException) as ei:
        await api_mod.dismiss(s.id)
    assert ei.value.status_code == 409
    assert store.get(s.id).status == "analyzing"  # 不变


@pytest.mark.asyncio
async def test_dismiss_again_conflicts(store):
    s = _seed(store, status="completed")
    await api_mod.dismiss(s.id)
    with pytest.raises(HTTPException) as ei:
        await api_mod.dismiss(s.id)
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_dismiss_unknown_404(store):
    with pytest.raises(HTTPException) as ei:
        await api_mod.dismiss("nope")
    assert ei.value.status_code == 404


# ======================================================================
# dismiss 不碰 tasks（终态归一结果保持原样）
# ======================================================================
@pytest.mark.asyncio
async def test_dismiss_keeps_normalized_completed_tasks(store):
    s = _seed(store, status="completed")
    s.tasks = [InvestigationTask(id="t0", title="step 0", status="todo")]
    finalize_terminal_tasks(s, ok=True)  # → done + derived=True
    await api_mod.dismiss(s.id)
    got = store.get(s.id)
    assert got.tasks[0].status == "done"
    assert got.tasks[0].derived is True


@pytest.mark.asyncio
async def test_dismiss_keeps_cancelled_failed_tasks(store):
    s = _seed(store, status="failed")
    s.tasks = [InvestigationTask(id="t0", title="step 0", status="todo")]
    finalize_terminal_tasks(s, ok=False)  # → cancelled（绝不置 done）
    await api_mod.dismiss(s.id)
    got = store.get(s.id)
    assert got.tasks[0].status == "cancelled"
    assert got.tasks[0].derived is False


# ======================================================================
# HTTP 层：路由/错误码冒烟
# ======================================================================
def test_http_dismiss_round_trip(store):
    s = _seed(store, status="completed")
    with TestClient(api_mod.app) as client:
        r = client.post(f"/dismiss/{s.id}", json={"reason": "误报，忽略"})
        assert r.status_code == 200
        assert r.json()["status"] == "dismissed"

        # 已 dismissed → 409
        assert client.post(f"/dismiss/{s.id}").status_code == 409
        # 未知 session → 404
        assert client.post("/dismiss/nope").status_code == 404
