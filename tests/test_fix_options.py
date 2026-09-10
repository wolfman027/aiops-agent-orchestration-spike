"""多方案（FixOption）解析与首选解析：纯离线单测，无 LLM / MCP。

契约见 prompts/conclusion.j2：recommended_fix 是**有序的方案列表**，每个方案带
applies_when（前提）+ recommended（首选）+ steps[]。根因依赖未定前提时给互斥多方案。
"""

from __future__ import annotations

from aidiag.domain import FixOption, RemediationStep, conclusion_from_dict, preferred_option_index


def _new_shape(**over: object) -> dict:
    base = {
        "title": "空安全 trim（assignee 可空）",
        "applies_when": "业务上 assignee 允许为空",
        "recommended": True,
        "reason": "最小改动",
        "steps": [
            {
                "action": "code_fix",
                "target": "src/main/java/acc/sipaiops/service/IncidentService.java",
                "change": "trim 前判空",
                "risk": "low",
                "rollback": "git revert 34ab3eec3483acee290fd694e9e7290d6a772571",
            }
        ],
    }
    base.update(over)
    return base


def test_parse_new_option_shape():
    conc = conclusion_from_dict({"recommended_fix": [_new_shape()]})
    assert len(conc.recommended_fix) == 1
    opt = conc.recommended_fix[0]
    assert opt.title == "空安全 trim（assignee 可空）"
    assert opt.applies_when == "业务上 assignee 允许为空"
    assert opt.recommended is True
    assert opt.reason == "最小改动"
    assert len(opt.steps) == 1
    assert opt.steps[0].action == "code_fix"
    assert opt.steps[0].target.endswith("IncidentService.java")
    assert opt.steps[0].risk == "low"


def test_parse_legacy_flat_step_wrapped():
    # 旧扁平形状（有 target/action、无 steps）→ 包成单方案，不丢
    legacy = {"action": "code_fix", "target": "IncidentService.java", "change": "trim 前判空"}
    conc = conclusion_from_dict({"recommended_fix": [legacy]})
    assert len(conc.recommended_fix) == 1
    opt = conc.recommended_fix[0]
    assert opt.title == "方案1"
    assert opt.recommended is False
    assert len(opt.steps) == 1
    assert opt.steps[0].target == "IncidentService.java"
    assert opt.steps[0].change == "trim 前判空"


def test_parse_mixed_and_non_dict_skipped():
    legacy = {"action": "restart", "target": "Deployment payment-db"}
    conc = conclusion_from_dict({"recommended_fix": [None, legacy, _new_shape()]})
    assert len(conc.recommended_fix) == 2
    assert conc.recommended_fix[0].steps[0].target == "Deployment payment-db"
    assert conc.recommended_fix[1].title == "空安全 trim（assignee 可空）"


def test_parse_single_dict_wrapped():
    # recommended_fix 给 dict 而非 list → 视为单元素列表
    conc = conclusion_from_dict({"recommended_fix": _new_shape()})
    assert len(conc.recommended_fix) == 1
    assert conc.recommended_fix[0].recommended is True


def test_parse_omitted_fix_is_empty():
    assert conclusion_from_dict({}).recommended_fix == []
    assert conclusion_from_dict({"recommended_fix": []}).recommended_fix == []


def test_recommended_tolerant_bool():
    assert conclusion_from_dict({"recommended_fix": [_new_shape(recommended="true")]}) \
        .recommended_fix[0].recommended is True
    assert conclusion_from_dict({"recommended_fix": [_new_shape(recommended="yes")]}) \
        .recommended_fix[0].recommended is True
    assert conclusion_from_dict({"recommended_fix": [_new_shape(recommended=False)]}) \
        .recommended_fix[0].recommended is False
    # 缺省 → False
    assert conclusion_from_dict({"recommended_fix": [_new_shape(recommended=None)]}) \
        .recommended_fix[0].recommended is False


def _opt(title: str, recommended: bool) -> FixOption:
    return FixOption(
        title=title,
        recommended=recommended,
        steps=[RemediationStep(target=f"Deployment {title}")],
    )


def test_preferred_option_index_resolution():
    assert preferred_option_index([]) is None
    # 零标记 → 0（方案1）
    assert preferred_option_index([_opt("a", False), _opt("b", False)]) == 0
    # 第二个标记 → 1
    assert preferred_option_index([_opt("a", False), _opt("b", True)]) == 1
    # 多个标记 → 第一个
    assert preferred_option_index([_opt("a", False), _opt("b", True), _opt("c", True)]) == 1
    # 第一个标记 → 0
    assert preferred_option_index([_opt("a", True), _opt("b", True)]) == 0


def test_two_mutually_exclusive_options_real_case():
    """复刻真实 NPE 案例：assignee 可空 vs 必填，是互斥方案而非同一方案的两步。"""
    conc = conclusion_from_dict(
        {
            "root_cause": "POST /api/sip-aiops/incidents 未传 assignee 时 NPE",
            "recommended_fix": [
                {
                    "title": "空安全 trim（assignee 可空）",
                    "applies_when": "业务上 assignee 允许为空",
                    "recommended": True,
                    "reason": "改动最小，且与 DDL DEFAULT NULL 一致",
                    "steps": [
                        {
                            "action": "code_fix",
                            "target": "src/main/java/acc/sipaiops/service/IncidentService.java",
                            "change": "第 72 行 trim 前判空",
                        }
                    ],
                },
                {
                    "title": "补 @NotBlank（assignee 必填）",
                    "applies_when": "业务上 assignee 为必填",
                    "recommended": False,
                    "reason": "若必填，应在入口拦截返回 400 而非存 null",
                    "steps": [
                        {
                            "action": "code_fix",
                            "target": "src/main/java/acc/sipaiops/dto/CreateIncidentRequest.java",
                            "change": "private String assignee 上加 @NotBlank",
                        }
                    ],
                },
            ],
        }
    )
    opts = conc.recommended_fix
    assert len(opts) == 2
    assert opts[0].recommended is True and opts[1].recommended is False
    # 两方案 target 不同、各自 1 步 —— 证明它们是备选而非同一方案的两步
    assert opts[0].steps[0].target != opts[1].steps[0].target
    assert all(len(o.steps) == 1 for o in opts)
    assert opts[0].applies_when and opts[1].applies_when
    assert preferred_option_index(opts) == 0
