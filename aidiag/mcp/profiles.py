"""case_type → MCP server 绑定 + 默认 seed 行（B1/B4 配置层）。

每个 trace_code 角色（git / app-log）有两种布局：

- **真 server 布局**（env 给了该角色 http url + 可选 token）：seed 行用**真实 server 名**（默认
  ``git-search-mcp-server`` / ``app-log-search-mcp-server``，可经 Settings 覆盖），行形状镜像生产
  ``public.mcp_servers``：http ``is_stateful=True``、``enable_tools=None``（信任 server 端只暴露
  只读工具集）、token 非空时注入 ``Authorization: Bearer`` header。
- **本地 mock 布局**（无 url）：seed 行用角色名 ``git`` / ``app-log``，stdio stateful，
  ``enable_tools`` 收敛只读白名单（B5 第二道闸——mock 故意暴露写工具 git_demo_write 验证闸）。

- ``PROFILES``：case_type → 该场景需要的**角色名**（默认布局下的行名）。``k8s`` = 空
  （旧 mock 数据源，不绑 MCP）。
- ``servers_for_case_type``：运行时把角色解析成**实际 seed 行名**（真 server 名或角色名，
  随 env 定），api resolve 靠它取 client + allow。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ..config import Settings

ROOT = Path(__file__).resolve().parent.parent.parent

# 角色声明：trace_code 需要 git / app-log 两个角色（默认布局名）；k8s 旧路径不绑 MCP
PROFILES: dict[str, frozenset[str]] = {
    "trace_code": frozenset({"git", "app-log"}),
    "k8s": frozenset(),
}

# mock（本地 stdio）布局用的只读白名单（B5 第二道闸）。真 server 布局 enable_tools=None：
# 真 server 在 server 端就只暴露只读工具，镜像生产 mcp_servers 行的信任模型。
GIT_READONLY_TOOLS = [
    "git_rev_parse",
    "git_status",
    "git_log_s",
    "git_grep",
    "git_show_file",
    "git_blame",
]
APPLOG_TOOLS = ["get_trace", "query_logs"]


def _role_row_name(role: str, settings: Settings) -> str:
    """角色 → 实际 seed 行名：真 server 布局用真实 server 名，mock 布局用角色名。"""
    if role == "git":
        return settings.git_mcp_name if settings.git_mcp_url else "git"
    return settings.applog_mcp_name if settings.applog_mcp_url else "app-log"


def servers_for_case_type(case_type: str, settings: Settings) -> frozenset[str]:
    """case_type → 该场景实际要拉的 server 名集合（resolve 用，真/mock 名随 env 定）。"""
    if case_type not in PROFILES:
        return frozenset()
    return frozenset(_role_row_name(role, settings) for role in PROFILES[case_type])


def _stdio_row(name: str, script: Path, *, env: dict[str, str]) -> dict[str, Any]:
    """本地 stdio mock server 的 store row（stateful 常驻连接）。"""
    return {
        "name": name,
        "transport": "stdio",
        "config": {
            "command": sys.executable,
            "args": [str(script)],
            "env": env,
            "cwd": str(ROOT),
        },
        "is_stateful": True,
    }


def _http_row(name: str, url: str) -> dict[str, Any]:
    """远程 http MCP server 的 store row（stateless——单测/旧路径用）。"""
    return {
        "name": name,
        "transport": "http",
        "config": {"url": url, "headers": {}, "timeout": 30},
        "is_stateful": False,
    }


def _real_http_row(name: str, url: str, *, token: str = "") -> dict[str, Any]:
    """镜像生产 ``public.mcp_servers`` 的 http 行：is_stateful=True、enable/disable_tools=None。

    enable_tools=None = 信任 server 端只暴露只读工具集（与生产行一致）；server 若日后加了写工具会
    直接可见——真 server 的只读边界在 server 端收敛，不是靠这里的白名单。
    """
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return {
        "name": name,
        "transport": "http",
        "config": {"url": url, "headers": headers, "timeout": 30},
        "is_stateful": True,
        "enable_tools": None,
        "disable_tools": None,
        "enabled": True,
    }


def build_seed_rows(settings: Settings) -> list[dict[str, Any]]:
    """构造 git / app-log 两角色的 seed 行：真 http server（url 给了）优先，否则本地 mock。"""
    rows: list[dict[str, Any]] = []

    # ---- git 角色 ----
    if settings.git_mcp_url:
        rows.append(
            _real_http_row(
                _role_row_name("git", settings),
                settings.git_mcp_url,
                token=settings.git_mcp_token,
            )
        )
    else:
        env = {"MOCK_GIT_REPO": settings.repo_cwd}
        row = _stdio_row("git", ROOT / "scripts/mock_git_mcp.py", env=env)
        row["enable_tools"] = list(GIT_READONLY_TOOLS)
        row["disable_tools"] = None
        row["enabled"] = True
        rows.append(row)

    # ---- app-log 角色 ----
    if settings.applog_mcp_url:
        rows.append(
            _real_http_row(
                _role_row_name("app-log", settings),
                settings.applog_mcp_url,
                token=settings.applog_mcp_token,
            )
        )
    else:
        env = {}
        if settings.applog_mock_data:
            env["MOCK_APPLOG_DATA"] = settings.applog_mock_data
        row = _stdio_row("app-log", ROOT / "scripts/mock_applog_mcp.py", env=env)
        row["enable_tools"] = list(APPLOG_TOOLS)
        row["disable_tools"] = None
        row["enabled"] = True
        rows.append(row)
    return rows
