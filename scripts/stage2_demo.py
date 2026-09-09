"""Stage2 计划级审批 demo（真实 DeepSeek）。

流程：/diagnose → 轮询 completed → /remediate → /approve
    --decision approve         → 终态签章 approved（零执行）
    --decision reject --feedback "..." → 并入 feedback 重跑（reanalyze_count+=1）→ completed

用法（先起 uvicorn）：
    uv run python scripts/stage2_demo.py --decision approve
    uv run python scripts/stage2_demo.py --decision reject \
        --feedback "DB 运维确认 checkout-db 正常，请复核配置指向"
"""

from __future__ import annotations

import argparse
import asyncio
import time

import httpx

from aidiag.config import get_settings

CASE_A = {
    "title": "[P0] payment/checkout 反复 CrashLoopBackOff，Deployment checkout 无可用副本",
    "description": (
        "从 2026-09-08T01:15Z 起，checkout 服务的 pod 持续重启并处于 CrashLoopBackOff，"
        "Deployment checkout 0/1 可用；支付请求超时。请定位根因并给出修复建议。"
    ),
    "namespace": "payment",
}


async def poll_until(client: httpx.AsyncClient, sid: str, timeout: float = 240.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        snap = (await client.get(f"/status/{sid}")).json()
        if snap["status"] in ("completed", "failed", "closed_manual", "approved"):
            return snap
        await asyncio.sleep(2)
    raise TimeoutError(f"session {sid} 未在 {timeout}s 内结束")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--base",
        default=None,
        help="服务地址；默认跟随 .env 的 AIDIAG_API_HOST/AIDIAG_API_PORT",
    )
    ap.add_argument("--decision", choices=["approve", "reject"], default="approve")
    ap.add_argument("--feedback", default="DB 运维确认 checkout-db 正常，请复核配置指向是否有误")
    args = ap.parse_args()
    settings = get_settings()
    base = args.base or f"http://{settings.api_host}:{settings.api_port}"

    async with httpx.AsyncClient(
        base_url=base, timeout=httpx.Timeout(180.0, connect=10.0)
    ) as c:
        created = await c.post("/diagnose", json=dict(CASE_A, strategy="crashloop"))
        sid = created.json()["session_id"]
        print(f"session_id={sid} 诊断中...")
        snap = await poll_until(c, sid)
        print(f"[{snap['status']}] reanalyze_count={snap.get('reanalyze_count')}")
        if snap["status"] != "completed":
            print("结论异常:", snap.get("error"))
            return

        plan = (await c.post(f"/remediate/{sid}")).json()
        print("remediate →", plan["remediation_status"], f"{len(plan['steps'])} steps")

        if args.decision == "approve":
            body = {"decision": "approve"}
        else:
            body = {"decision": "reject", "feedback": args.feedback}
        out = (await c.post(f"/approve/{sid}", json=body)).json()
        print(
            f"approve → session_status={out['session_status']} "
            f"remediation_status={out['remediation_status']} reanalyze={out['reanalyze_count']}"
        )

        if out["remediation_status"] == "approved":
            print("=== approved：终态签章，计划给人实施，agent 未做任何改动 ===")
            print(out.get("note", ""))
        elif out["remediation_status"] == "rejected":
            # 重跑中：等到 completed 看新结论（feedback 已并入）
            snap = await poll_until(c, sid)
            print(f"[重跑完成] status={snap['status']} reanalyze={snap.get('reanalyze_count')}")
            if snap.get("conclusion"):
                print("新 root_cause:", snap["conclusion"].get("root_cause")[:200])


if __name__ == "__main__":
    asyncio.run(main())
