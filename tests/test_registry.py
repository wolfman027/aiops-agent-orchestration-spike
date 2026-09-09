"""工具注册表机制：schema 推断 / 权限白名单 / 进度 recorder / 工具错误自纠错结构。"""

from __future__ import annotations

import pytest

from aidiag.tools.datasources import build_datasources, crashloop_env
from aidiag.tools.progress import ToolCallEvent
from aidiag.tools.registry import (
    _filter_connected_mcps,
    bind_tools,
    build_permission_context,
    build_toolkit,
)
from aidiag.tools.todo import build_todo_specs

POD = "payment-checkout-6f9c8d7b5-xvz2p"


def _tools(toolkit):
    """Toolkit 2.0.3 把工具放在 tool_groups[*].tools（无顶层 .tools）。"""
    out = []
    for group in toolkit.tool_groups:
        out.extend(group.tools)
    return out


def _specs():
    env = crashloop_env()
    return bind_tools(env, build_datasources(env))


def test_tool_schemas_have_typed_params():
    toolkit = build_toolkit(_specs())
    by_name = {t.name: t for t in _tools(toolkit)}
    # FunctionTool 由签名推断 schema
    describe = by_name["describe_pod"]
    props = describe.input_schema.get("properties", {})
    assert "namespace" in props and "pod" in props
    logs = by_name["query_logs"]
    assert set(logs.input_schema["properties"]).issuperset(
        {"namespace", "pod", "container", "level", "tail"}
    )


def test_permission_allow_rules_match_toolkit():
    specs = _specs()
    ctx = build_permission_context(specs)
    names = {t.name for t in _tools(build_toolkit(specs))}
    assert set(ctx.allow_rules.keys()) == names
    from agentscope.permission import PermissionBehavior

    for rules in ctx.allow_rules.values():
        assert all(r.behavior == PermissionBehavior.ALLOW for r in rules)


def test_readonly_allowed_for_all_specs():
    specs = _specs()
    assert specs and all(s.read_only for s in specs)


def test_permission_allow_extra_merges_mcp_names():
    """DONT_ASK 下 MCP 工具名经 allow_extra 注入，与 spec 名单同批建 allow 规则（防静默 DENY）。"""
    specs = _specs()
    extra = ["mcp__git__git_rev_parse", "mcp__app-log__get_trace"]
    ctx = build_permission_context(specs, allow_extra=extra)
    assert set(ctx.allow_rules.keys()) == {s.name for s in specs} | set(extra)


def test_permission_without_allow_extra_unchanged():
    specs = _specs()
    ctx = build_permission_context(specs)
    assert set(ctx.allow_rules.keys()) == {s.name for s in specs}


def test_filter_connected_mcps_keeps_only_usable_clients():
    """stateful 未连接会抛 ValueError → 只保留 stateless 或已连接的 client。"""
    class _C:
        def __init__(self, stateful, connected):
            self.is_stateful = stateful
            self.is_connected = connected

    stateless = _C(stateful=False, connected=False)
    connected = _C(stateful=True, connected=True)
    stale = _C(stateful=True, connected=False)
    kept = _filter_connected_mcps([stale, stateless, connected])
    assert kept == [stateless, connected]


@pytest.mark.asyncio
async def test_recorder_captures_tool_calls():
    env = crashloop_env()
    specs = bind_tools(env, build_datasources(env))
    captured: list[ToolCallEvent] = []

    class Rec:
        def on_tool_start(self, tool, args):  # noqa: ANN001
            self._open = tool

        def on_tool_end(self, tool, result, ok):  # noqa: ANN001
            captured.append(ToolCallEvent(tool=tool, status="ok" if ok else "error"))

    toolkit = build_toolkit(specs, recorder=Rec())
    fn = next(t for t in _tools(toolkit) if t.name == "describe_pod")
    await fn.call(namespace="payment", pod=POD)
    assert captured and captured[-1].tool == "describe_pod"


@pytest.mark.asyncio
async def test_query_logs_missing_pod_returns_self_correct_error():
    env = crashloop_env()
    specs = bind_tools(env, build_datasources(env))
    spec = next(s for s in specs if s.name == "query_logs")
    res = await spec.func(namespace="payment", pod="ghost-pod")
    assert res["found"] is False
    assert "ghost-pod" in res["error"]
    assert res["query"]["pod"] == "ghost-pod"


@pytest.mark.asyncio
async def test_todo_specs_bound_to_session():
    from aidiag.diag.session import SessionStore
    from aidiag.domain import Issue

    store = SessionStore()
    session = store.create(Issue(title="t"))
    specs = build_todo_specs(session)
    names = {s.name for s in specs}
    assert names == {"plan_investigation", "complete_task"}

    fn = next(s for s in specs if s.name == "plan_investigation")
    await fn.func(title="x", steps=["a", "b"])
    assert [t.title for t in session.tasks] == ["a", "b"]
    assert all(t.status == "todo" for t in session.tasks)
