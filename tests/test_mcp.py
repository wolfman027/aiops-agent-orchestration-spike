"""MCP 控制面（A3）离线机制：store CRUD / profiles seed / manager client 构造。

不连任何 server（不 spawn 子进程）：manager._build_client 只做配置→MCPClient 对象映射。
"""

from __future__ import annotations

import pytest

from aidiag.config import Settings
from aidiag.mcp.manager import MCPClientManager
from aidiag.mcp.profiles import (
    APPLOG_TOOLS,
    GIT_READONLY_TOOLS,
    PROFILES,
    _http_row,
    _real_http_row,
    _stdio_row,
    build_seed_rows,
    servers_for_case_type,
)
from aidiag.mcp.store import MCPStore

GIT_ROW = {
    "name": "git",
    "transport": "stdio",
    "config": {"command": "python", "args": ["mock_git_mcp.py"], "env": {}, "cwd": "."},
    "is_stateful": True,
    "enable_tools": list(GIT_READONLY_TOOLS),
    "enabled": True,
}


def _mock_settings() -> Settings:
    """离线测试用的空 MCP 配置：显式清 url/token，避免被开发者 .env 里的真实 url 污染
    （.env 一旦填了 AIDIAG_GIT_MCP_URL 等，build_seed_rows 就会走真 server 分支）。"""
    return Settings(
        git_mcp_url="",
        git_mcp_token="",
        applog_mcp_url="",
        applog_mcp_token="",
    )


def test_profiles_known_case_types():
    # PROFILES 是角色层声明（默认布局名）；运行时实际名由 servers_for_case_type 按 env 解析
    assert PROFILES["trace_code"] == frozenset({"git", "app-log"})
    assert PROFILES["k8s"] == frozenset()  # 旧路径不绑 MCP


def test_git_readonly_whitelist_has_no_write_tool():
    assert "git_demo_write" not in GIT_READONLY_TOOLS
    assert set(GIT_READONLY_TOOLS) >= {"git_rev_parse", "git_grep", "git_blame", "git_log_s"}


def test_seed_rows_are_two_enabled_with_enable_tools():
    rows = build_seed_rows(_mock_settings())  # 无 url → mock 布局
    assert {r["name"] for r in rows} == {"git", "app-log"}
    assert all(r["enabled"] for r in rows)
    git = next(r for r in rows if r["name"] == "git")
    applog = next(r for r in rows if r["name"] == "app-log")
    assert git["enable_tools"] == GIT_READONLY_TOOLS  # 只读白名单 = enable_tools（第二道闸）
    assert applog["enable_tools"] == APPLOG_TOOLS


def test_real_http_row_mirrors_prod_row():
    """镜像生产 mcp_servers 行：http is_stateful=True、enable_tools=None、Bearer header。"""
    row = _real_http_row("git-search-mcp-server", "http://127.0.0.1:8100/mcp", token="sekrit")
    assert row["transport"] == "http"
    assert row["is_stateful"] is True
    assert row["enable_tools"] is None  # 信任 server 端只读工具集（与生产行一致）
    assert row["enabled"] is True
    assert row["config"]["url"] == "http://127.0.0.1:8100/mcp"
    assert row["config"]["headers"]["Authorization"] == "Bearer sekrit"
    no_auth = _real_http_row("x", "http://x/mcp")  # token 空 → 不注入 header
    assert no_auth["config"]["headers"] == {}


def test_seed_rows_real_url_uses_real_server_names_and_auth():
    s = Settings(
        git_mcp_url="http://127.0.0.1:8100/mcp",
        git_mcp_token="git-tok",
        applog_mcp_url="http://127.0.0.1:8101/mcp",
        applog_mcp_token="app-tok",
    )
    rows = build_seed_rows(s)
    assert {r["name"] for r in rows} == {"git-search-mcp-server", "app-log-search-mcp-server"}
    git = next(r for r in rows if r["name"] == "git-search-mcp-server")
    assert git["transport"] == "http" and git["is_stateful"] is True
    assert git["config"]["headers"]["Authorization"] == "Bearer git-tok"
    app = next(r for r in rows if r["name"] == "app-log-search-mcp-server")
    assert app["config"]["headers"]["Authorization"] == "Bearer app-tok"


def test_seed_rows_mixed_keeps_mock_role_for_unset_url():
    # 显式清空 app-log url/token：否则会被开发者 .env 的真实 url 污染（同 _mock_settings 的理由）
    s = Settings(
        git_mcp_url="http://127.0.0.1:8100/mcp",
        git_mcp_token="git-tok",
        applog_mcp_url="",
        applog_mcp_token="",
    )
    names = {r["name"] for r in build_seed_rows(s)}
    assert "git-search-mcp-server" in names
    assert "app-log" in names  # 未配的 app-log 角色仍走 mock


def test_servers_for_case_type_resolves_real_or_mock_names():
    assert servers_for_case_type("trace_code", _mock_settings()) == frozenset({"git", "app-log"})
    assert servers_for_case_type("k8s", _mock_settings()) == frozenset()
    assert servers_for_case_type("whatever", _mock_settings()) == frozenset()
    real = Settings(
        git_mcp_url="http://127.0.0.1:8100/mcp",
        applog_mcp_url="http://127.0.0.1:8101/mcp",
    )
    assert servers_for_case_type("trace_code", real) == frozenset(
        {"git-search-mcp-server", "app-log-search-mcp-server"}
    )


def test_http_row_is_stateless():
    row = _http_row("git", "http://localhost:1234/mcp")
    assert row["transport"] == "http"
    assert row["is_stateful"] is False
    assert row["config"]["url"] == "http://localhost:1234/mcp"


def test_stdio_row_points_at_script_and_stateful():
    from pathlib import Path

    row = _stdio_row("git", Path("/tmp/mock_git_mcp.py"), env={"MOCK_GIT_REPO": "/repo"})
    assert row["transport"] == "stdio"
    assert row["is_stateful"] is True
    assert row["config"]["args"][0] == "/tmp/mock_git_mcp.py"
    assert row["config"]["env"]["MOCK_GIT_REPO"] == "/repo"


async def test_store_save_get_delete_and_duplicate_name():
    store = MCPStore()
    mid = await store.save(GIT_ROW)
    assert (await store.get(mid))["name"] == "git"
    with pytest.raises(ValueError):
        await store.save(GIT_ROW)  # name 冲突
    assert await store.delete(mid) is True
    assert await store.get(mid) is None


async def test_store_seed_if_empty_is_idempotent():
    store = MCPStore()
    first = await store.seed_if_empty([GIT_ROW])
    second = await store.seed_if_empty([GIT_ROW])
    assert len(first) == 1 and second == []
    assert len(await store.list()) == 1


def test_manager_build_client_stdio_forced_stateful():
    mgr = MCPClientManager(MCPStore())
    client = mgr._build_client(GIT_ROW)
    assert client.name == "git"
    assert client.is_stateful is True  # stdio 强约束
    assert client.enable_tools == GIT_READONLY_TOOLS


def test_manager_build_client_http_respects_stateful_flag():
    mgr = MCPClientManager(MCPStore())
    row = _http_row("app-log", "http://localhost:9/mcp")
    row["config"] = {"url": "http://localhost:9/mcp", "headers": {}, "timeout": 30}
    client = mgr._build_client(row)
    assert client.is_stateful is False


def test_manager_build_client_real_http_row_keeps_stateful_and_null_whitelist():
    """生产行（真实 server 名/http stateful/enable_tools=null）过 manager 映射无损耗。"""
    mgr = MCPClientManager(MCPStore())
    row = _real_http_row("git-search-mcp-server", "http://127.0.0.1:8100/mcp", token="t")
    client = mgr._build_client(row)
    assert client.name == "git-search-mcp-server"
    assert client.is_stateful is True
    assert client.enable_tools is None
    assert client.disable_tools is None


def test_manager_build_client_unsupported_transport_raises():
    mgr = MCPClientManager(MCPStore())
    with pytest.raises(ValueError):
        mgr._build_client({**GIT_ROW, "transport": "inproc"})


class _StubMCPManager:
    """resolve 测试用桩：不真连 MCP，只回空 client/allow。"""

    async def clients_for(self, names):  # noqa: ANN001, ARG002
        return []

    async def allow_names_for(self, names):  # noqa: ANN001, ARG002
        return []


async def test_resolve_specs_trace_code_has_no_local_repo_state(monkeypatch):
    """HEAD 走 git MCP，不再有本地 repo_state function 工具：trace_code 的 function specs
    只剩 fetch_strategy（Phase 7 delta）。"""
    import aidiag.api as api_mod
    from aidiag.domain import Issue

    monkeypatch.setattr(api_mod.app.state, "mcp", _StubMCPManager(), raising=False)
    specs, clients, allow = await api_mod._resolve_specs_for(
        Issue(case_type="trace_code", repo="sip-aiops-management")
    )
    assert {s.name for s in specs} == {"fetch_strategy"}
    assert clients == [] and allow == []

