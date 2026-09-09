"""REST 冒烟（离线，无 LLM key）：case_type 解析 + 真 MCP 桩拉起 + 状态机终点。

覆盖 TRACE_CODE_IMPLEMENTATION_PLAN Phase 4 ②：真 MCP **桩** server（scripts/mock_*_mcp.py）
+ ScriptedJsonModel（build_model 在无 key 时的回退）走整条链路：
POST /diagnose case_type=trace_code → 后台 resolve_specs_for 拉起 git/app-log 桩 →
build_agent 挂 MCP client + MCPRecorderMiddleware → run_diagnose → /status 终到 completed。

断言的是「链路没断、能到终态」，不是诊断质量（质量靠 live golden）。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import aidiag.api as api_mod
from aidiag.config import get_settings
from aidiag.diag.session import SessionStore
from aidiag.llm import ScriptedJsonModel


@pytest.fixture
def isolated_store(monkeypatch):
    """每次冒烟用全新 store，避免与其它测试共享模块全局 STORE。"""
    st = SessionStore(ttl_seconds=get_settings().session_ttl_seconds)
    monkeypatch.setattr(api_mod, "STORE", st)
    return st


@pytest.fixture
def scripted_model(monkeypatch):
    """离线冒烟必须用 Scripted 模型：本机 .env 有 DEEPSEEK_API_KEY，build_model 会返回真模型，
    一发出去就打真实 DeepSeek → 网络阻塞/超时，永远停在 analyzing。这里把 lifespan 用的
    build_model 换成固定回 ScriptedJsonModel。"""

    def _fake(settings=None):  # noqa: ANN001, ARG001
        return ScriptedJsonModel({"root_cause": "db conn timeout", "confidence": "medium"})

    monkeypatch.setattr(api_mod, "build_model", _fake)


def _poll(client: TestClient, session_id: str, timeout: float = 40.0) -> dict:
    """轮询 /status 直到离开 analyzing（终到 completed/failed/approved）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get(f"/status/{session_id}").json()
        if snap["status"] != "analyzing":
            return snap
        time.sleep(0.2)
    raise AssertionError(f"session {session_id} 在 {timeout}s 内未离开 analyzing")


def test_trace_code_full_stack_completes(isolated_store, scripted_model):
    with TestClient(api_mod.app) as client:
        r = client.post(
            "/diagnose",
            json={
                "title": "[trace] order-checkout 下单链路失败",
                "description": "order-api 的 T-88f1a2 请求失败，需诊断",
                "case_type": "trace_code",
                "app": "order-api",
                "repo": "checkout",
                "trace_id": "T-88f1a2",
                "deployed": {"kind": "commit", "value": "9a2ff1fe3d4850a4d1292999f6d224f1fa65c08f"},
            },
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]
        snap = _poll(client, sid)
        assert snap["status"] == "completed", f"error={snap['error']!r}"
        # 链路终点有 conclusion（Scripted 模型给出空壳结论，重点在没断）
        assert snap["conclusion"] is not None
        assert snap["error"] == ""


def test_k8s_case_default_back_compat(isolated_store, scripted_model):
    """不传 case_type（默认 k8s）走旧 crashloop mock 数据源，链路照常 completed。"""
    with TestClient(api_mod.app) as client:
        r = client.post(
            "/diagnose",
            json={
                "title": "[P0] payment/checkout CrashLoopBackOff",
                "description": "Deployment checkout 0/1 可用",
                "namespace": "payment",
            },
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]
        snap = _poll(client, sid)
        assert snap["status"] == "completed", f"error={snap['error']!r}"
