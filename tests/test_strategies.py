"""M3 发现型 runbook 机制（DESIGN §6）离线断言：防作弊 = 场景内目录 + 默认不预烤。

live golden 断言"真调了 fetch_strategy(name)"；这里在机制层锁定它的两个前提：
- 目录/allowlist 按 case_type 收敛（trace_code 上下文取不到 k8s 的 crashloop）；
- trace_code 默认（strategy=None）不把任何 runbook 预烤进 system prompt → agent 只能靠
  证据命中后 fetch_strategy 现取（discovery 而非 recognition）。
"""

from __future__ import annotations

import pytest

from aidiag.prompts import build_system_prompt
from aidiag.prompts.runbook_registry import RUNBOOKS, names_for_case_type
from aidiag.tools.strategies import build_strategy_spec

TRACE_NAMES = {"trace_bug", "trace_dependency", "trace_startup"}


def test_trace_code_catalog_excludes_crashloop():
    assert set(names_for_case_type("trace_code")) == TRACE_NAMES
    assert "crashloop" not in names_for_case_type("trace_code")
    assert names_for_case_type("k8s") == ["crashloop"]


@pytest.mark.asyncio
async def test_fetch_strategy_gates_k8s_runbook_out_of_trace_code():
    spec = build_strategy_spec(case_type="trace_code")
    denied = await spec.func(name="crashloop")
    assert denied["found"] is False
    assert set(denied["available"]) == TRACE_NAMES  # 错误返回带可用清单，模型可自纠

    ok = await spec.func(name="trace_dependency")
    assert ok["found"] is True
    assert "trace_dependency" in ok["strategy"]
    assert ok["runbook"].strip()  # runbook 正文在跑动中当工具结果拉进来


@pytest.mark.asyncio
async def test_description_embeds_scoped_catalog_not_crashloop():
    spec = build_strategy_spec(case_type="trace_code")
    for name in TRACE_NAMES:
        assert name in spec.description
    assert "crashloop" not in spec.description


def test_trace_code_default_has_no_runbook_prebaked():
    """M3 核心：strategy=None（trace_code 默认）不预烤任何 runbook → discovery 而非 recognition。"""
    default = build_system_prompt(strategy=None, planning=False)
    assert "CrashLoopBackOff" not in default  # crashloop runbook 正文唯一标记
    assert default == build_system_prompt(strategy=None, planning=False)  # 恒等、无状态


def test_explicit_strategy_does_prebake():
    """M1 显式覆盖（golden/确定场景）仍预烤：strategy='crashloop' → 正文进 system prompt。"""
    pre = build_system_prompt(strategy="crashloop", planning=False)
    assert "CrashLoopBackOff" in pre
    # 渲染源 = RUNBOOKS['crashloop'].template
    from aidiag.prompts import render_runbook

    assert render_runbook(RUNBOOKS["crashloop"]) in pre
