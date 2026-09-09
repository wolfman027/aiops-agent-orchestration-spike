"""启动 aidiag API 服务：host/port 从 .env 读取（AIDIAG_API_HOST / AIDIAG_API_PORT）。

用法：
    uv run python scripts/run_api.py [--reload]

等价于旧命令 `uv run uvicorn aidiag.api:app --port 8017`，但端口/主机收进 .env 统一管理。
"""

from __future__ import annotations

import argparse

import uvicorn

from aidiag.config import get_settings


def main() -> None:
    ap = argparse.ArgumentParser(description="启动 aidiag API 服务（host/port 读 .env）")
    ap.add_argument("--reload", action="store_true", help="开发热重载（默认关）")
    args = ap.parse_args()

    settings = get_settings()
    print(
        f"== 启动 aidiag API: http://{settings.api_host}:{settings.api_port} "
        f"(reload={args.reload}) =="
    )
    uvicorn.run("aidiag.api:app", host=settings.api_host, port=settings.api_port, reload=args.reload)


if __name__ == "__main__":
    main()
