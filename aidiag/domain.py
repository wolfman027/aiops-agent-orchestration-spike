"""领域模型：Issue / Evidence / RemediationStep / DiagnosticConclusion（TRACE_CODE_DESIGN §3）。

与 prompts/conclusion.j2 的 JSON 契约一一对应；parse 时宽容（缺字段给默认）。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---- 合法枚举（L1/L2 契约）----
_CASE_TYPES = ("trace_code", "k8s")
_ACTIONS = ("code_fix", "config_change", "restart", "scale", "db_action")
_RISK_LEVELS = ("low", "medium", "high")
_SHA_RE = re.compile(r"\b[0-9a-fA-F]{7,64}\b")


class Issue(BaseModel):
    """一条待诊断请求。只带"去哪查 + 基线"，绝不携带 bug 签名/根因方向（防作弊）。"""

    id: str = "issue-1"
    case_type: Literal["trace_code", "k8s"] = "trace_code"  # 外层平台按可达 MCP 分派定
    title: str = ""
    description: str = ""  # trace_code 下可只写"诊断 trace T-xxx 的失败请求"
    app: str | None = None  # 日志源身份（agent 第一跳）
    repo: str | None = None  # git 仓库；app→repo 一一对应时可省略
    trace_id: str | None = None  # 日志关联键（强过滤）
    environment: str | None = None
    time_window: str | None = None  # 可选提示，如 "2026-09-08T01:05Z/01:40Z"
    deployed: DeployedRef | None = None  # 线上基线（优 = 平台直接给 commit）
    namespace: str = ""  # k8s legacy；trace_code 下留空
    strategy: str | None = None  # 显式 runbook 名；trace_code 默认不设（走 M3 fetch）


class DeployedRef(BaseModel):
    kind: Literal["commit", "image"]
    value: str  # commit sha 或 image tag（如 checkout:1.4.2）
    source: Literal["platform", "log_meta", "git_tag"] = "platform"


class EvidenceItem(BaseModel):
    source: str = Field(default="", description="app_log / git_log / git_blame / git_grep ...")
    query: str = Field(default="", description="实际传入的关键参数，如 trace_id/repo+pattern")
    finding: str = Field(default="", description="该证据支持的判断")
    supporting_text: str = Field(default="", description="工具输出原文引用，保留唯一标识")
    commit: str | None = Field(default=None, description="可选：这条证据取自哪个 commit")


class RemediationStep(BaseModel):
    """只读的『建议修复』（给人批复）；执行与否由审批决定，本对象不携带执行语义。"""

    action: Literal["code_fix", "config_change", "restart", "scale", "db_action"] = "code_fix"
    target: str = Field(default="", description="infra=资源名；code=文件路径（须与 git 返回一致）")
    change: str = Field(default="", description="纯文本'怎么改'（code_fix 必填）")
    expected_effect: str = ""
    risk: Literal["low", "medium", "high"] = "medium"
    rollback: str = ""
    suggested_diff: str | None = Field(default=None, description="示意 diff（给人看、无执行语义）")


class DiagnosticConclusion(BaseModel):
    """只读诊断的终点 = 给人批复的 plan。recommended_fix 只是建议，不执行。"""

    root_cause: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"
    summary: str = ""

    # 版本对齐（TRACE_CODE_DESIGN §4）
    deployed_commit: str | None = None  # 证据锚点：日志栈帧对应的线上 sha
    base_commit: str | None = None  # diff 落点：默认 HEAD tip；必须显式给出
    base_note: str = ""  # deployed≠base 时写 case a/b/c 判定
    already_fixed_by: str | None = None  # 非空=无需新改动；recommended_fix 应为空

    evidence: list[EvidenceItem] = Field(default_factory=list)
    recommended_fix: list[RemediationStep] = Field(default_factory=list)


# ======================================================================
# 宽容解析（LLM dict → 模型）
# ======================================================================
def _str(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _opt_str(v: Any) -> str | None:
    """可选字符串字段：空白/缺省 → None；否则原样（commit sha 不做截断）。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def conclusion_from_dict(data: dict[str, Any]) -> DiagnosticConclusion:
    """宽容地把 LLM 输出的 dict 归一到 DiagnosticConclusion。"""
    evidence = []
    for e in data.get("evidence") or []:
        if isinstance(e, dict):
            evidence.append(
                EvidenceItem(
                    source=_str(e.get("source")),
                    query=_str(e.get("query")),
                    finding=_str(e.get("finding")),
                    supporting_text=_str(e.get("supporting_text")),
                    commit=_opt_str(e.get("commit")),
                )
            )
    fix = []
    for r in data.get("recommended_fix") or []:
        if not isinstance(r, dict):
            continue
        action = _str(r.get("action")) or "code_fix"
        if action not in _ACTIONS:
            action = "code_fix"
        risk = r.get("risk") or "medium"
        if risk not in _RISK_LEVELS:
            risk = "medium"
        fix.append(
            RemediationStep(
                action=action,  # type: ignore[arg-type]
                target=_str(r.get("target")),
                change=_str(r.get("change")),
                expected_effect=_str(r.get("expected_effect")),
                risk=risk,  # type: ignore[arg-type]
                rollback=_str(r.get("rollback")),
                suggested_diff=_opt_str(r.get("suggested_diff")),
            )
        )
    confidence = data.get("confidence") or "medium"
    if confidence not in _RISK_LEVELS:
        confidence = "medium"
    return DiagnosticConclusion(
        root_cause=_str(data.get("root_cause")),
        confidence=confidence,  # type: ignore[arg-type]
        summary=_str(data.get("summary")),
        deployed_commit=_opt_str(data.get("deployed_commit")),
        base_commit=_opt_str(data.get("base_commit")),
        base_note=_str(data.get("base_note")),
        already_fixed_by=_opt_str(data.get("already_fixed_by")),
        evidence=evidence,
        recommended_fix=fix,
    )


# ======================================================================
# sha 幻觉纪律（TRACE_CODE_DESIGN §4.3）
# ======================================================================
def _sha_fields(conclusion: DiagnosticConclusion) -> list[tuple[str, str]]:
    """plan 中出现的全部 (字段名, sha)。base/deployed/already_fixed_by + 每条证据 commit。"""
    out: list[tuple[str, str]] = []
    for f in ("deployed_commit", "base_commit", "already_fixed_by"):
        v = getattr(conclusion, f)
        if isinstance(v, str) and v:
            out.append((f, v))
    for i, e in enumerate(conclusion.evidence):
        if isinstance(e.commit, str) and e.commit:
            out.append((f"evidence[{i}].commit", e.commit))
    return out


def hallucinated_shas(
    conclusion: DiagnosticConclusion,
    known_shas: set[str] | None,
) -> list[tuple[str, str]]:
    """返回 plan 中『从未在工具输出里出现过』的 (字段名, sha)。

    known_shas=None（未启用校验/纯解析路径）→ 视为空集；任何出现的 sha 都会被标为幻觉。
    agent 只做选择/引用，绝不拼 sha —— 未命中即幻觉。
    """
    known = known_shas or set()
    return [(f, v) for f, v in _sha_fields(conclusion) if v not in known]


def sanitize_hallucinated_shas(
    conclusion: DiagnosticConclusion,
    known_shas: set[str] | None,
    *,
    confidence_downgrade: str = "low",
) -> int:
    """就地清掉幻觉 sha（字段置 None）并降 confidence，返回清掉的个数。

    调用方（runner / /diagnose 落地前）在记录工具输出后以工具返回过的 sha 集合调用；
    golden/单元测试可单独测。
    """
    bad = hallucinated_shas(conclusion, known_shas)
    if not bad:
        return 0
    bad_fields = {f for f, _ in bad}
    for f in ("deployed_commit", "base_commit", "already_fixed_by"):
        if f in bad_fields:
            setattr(conclusion, f, None)
    for i, e in enumerate(conclusion.evidence):
        if f"evidence[{i}].commit" in bad_fields:
            e.commit = None
    conclusion.confidence = confidence_downgrade  # type: ignore[assignment]
    return len(bad)


def looks_like_sha(s: str) -> bool:
    return bool(_SHA_RE.fullmatch(s))
