"""模型构建：DeepSeek（OpenAI 兼容）经 AgentScope OpenAI 模型接入。

- 配置了 API key → ``OpenAIChatModel``（真实 DeepSeek，live 验证用）。
- 未配置 → ``ScriptedJsonModel``（确定性输出，仅供无 LLM 的机制单测）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from agentscope.credential import CredentialBase, OpenAICredential
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase, ChatResponse, OpenAIChatModel

from .config import Settings, get_settings


def build_model(settings: Settings | None = None) -> ChatModelBase:
    """构建 DeepSeek Chat 模型；未配置 API key 时回退 ScriptedJsonModel。

    注入显式 ``httpx.AsyncClient``（``trust_env=False`` + 有限超时）：本机实测 openai
    默认内部 client 在部分网络下 connect 超时，显式 http_client 可绕过（详见
    docs/IMPLEMENTATION_PLAN.md §风险）。
    """
    settings = settings or get_settings()
    if not settings.deepseek_api_key:
        return ScriptedJsonModel({"note": "no_api_key"})
    import httpx

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=20.0),
        trust_env=False,
        follow_redirects=True,
    )
    return OpenAIChatModel(
        credential=OpenAICredential(
            api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url
        ),
        model=settings.deepseek_model,
        stream=True,
        client_kwargs={"http_client": http_client},
    )


class ScriptedJsonModel(ChatModelBase):
    """确定性脚本模型：按调用次序返回预置文本（JSON 或多段）。

    用途：离线机制测试（注册表 / 权限 / session / API 状态机），不做诊断质量验证。
    """

    class Parameters(ChatModelBase.Parameters):
        pass

    def __init__(self, responses: list[str] | dict, model: str = "scripted-json") -> None:
        super().__init__(
            credential=CredentialBase(),
            model=model,
            parameters=self.Parameters(),
            stream=False,
        )
        # 归一化：dict → 单条；list → 顺序弹出
        self._responses: list[str] = (
            [json.dumps(responses, ensure_ascii=False)]
            if isinstance(responses, dict)
            else list(responses)
        )
        self._call_count = 0

    @classmethod
    def _get_retryable_exceptions(cls):
        return ()

    async def _call_api(
        self, model_name: str, messages, tools=None, tool_choice=None, **kwargs: Any
    ) -> AsyncGenerator:
        text = self._responses[min(self._call_count, len(self._responses) - 1)]
        self._call_count += 1
        return _single(TextBlock(text=text))


def _single(content_block) -> AsyncGenerator:
    async def _gen():
        yield ChatResponse(content=[content_block], is_last=True)

    return _gen()
