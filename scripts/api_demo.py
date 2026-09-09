"""API 演示：POST /diagnose → 轮询 /status 观察任务与工具进度 → completed 打印结论。

用法（先起服务）：
    make api        # 或 uv run python scripts/run_api.py（host/port 读 .env）
    uv run python scripts/api_demo.py [--base http://127.0.0.1:8017] [--strategy crashloop]
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--base",
        default=None,
        help="服务地址；默认跟随 .env 的 AIDIAG_API_HOST/AIDIAG_API_PORT",
    )
    ap.add_argument("--strategy", default="crashloop")
    ap.add_argument("--timeout", type=float, default=240.0)
    args = ap.parse_args()

    settings = get_settings()
    base = args.base or f"http://{settings.api_host}:{settings.api_port}"
    body = dict(CASE_A)
    if args.strategy:
        body["strategy"] = args.strategy
    async with httpx.AsyncClient(base_url=base, timeout=30) as c:
        r = await c.post("/diagnose", json=body)
        r.raise_for_status()
        sid = r.json()["session_id"]
        print(f"session_id = {sid}")

        t0 = time.time()
        last_tasks = ""
        while time.time() - t0 < args.timeout:
            snap = (await c.get(f"/status/{sid}")).json()
            status = snap["status"]
            tasks = [(t["id"], t["title"], t["status"]) for t in snap["tasks"]]
            calls = [f"{e['tool']}:{e['status']}" for e in snap.get("tool_calls", [])]
            sig = f"[{status}] tasks={tasks} calls={calls}"
            if sig != last_tasks:
                print(sig)
                last_tasks = sig
            if status in ("completed", "failed", "closed_manual"):
                break
            await asyncio.sleep(2)

        snap = (await c.get(f"/status/{sid}")).json()
        print("=== FINAL status:", snap["status"], "error:", snap.get("error"))
        if snap.get("conclusion"):
            conc = snap["conclusion"]
            print("root_cause:", conc.get("root_cause"))
            print(
                "recommended_fix:",
                json.dumps(conc.get("recommended_fix"), ensure_ascii=False, indent=2),
            )


if __name__ == "__main__":
    asyncio.run(main())
