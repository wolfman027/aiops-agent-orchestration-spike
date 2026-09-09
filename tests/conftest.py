"""pytest 共享 fixture。默认离线（无 LLM）；live 用例用 ``-m live`` 标记。"""

from __future__ import annotations

import pytest


@pytest.fixture
def canned_conclusion():
    from aidiag.domain import DiagnosticConclusion, EvidenceItem, RemediationStep

    return DiagnosticConclusion(
        root_cause=(
            "checkout 容器启动连接 checkout-db 超时（code=payment-checkout-db-conn-timeout-e9f2）"
        ),
        confidence="high",
        summary="describe_pod 见 exit_code=1；query_logs 见连接超时原文",
        evidence=[
            EvidenceItem(
                source="query_logs",
                query="payment/payment-checkout-6f9c8d7b5-xvz2p",
                finding="启动期数据库连接超时",
                supporting_text=(
                    "code=payment-checkout-db-conn-timeout-e9f2 "
                    "host=checkout-db.payment.svc.cluster.local:5432"
                ),
            )
        ],
        recommended_fix=[
            RemediationStep(
                action="config_change",
                target="Deployment checkout",
                expected_effect="恢复启动",
                risk="low",
                rollback="回滚配置",
            )
        ],
    )
