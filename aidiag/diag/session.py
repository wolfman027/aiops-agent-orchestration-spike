"""SessionStore：诊断会话状态机 + TTL + 进度（tasks/tool_calls）。

状态机：analyzing → completed | failed；approve 终态 = session/plan 均 approved；
驳回达上限 → closed_manual（终态）。approve 不执行任何东西（TRACE_CODE_DESIGN §1）。
所有变更都在单线程事件循环内同步完成（REST 轮询与后台任务协作式并发，无锁）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from ..domain import DiagnosticConclusion, Issue, RemediationStep
from ..tools.progress import ToolCallEvent


class InvestigationTask(BaseModel):
    id: str
    title: str
    status: str = "todo"  # todo | in_progress | done | failed


class RemediationPlan(BaseModel):
    """计划级审批（TRACE_CODE_DESIGN §3 L3）：approve 是终态签章，零下游动作。"""

    status: str = "pending_review"  # pending_review|approved|rejected|closed_manual
    steps: list[RemediationStep] = Field(default_factory=list)
    last_feedback: str = ""
    submitted_at: float = Field(default_factory=time.time)
    decided_at: float | None = None


class Session(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    issue: Issue
    # Stage1 + 审批
    status: str = "analyzing"  # analyzing | completed | failed | approved | closed_manual
    error: str = ""
    raw_reply: str = ""
    tasks: list[InvestigationTask] = Field(default_factory=list)
    tool_calls: list[ToolCallEvent] = Field(default_factory=list)
    conclusion: DiagnosticConclusion | None = None
    # 驳回重跑（Stage2）
    reanalyze_count: int = 0
    feedbacks: list[str] = Field(default_factory=list)
    remediation: RemediationPlan | None = None
    # TTL
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()


class SessionStore:
    """内存版 Session 存储（spike；生产可换 DB）。TTL 惰性清理 + 显式 sweep。"""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._sessions: dict[str, Session] = {}
        self.ttl_seconds = ttl_seconds

    def create(self, issue: Issue) -> Session:
        s = Session(issue=issue)
        self._sessions[s.id] = s
        return s

    def get(self, session_id: str) -> Session | None:
        s = self._sessions.get(session_id)
        if s is None:
            return None
        if self._expired(s):
            self._sessions.pop(session_id, None)
            return None
        return s

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def _expired(self, s: Session) -> bool:
        return (time.time() - s.updated_at) > self.ttl_seconds

    def sweep_expired(self) -> int:
        """删除过期 session，返回删除数（供 TTL sweeper 调用）。"""
        expired = [sid for sid, s in self._sessions.items() if self._expired(s)]
        for sid in expired:
            self._sessions.pop(sid, None)
        return len(expired)

    def snapshot(self, session_id: str) -> dict[str, Any] | None:
        """/status 用：把 session 序列化成扁平 dict（task/tool_calls 原文）。"""
        s = self.get(session_id)
        if s is None:
            return None
        return {
            "session_id": s.id,
            "status": s.status,
            "issue_title": s.issue.title,
            "tasks": [t.model_dump() for t in s.tasks],
            "tool_calls": [t.model_dump() for t in s.tool_calls],
            "reanalyze_count": s.reanalyze_count,
            "remediation_status": s.remediation.status if s.remediation else None,
            "conclusion": s.conclusion.model_dump() if s.conclusion else None,
            "error": s.error,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
