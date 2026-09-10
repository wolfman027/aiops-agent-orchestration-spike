"""诊断入口分门（来源=门）：log/metric 两个专用门 → Issue 映射 + seed 渲染 + 共享脊柱。

纯离线，无 LLM / MCP：只测字段映射、``compose_user_content`` 的渲染纪律（seed 是"提示须查证"
而非结论）、以及两门共用 ``_start``。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import aidiag.api as api_mod
from aidiag.api import (
    LogTriggerRequest,
    MetricTriggerRequest,
    _issue_from_log,
    _issue_from_metric,
)
from aidiag.diag.runner import compose_user_content
from aidiag.diag.session import SessionStore

NPE = (
    'Cannot invoke "String.trim()" because the return value of '
    '"acc.sipaiops.dto.CreateIncidentRequest.getAssignee()" is null'
)


def test_log_door_maps_issue():
    issue = _issue_from_log(
        LogTriggerRequest(
            log_excerpt=NPE,
            app="sip-aiops-management",
            repo="sip-aiops-management",
            trace_id="2430a48a7e4d4a4f97b2788ed6a8891b",
        )
    )
    assert issue.case_type == "trace_code"
    assert issue.trigger == "log"
    assert issue.title == "[log] sip-aiops-management 2430a48a7e4d4a4f97b2788ed6a8891b"
    assert issue.app == "sip-aiops-management"
    assert issue.log_excerpt == NPE
    assert issue.metric_alert is None


def test_log_door_title_falls_back_to_time_window():
    issue = _issue_from_log(
        LogTriggerRequest(log_excerpt=NPE, app="svc", time_window="2026-09-08T01:05Z/01:40Z")
    )
    assert issue.title == "[log] svc 2026-09-08T01:05Z/01:40Z"


def test_log_door_requires_excerpt_and_app():
    with pytest.raises(ValidationError):
        LogTriggerRequest(app="svc")  # 缺 log_excerpt
    with pytest.raises(ValidationError):
        LogTriggerRequest(log_excerpt=NPE)  # 缺 app


def test_metric_door_maps_issue():
    issue = _issue_from_metric(
        MetricTriggerRequest(
            metric="container_restarts",
            resource="payment-checkout-6f9c8d7b5-xvz2p",
            value="7",
            threshold=">3 in 5m",
            description="pod restarts spiked",
        )
    )
    assert issue.case_type == "trace_code"
    assert issue.trigger == "metric"
    assert issue.title == "[metric] payment-checkout-6f9c8d7b5-xvz2p container_restarts"
    assert issue.app == "payment-checkout-6f9c8d7b5-xvz2p"  # app 回退 resource
    assert issue.metric_alert is not None
    assert issue.metric_alert.metric == "container_restarts"
    assert issue.metric_alert.value == "7"
    assert issue.metric_alert.threshold == ">3 in 5m"
    assert issue.log_excerpt == ""


def test_metric_door_app_overrides_resource():
    issue = _issue_from_metric(
        MetricTriggerRequest(metric="http_5xx_rate", resource="order-api", app="order-api-svc")
    )
    assert issue.app == "order-api-svc"


def test_compose_user_content_seeds_log_as_hint():
    issue = _issue_from_log(LogTriggerRequest(log_excerpt=NPE, app="sip", trace_id="T-1"))
    text = compose_user_content(issue)
    assert NPE in text  # 摘录带进来
    assert "仅供参考" in text  # 但压成提示
    assert "不是结论" in text
    assert "trigger_log" in text  # 出处纪律


def test_compose_user_content_renders_metric_alert():
    issue = _issue_from_metric(
        MetricTriggerRequest(
            metric="http_5xx_rate",
            resource="order-api",
            value="12%",
            threshold=">5%",
            description="elevated 5xx",
        )
    )
    text = compose_user_content(issue)
    assert "指标告警" in text
    assert "metric=http_5xx_rate" in text
    assert "value=12%" in text
    assert "threshold=>5%" in text
    assert "elevated 5xx" in text


def test_compose_user_content_no_seed_when_manual():
    """通用/人工门不带 seed：渲染里不出现日志/指标块。"""
    issue = _issue_from_log(LogTriggerRequest(log_excerpt=NPE, app="sip"))
    issue.trigger = "manual"
    issue.log_excerpt = ""
    text = compose_user_content(issue)
    assert "触发日志摘录" not in text
    assert "指标告警" not in text


async def test_doors_share_spine(monkeypatch):
    """两门共用 _start：各自落库 session、trigger 正确、状态 analyzing（后台调度 stub 掉）。"""
    store = SessionStore()
    monkeypatch.setattr(api_mod, "STORE", store)
    monkeypatch.setattr(api_mod, "_schedule_run", lambda session: None)

    r1 = await api_mod.diagnose_logs(
        LogTriggerRequest(log_excerpt=NPE, app="sip", trace_id="T-1")
    )
    r2 = await api_mod.diagnose_metrics(
        MetricTriggerRequest(metric="container_restarts", resource="pod-x")
    )

    s1 = store.get(r1["session_id"])
    s2 = store.get(r2["session_id"])
    assert s1 is not None and s1.issue.trigger == "log" and s1.status == "analyzing"
    assert s2 is not None and s2.issue.trigger == "metric" and s2.status == "analyzing"
    assert r1["status"] == "analyzing" and r2["status"] == "analyzing"
