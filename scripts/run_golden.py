"""Golden 用例 live 跑分（v4 §6）：真实 DeepSeek 跑 case A 断言。

用法：
    uv run python scripts/run_golden.py
退出码 0 = 全过；非 0 = 有失败。需要 .env 提供 DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

from aidiag.config import get_settings
from aidiag.diag.runner import run_diagnose
from aidiag.diag.session import SessionStore
from aidiag.domain import DiagnosticConclusion, Issue
from aidiag.llm import build_model
from aidiag.tools import datasources
from aidiag.tools.datasources import build_datasources
from aidiag.tools.registry import bind_tools

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"


def _conclusion_text(c: DiagnosticConclusion) -> str:
    parts = [c.root_cause, c.summary]
    for e in c.evidence:
        parts.extend([e.finding, e.supporting_text])
    return "\n".join(parts)


def _check_case(cfg: dict, conc: DiagnosticConclusion) -> list[str]:
    failures: list[str] = []
    expect = cfg["expect"]
    text = _conclusion_text(conc)

    for token in expect.get("must_contain_in_conclusion", []):
        if token not in text:
            failures.append(f"结论未包含唯一 token: {token}")
    if expect.get("fix_nonempty") and not conc.recommended_fix:
        failures.append("recommended_fix 为空")
    scopes = expect.get("fix_target_scope", [])
    for oi, opt in enumerate(conc.recommended_fix):
        for si, step in enumerate(opt.steps):
            t = step.target
            if scopes and not any(s in t for s in scopes):
                failures.append(
                    f"recommended_fix[{oi}].steps[{si}].target={t!r} 超出查证范围（须含 {scopes} 之一）"
                )
    if conc.evidence and len(conc.evidence) < expect.get("min_evidence", 0):
        failures.append(f"evidence 不足 {expect.get('min_evidence')} 条")
    return failures


async def run_case(cfg: dict) -> tuple[bool, list[str], list[str]]:
    settings = get_settings()
    if not settings.deepseek_api_key:
        return False, ["DEEPSEEK_API_KEY 未配置"], []
    fixture = getattr(datasources, f"{cfg['env_fixture']}_env")
    env = fixture()
    tool_funcs = build_datasources(env)
    specs = bind_tools(env, tool_funcs)
    model = build_model(settings)
    issue = Issue(**cfg["issue"])

    all_fail: list[str] = []
    raw_samples: list[str] = []
    n = int(cfg.get("runs", 1))
    for i in range(n):
        store = SessionStore()
        session = store.create(issue)
        await run_diagnose(session, model, specs, strategy=issue.strategy)
        if session.status != "completed":
            all_fail.append(f"run{i + 1} status={session.status}: {session.error[:200]}")
            continue
        raw_samples.append(session.raw_reply[-120:])
        conc = session.conclusion
        assert conc is not None
        fails = _check_case(cfg, conc)
        if fails:
            all_fail.append(f"run{i + 1}: " + "; ".join(fails))
    return not all_fail, all_fail, raw_samples


def main() -> int:
    yml = EVALS / "golden_crashloop.yaml"
    cfg = yaml.safe_load(yml.read_text(encoding="utf-8"))
    ok, failures, _raw = asyncio.run(run_case(cfg))
    if ok:
        print(f"PASS {cfg['case']}（{cfg.get('runs', 1)} runs）")
        return 0
    print(f"FAIL {cfg['case']}")
    for f in failures:
        print("  -", f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
