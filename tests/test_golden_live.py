"""Golden 用例 live 跑分（真实 DeepSeek），-m live。

断言复用 ``scripts/run_golden.py::run_case``（读取 evals/golden_crashloop.yaml）：
只针对「只能靠查询发现」的唯一 token 与查证范围断言，不做诊断质量之外的检查。

运行：
    uv run pytest -m live tests/test_golden_live.py -v
需 .env 提供 DEEPSEEK_API_KEY；未配置时跳过。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from aidiag.config import get_settings

ROOT = Path(__file__).resolve().parent.parent


def _load_run_golden():
    path = ROOT / "scripts" / "run_golden.py"
    spec = importlib.util.spec_from_file_location("run_golden_mod", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_golden_crashloop_real_deepseek():
    if not get_settings().deepseek_api_key:
        pytest.skip("DEEPSEEK_API_KEY 未配置，跳过 live golden")
    rg = _load_run_golden()
    cfg = yaml.safe_load((ROOT / "evals" / "golden_crashloop.yaml").read_text("utf-8"))
    ok, failures, _raw = await rg.run_case(cfg)
    assert ok, "golden crashloop 未通过:\n" + "\n".join(f"  - {f}" for f in failures)
