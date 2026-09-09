#!/usr/bin/env python3
"""Mock app-log MCP server（FastMCP v1, stdio）——应用日志/调用链查询原语。

对应 DESIGN §5 已确认的 app-log 原语：
- ``get_trace(app, trace_id)``：trace_id 拉单条 trace + 关联日志（第一跳，trace_bug/dependency）；
- ``query_logs(app, time_window, level)``：**app + 时间窗**拉日志（无 trace 场景，trace_startup）。

数据来源：env ``MOCK_APPLOG_DATA`` 指向一个 JSON 文件 {apps: {app: {traces: {...}, logs: [...]}}}；
缺省用内置 ``DEFAULT_DATA``（一条 DB 连接超时的失败 trace，含唯一 token）。live 换真实 app-log
url 即可，本 mock 只服务离线/冒烟。全部只读，``readOnlyHint=True``。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP(name="app-log")

# 唯一 token：只能靠 get_trace/query_logs 发现，禁止出现在提示词里
DEFAULT_TOKEN = "order-checkout-db-conn-timeout-9f3c"

DEFAULT_DATA: dict = {
    "apps": {
        "order-api": {
            "traces": {
                "T-88f1a2": {
                    "trace_id": "T-88f1a2",
                    "app": "order-api",
                    "status": "ERROR",
                    "root_span": "POST /checkout 2026-09-09T03:11:52Z",
                    "logs": [
                        {
                            "ts": "2026-09-09T03:11:53.210Z",
                            "level": "ERROR",
                            "msg": "checkout-db connection attempt failed, code="
                            + DEFAULT_TOKEN
                            + ", host=checkout-db.prod.svc.cluster.local:5432",
                        },
                        {
                            "ts": "2026-09-09T03:11:53.215Z",
                            "level": "ERROR",
                            "msg": "java.net.ConnectException: Connection refused "
                            "(Connection refused) to checkout-db.prod.svc.cluster.local:5432",
                        },
                        {
                            "ts": "2026-09-09T03:11:53.216Z",
                            "level": "ERROR",
                            "msg": "at com.acme.order.CheckoutService.persist(CheckoutService.java:142)",
                        },
                    ],
                }
            },
            "startup_logs": [
                {
                    "ts": "2026-09-09T03:10:00.001Z",
                    "level": "INFO",
                    "msg": "Starting OrderApplication on pid 1",
                },
                {
                    "ts": "2026-09-09T03:10:02.100Z",
                    "level": "ERROR",
                    "msg": "HikariPool-1 - Exception during pool initialization",
                },
                {
                    "ts": "2026-09-09T03:10:02.150Z",
                    "level": "ERROR",
                    "msg": "checkout-db connection attempt failed, code="
                    + DEFAULT_TOKEN
                    + ", host=checkout-db.prod.svc.cluster.local:5432",
                },
            ],
        }
    }
}


def _data() -> dict:
    path = os.environ.get("MOCK_APPLOG_DATA")
    if path:
        return json.loads(Path(path).read_text())
    return DEFAULT_DATA


def _out(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_trace(app: str, trace_id: str) -> str:
    """按 trace_id 拉单条 trace + 关联日志（app: 应用名, trace_id: 调用链 id）。"""
    data = _data()
    app_data = data.get("apps", {}).get(app)
    if app_data is None:
        return _out({"found": False, "error": f"未知应用 {app}", "query": {"app": app, "trace_id": trace_id}})
    trace = app_data.get("traces", {}).get(trace_id)
    if trace is None:
        return _out(
            {
                "found": False,
                "error": f"trace {trace_id} 不存在（app={app}）",
                "query": {"app": app, "trace_id": trace_id},
            }
        )
    return _out({"found": True, "query": {"app": app, "trace_id": trace_id}, **trace})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def query_logs(app: str, time_window: str | None = None, level: str | None = None) -> str:
    """无 trace 场景：按 app + 时间窗拉日志（app: 应用名, time_window: 形如 'T1/T2' 可选）。"""
    data = _data()
    app_data = data.get("apps", {}).get(app)
    if app_data is None:
        return _out({"found": False, "error": f"未知应用 {app}", "query": {"app": app, "time_window": time_window}})
    lines = list(app_data.get("startup_logs", []))
    if level:
        lines = [ln for ln in lines if ln["level"] == level.upper()]
    return _out(
        {
            "found": bool(lines),
            "query": {"app": app, "time_window": time_window, "level": level},
            "log_lines": lines,
        }
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
