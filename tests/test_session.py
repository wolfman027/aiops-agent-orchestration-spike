"""SessionStore 机制：状态机 + TTL + 驳回计数（无 LLM）。"""

from __future__ import annotations

import time

from aidiag.diag.session import RemediationPlan, SessionStore
from aidiag.domain import Issue


def _issue() -> Issue:
    return Issue(title="t", description="d", namespace="n")


def test_create_get_and_status_flow():
    store = SessionStore()
    s = store.create(_issue())
    assert s.status == "analyzing"
    assert store.get(s.id) is s

    s.status = "completed"
    s.touch()
    snap = store.snapshot(s.id)
    assert snap["status"] == "completed"
    assert snap["session_id"] == s.id


def test_missing_returns_none():
    store = SessionStore()
    assert store.get("nope") is None


def test_ttl_sweep_expires_idle_session():
    store = SessionStore(ttl_seconds=1)
    s = store.create(_issue())
    assert store.get(s.id) is s
    time.sleep(1.2)
    # 显式 sweep：一次性清掉过期会话
    assert store.sweep_expired() == 1
    # get() 对过期会话做惰性清理，也会返回 None
    assert store.get(s.id) is None


def test_ttl_get_lazily_removes_expired():
    store = SessionStore(ttl_seconds=1)
    s = store.create(_issue())
    time.sleep(1.2)
    assert store.get(s.id) is None
    assert store.sweep_expired() == 0  # 已被惰性清理


def test_reanalyze_and_closed_manual_accumulate():
    store = SessionStore()
    s = store.create(_issue())
    s.status = "completed"
    s.remediation = RemediationPlan(status="pending_review")
    s.feedbacks.append("复核下")
    s.reanalyze_count += 1
    snap = store.snapshot(s.id)
    assert snap["reanalyze_count"] == 1
    assert snap["remediation_status"] == "pending_review"
