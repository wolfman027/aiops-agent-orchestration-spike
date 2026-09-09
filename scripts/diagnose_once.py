"""M1 单跑诊断：真实 DeepSeek 一次只读诊断，目检 JSON 是否合法、含证据。

用法：
    uv run python scripts/diagnose_once.py [--strategy crashloop] [--no-live]
"""

from __future__ import annotations

import argparse
import asyncio
import json

from aidiag.agents import build_agent, run_agent
from aidiag.config import get_settings
from aidiag.domain import Issue, conclusion_from_dict
from aidiag.llm import build_model
from aidiag.prompts import build_system_prompt
from aidiag.tools.datasources import build_datasources, crashloop_env
from aidiag.tools.registry import bind_tools

CASE_A = Issue(
    id="case-a",
    title="[P0] payment/checkout 反复 CrashLoopBackOff，Deployment checkout 无可用副本",
    description=(
        "从 2026-09-08T01:15Z 起，checkout 服务的 pod 持续重启并处于 CrashLoopBackOff，"
        "Deployment checkout 0/1 可用；支付请求超时。请定位根因并给出修复建议。"
    ),
    namespace="payment",
)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None, help="注入场景策略，如 crashloop")
    ap.add_argument("--no-live", action="store_true", help="无 key 时用 scripted 模型跑机制")
    args = ap.parse_args()

    settings = get_settings()
    if args.no_live or not settings.deepseek_api_key:
        from aidiag.llm import ScriptedJsonModel

        model = ScriptedJsonModel(
            {
                "root_cause": "no_api_key（机制演示）",
                "confidence": "low",
                "evidence": [],
                "recommended_fix": [],
            }
        )
    else:
        model = build_model(settings)

    env = crashloop_env()
    tool_funcs = build_datasources(env)
    specs = bind_tools(env, tool_funcs)
    system_prompt = build_system_prompt(strategy=args.strategy)
    agent = build_agent(system_prompt, model, specs, name="diagnostician")

    user_content = (
        f"命名空间: {CASE_A.namespace}\n"
        f"告警标题: {CASE_A.title}\n"
        f"告警详情: {CASE_A.description}\n\n"
        "请诊断。"
    )
    parsed, raw = await run_agent(agent, user_content)
    print("===== RAW REPLY (last 1500) =====")
    print(raw[-1500:])
    print("===== PARSED JSON =====")
    print(json.dumps(parsed, ensure_ascii=False, indent=2)[:3000])
    if parsed:
        try:
            conc = conclusion_from_dict(parsed)
            print("===== normalized =====")
            print(conc.model_dump_json(indent=2)[:2000])
        except Exception as e:  # noqa: BLE001
            print("normalize failed:", e)


if __name__ == "__main__":
    asyncio.run(main())
