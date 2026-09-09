"""M0 冒烟：DeepSeek 连通性（无工具的一次对话）。

用法：
    cd aiops-agent-orchestration-spike
    uv run python scripts/smoke.py
需 .env 提供 DEEPSEEK_API_KEY。成功即打印模型回话。
"""

from __future__ import annotations

import asyncio

from agentscope.agent import Agent, ReActConfig
from agentscope.message import UserMsg
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from aidiag.config import get_settings
from aidiag.llm import build_model


async def main() -> None:
    settings = get_settings()
    if not settings.deepseek_api_key:
        raise SystemExit("DEEPSEEK_API_KEY 未配置：请 cp .env.example .env 并填入 key。")
    model = build_model(settings)
    agent = Agent(
        name="smoke",
        system_prompt="你是 AIOps 诊断助手。用一句中文简短回答。",
        model=model,
        toolkit=Toolkit(tools=[]),
        state=AgentState(),
        react_config=ReActConfig(max_iters=1),
    )
    final = await agent.reply(UserMsg(name="user", content="你好，连通性测试。你是谁？"))
    text = "".join(b.text for b in final.get_content_blocks("text") if b.text)
    print("== model reply ==")
    print(text.strip() or "(空回复)")


if __name__ == "__main__":
    asyncio.run(main())
