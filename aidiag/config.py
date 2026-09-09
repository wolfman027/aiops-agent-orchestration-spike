"""全局配置：LLM 连接 + 诊断默认参数。

移植 agentflow ``agentflow/config.py`` 的 pydantic-settings 约定（env_prefix + alias 兼容
环境里既有的 ``DEEPSEEK_API_KEY``）。本 spike 只用 DeepSeek（OpenAI 兼容路径）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT / ".env",),
        env_prefix="AIDIAG_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # ---- LLM：DeepSeek（deepseek-v4-flash）----
    # 兼容两种环境变量：AIDIAG_DEEPSEEK_API_KEY（本库）与 DEEPSEEK_API_KEY（惯例）
    deepseek_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AIDIAG_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1",
        validation_alias=AliasChoices("AIDIAG_DEEPSEEK_BASE_URL", "DEEPSEEK_BASE_URL"),
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias=AliasChoices("AIDIAG_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"),
    )

    # ---- API 服务（启动绑定；scripts/run_api.py / make api 读取）----
    api_host: str = Field(default="127.0.0.1", description="uvicorn 绑定主机")
    api_port: int = Field(default=8017, description="uvicorn 绑定端口")

    # ---- MCP 控制面（A3）：真实 git / app-log server（http）----
    # 角色填 http url → 接真 server（client 名 = 该 server 名 → 工具前缀 mcp__<name>__<tool>；
    # 行形状镜像生产 mcp_servers：http is_stateful=True、enable_tools=None 信任只读工具集）。
    # url 留空 → 该角色回退本地 stdio mock（scripts/mock_*_mcp.py）。
    git_mcp_url: str = Field(default="")
    git_mcp_name: str = Field(default="git-search-mcp-server")
    git_mcp_token: str = Field(
        default="", description="Bearer token；非空才注入 Authorization header"
    )
    applog_mcp_url: str = Field(default="")
    applog_mcp_name: str = Field(default="app-log-search-mcp-server")
    applog_mcp_token: str = Field(default="")
    # mock app-log 数据集文件（JSON，见 scripts/mock_applog_mcp.py 形状）；留空用内置默认
    applog_mock_data: str = Field(default="")
    # repo 基线：repo_state 工具 / git mock 服务器操作的工作区（app→repo 收敛到单仓库）
    repo_cwd: str = Field(default=str(ROOT))

    # ---- 诊断 agent 默认 ----
    max_iters: int = Field(default=10, description="ReAct 循环最大迭代数")
    session_ttl_seconds: int = Field(default=3600, description="session 过期时间（秒）")
    max_reanalyze: int = Field(default=3, description="Stage2 驳回后最多重分析次数")


@lru_cache
def get_settings() -> Settings:
    return Settings()
