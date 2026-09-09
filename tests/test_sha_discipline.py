"""sha 幻觉纪律（TRACE_CODE_DESIGN §4.3 / B7）：plan 出现的 sha 必须来自工具返回。

纯离线单测：不跑 agent，直接对 conclusion + known_shas 调 domain 校验函数，断言消毒行为。
"""

from __future__ import annotations

from aidiag.domain import (
    DiagnosticConclusion,
    EvidenceItem,
    hallucinated_shas,
    sanitize_hallucinated_shas,
)

# 全 40 hex；REAL 模拟 git 工具真实返回，FAKE 模拟模型拼出来的 sha
REAL = "9a2ff1fe3d4850a4d1292999f6d224f1fa65c08f"
FAKE = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _conc(*, already_fixed_by: str | None = None) -> DiagnosticConclusion:
    return DiagnosticConclusion(
        root_cause="启动连接 DB 超时",
        confidence="high",
        deployed_commit=REAL,
        base_commit=FAKE,  # 这条没在工具返回里出现过 → 幻觉
        already_fixed_by=already_fixed_by,
        evidence=[
            EvidenceItem(source="git_rev_parse", finding="HEAD", commit=REAL),
            EvidenceItem(source="git_log_s", finding="修复提交", commit=FAKE),
        ],
    )


def test_known_sha_kept_unknown_cleared_and_confidence_downgraded():
    conc = _conc(already_fixed_by=FAKE)
    n = sanitize_hallucinated_shas(conc, {REAL})
    assert n == 3  # base_commit + evidence[1].commit + already_fixed_by
    # 真 sha 保留
    assert conc.deployed_commit == REAL
    assert conc.evidence[0].commit == REAL
    # 幻觉 sha 清空
    assert conc.base_commit is None
    assert conc.already_fixed_by is None
    assert conc.evidence[1].commit is None
    # 降 confidence（默认 low）
    assert conc.confidence == "low"


def test_hallucinated_shas_lists_fields_with_fake():
    conc = _conc()
    bad = hallucinated_shas(conc, {REAL})
    fields = {f for f, _ in bad}
    assert fields == {"base_commit", "evidence[1].commit"}


def test_known_shas_none_means_everything_is_hallucinated():
    """known_shas=None = 纯解析路径（无工具输出可对照）→ 一律视为幻觉，倒逼 agent 引工具。"""
    conc = _conc()
    assert len(hallucinated_shas(conc, None)) == 4


def test_all_known_no_clear_no_downgrade():
    conc = _conc()
    n = sanitize_hallucinated_shas(conc, {REAL, FAKE})
    assert n == 0
    assert conc.base_commit == FAKE
    assert conc.confidence == "high"  # 无幻觉时不降级


def test_empty_known_set_clears_every_commit_field():
    """run_diagnose 在没有任何 sha 型工具被调用时 known_shas 为空 → 全部 sha 都是幻觉。"""
    conc = _conc()
    n = sanitize_hallucinated_shas(conc, set())
    assert n == 4
    assert conc.deployed_commit is None
    assert conc.base_commit is None
    assert all(e.commit is None for e in conc.evidence)
