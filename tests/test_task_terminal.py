"""终态任务归一契约（离线，无 LLM）：**终态 ⇒ 不留 pending**。

症结回归：``session.tasks`` 是**模型自报账本**——只有 ``complete_task`` 工具写 done；而最终
结论走的是另一条通道（runner 解析模型最后那条 JSON）。于是"收敛根因并给出修复方案"这类
收尾步骤永远没人置 done，``completed`` 的会话却显示 ``todo``。

修法：runner/api 在落终态后调用 ``finalize_terminal_tasks`` 做**确定性**收敛：
- completed/approved（有结论）→ 残留 done + ``derived=True``（UI 灰显「随结论完成」）；
- failed/closed_manual（无结论）→ 残留 ``cancelled``（**绝不置 done**，不伪造完成）。
"""

from __future__ import annotations

import aidiag.diag.runner as runner_mod
from aidiag.diag import session as session_mod
from aidiag.diag.session import (
    InvestigationTask,
    SessionStore,
    finalize_terminal_tasks,
)
from aidiag.domain import Issue
from aidiag.llm import ScriptedJsonModel

PENDING = {"todo", "in_progress"}
OK_CONCLUSION = {"root_cause": "db conn timeout", "confidence": "medium"}


def _session(store: SessionStore) -> session_mod.Session:
    return store.create(Issue(title="t", case_type="trace_code"))


def _seed(session: session_mod.Session, statuses: list[str]) -> None:
    session.tasks = [
        InvestigationTask(id=f"t{i}", title=f"step {i}", status=st)
        for i, st in enumerate(statuses)
    ]


def test_completed_normalizes_residual_to_derived_done():
    session = _session(SessionStore())
    _seed(session, ["done", "todo", "in_progress"])
    session.status = "completed"

    finalize_terminal_tasks(session, ok=True)

    assert [t.status for t in session.tasks] == ["done", "done", "done"]
    # 显式完成的保持 derived=False；补写的标 derived=True 供 UI 灰显
    assert [t.derived for t in session.tasks] == [False, True, True]
    assert not PENDING & {t.status for t in session.tasks}


def test_failed_marks_cancelled_never_done():
    session = _session(SessionStore())
    _seed(session, ["done", "todo", "in_progress"])
    session.status = "failed"

    finalize_terminal_tasks(session, ok=False)

    assert [t.status for t in session.tasks] == ["done", "cancelled", "cancelled"]
    assert all(t.derived is False for t in session.tasks)
    assert not PENDING & {t.status for t in session.tasks}
    # 终态失败不能伪造完成：已有的 done 不被改写，pending 也不升格为 done
    assert [t.status for t in session.tasks].count("done") == 1


def test_finalize_is_idempotent():
    session = _session(SessionStore())
    _seed(session, ["todo"])
    session.status = "completed"

    finalize_terminal_tasks(session, ok=True)
    finalize_terminal_tasks(session, ok=True)

    assert session.tasks[0].status == "done"
    assert session.tasks[0].derived is True


def test_snapshot_exposes_derived_flag():
    """/status 契约：derived 随 model_dump 透出（纯加法，不破既有字段）。"""
    store = SessionStore()
    session = _session(store)
    _seed(session, ["todo"])
    session.status = "completed"
    finalize_terminal_tasks(session, ok=True)

    tasks = store.snapshot(session.id)["tasks"]
    assert tasks[0] == {
        "id": "t0",
        "title": "step 0",
        "status": "done",
        "derived": True,
    }


async def _run_with_injected_task(monkeypatch, model, *, status: str) -> session_mod.Session:
    """跑真实 runner，并在终态归一前注入一个残留步骤（模拟模型漏掉的收尾步骤）。

    ``run_diagnose`` 开头会 ``tasks.clear()``，所以无法预置；这里在 finalize 被调用的瞬间
    注入——正是"模型自报账本残留"落到归一逻辑上的那个点。顺带校验 ok 取值。
    """
    real_finalize = session_mod.finalize_terminal_tasks
    seen_ok: list[bool] = []

    def spy(session: session_mod.Session, ok: bool) -> None:
        session.tasks.append(
            InvestigationTask(id="tx", title="收敛根因并给出修复方案", status="todo")
        )
        seen_ok.append(ok)
        real_finalize(session, ok)

    monkeypatch.setattr(runner_mod, "finalize_terminal_tasks", spy)

    store = SessionStore()
    session = _session(store)
    await runner_mod.run_diagnose(session, model, [])
    assert seen_ok == [status == "completed"], f"finalize 未被正确调用: {seen_ok}"
    return session


async def test_runner_completed_leaves_no_pending(monkeypatch):
    session = await _run_with_injected_task(
        monkeypatch, ScriptedJsonModel(dict(OK_CONCLUSION)), status="completed"
    )

    assert session.status == "completed"
    assert not PENDING & {t.status for t in session.tasks}
    assert session.tasks[0].status == "done"
    assert session.tasks[0].derived is True


async def test_runner_failed_leaves_no_pending(monkeypatch):
    session = await _run_with_injected_task(
        monkeypatch, ScriptedJsonModel(["not json", "still not json"]), status="failed"
    )

    assert session.status == "failed"
    assert not PENDING & {t.status for t in session.tasks}
    assert session.tasks[0].status == "cancelled"
