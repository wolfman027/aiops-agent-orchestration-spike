"""Mock 只读数据源：K8s + 日志 + 指标。

数据集**不进入提示词**，只作为工具返回被 agent 查询到（反幻觉 golden 的基础）。
`build_datasources(env)` 把工具绑定到一份故障环境字典；`crashloop_env()` 返回
case A 的规范数据（logs 含唯一 token ``payment-checkout-db-conn-timeout-e9f2``，
只出现在 checkout 容器日志）。

env 形状见 ``crashloop_env()``。所有工具是 ``async def``，返回扁平 dict；未命中时
返回 ``{"found": False, "error": ..., "query": {...}}``（完整回传查询，供 LLM 自纠错，
对齐 v4 §5 硬规则①）。全部只读。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# 唯一 token：只能靠 query_logs 发现，禁止出现在提示词/告警里
GOLDEN_TOKEN = "payment-checkout-db-conn-timeout-e9f2"

_POD = "payment-checkout-6f9c8d7b5-xvz2p"
_TS = "2026-09-08T01:22:5xZ"


def crashloop_env() -> dict[str, Any]:
    """case A：payment 命名空间 checkout 服务 CrashLoopBackOff（DB 连接超时）。"""
    return {
        "namespace": "payment",
        "pods": {
            _POD: {
                "name": _POD,
                "namespace": "payment",
                "node": "worker-1",
                "phase": "Running",
                "status": "CrashLoopBackOff",
                "ready": False,
                "restarts": 7,
                "owner": {"kind": "ReplicaSet", "name": "payment-checkout-6f9c8d7b5"},
                "containers": [
                    {
                        "name": "checkout",
                        "image": "registry.infra/payment/checkout:1.4.2",
                        "ready": False,
                        "restart_count": 7,
                        "ports": [{"containerPort": 8080}],
                        "last_state": {
                            "terminated": {
                                "exit_code": 1,
                                "reason": "Error",
                                "started_at": "2026-09-08T01:22:11Z",
                                "finished_at": "2026-09-08T01:22:49Z",
                            }
                        },
                    }
                ],
                "events": [
                    {
                        "ts": "2026-09-08T01:23:05Z",
                        "type": "Warning",
                        "reason": "BackOff",
                        "message": (
                            f"Back-off restarting failed container checkout "
                            f"in pod {_POD} on worker-1"
                        ),
                    },
                    {
                        "ts": "2026-09-08T01:22:55Z",
                        "type": "Normal",
                        "reason": "Created",
                        "message": "Created container checkout",
                    },
                ],
            },
            "payment-checkout-gateway-7b9d2c4f1-k2n8m": {
                "name": "payment-checkout-gateway-7b9d2c4f1-k2n8m",
                "namespace": "payment",
                "node": "worker-2",
                "phase": "Running",
                "status": "Running",
                "ready": True,
                "restarts": 0,
                "owner": {"kind": "ReplicaSet", "name": "payment-checkout-gateway-7b9d2c4f1"},
                "containers": [
                    {
                        "name": "gateway",
                        "image": "registry.infra/payment/checkout-gateway:2.0.1",
                        "ready": True,
                        "restart_count": 0,
                        "ports": [{"containerPort": 9090}],
                    }
                ],
                "events": [],
            },
        },
        "deployments": {
            "checkout": {
                "name": "checkout",
                "namespace": "payment",
                "replicas": 1,
                "available": 0,
                "unavailable": 1,
            },
            "checkout-gateway": {
                "name": "checkout-gateway",
                "namespace": "payment",
                "replicas": 1,
                "available": 1,
                "unavailable": 0,
            },
        },
        "logs": {
            f"payment/{_POD}/checkout": [
                {
                    "ts": "2026-09-08T01:22:48Z",
                    "level": "INFO",
                    "msg": "Starting CheckoutApplication v1.4.2 using Java 17 "
                    "on 8f9d0a3c1b2e with PID 1",
                },
                {
                    "ts": "2026-09-08T01:22:49Z",
                    "level": "INFO",
                    "msg": 'The following 1 profile is active: "prod"',
                },
                {
                    "ts": "2026-09-08T01:22:49Z",
                    "level": "ERROR",
                    "msg": "HikariPool-1 - Exception during pool initialization.",
                },
                {
                    "ts": "2026-09-08T01:22:49Z",
                    "level": "ERROR",
                    "msg": f"checkout-db connection attempt failed, code={GOLDEN_TOKEN}, "
                    f"host=checkout-db.payment.svc.cluster.local:5432",
                },
                {
                    "ts": "2026-09-08T01:22:49Z",
                    "level": "ERROR",
                    "msg": "java.sql.SQLTransientConnectionException: HikariPool-1 - "
                    "Connection is not available, request timed out after 30000ms",
                },
                {
                    "ts": "2026-09-08T01:22:49Z",
                    "level": "ERROR",
                    "msg": "Caused by: java.net.ConnectException: Connection timed out "
                    "(Connection timed out) to "
                    "checkout-db.payment.svc.cluster.local/10.96.12.34:5432",
                },
                {
                    "ts": "2026-09-08T01:22:49Z",
                    "level": "ERROR",
                    "msg": "Application run failed, process exiting with exit code 1",
                },
            ],
        },
    }


def _find_pod(env: dict[str, Any], namespace: str, pod: str) -> dict[str, Any] | None:
    if namespace != env["namespace"]:
        return None
    return env["pods"].get(pod)


# ======================================================================
# 工具实现（async def → FunctionTool 自动推断 JSON schema）
# ======================================================================
async def list_pods(env: dict[str, Any], namespace: str, **_: Any) -> dict[str, Any]:
    pods = env["pods"]
    rows = [
        {
            "name": p["name"],
            "status": p["status"],
            "ready": p["ready"],
            "restarts": p["restarts"],
            "containers": [c["name"] for c in p["containers"]],
        }
        for p in pods.values()
    ]
    rows.sort(key=lambda r: r["name"])
    return {"namespace": namespace, "found": True, "pods": rows}


async def describe_pod(env: dict[str, Any], namespace: str, pod: str, **_: Any) -> dict[str, Any]:
    p = _find_pod(env, namespace, pod)
    if p is None:
        return {
            "found": False,
            "error": f"pod {namespace}/{pod} 不存在",
            "query": {"namespace": namespace, "pod": pod},
        }
    return {
        "namespace": namespace,
        "name": p["name"],
        "node": p["node"],
        "phase": p["phase"],
        "status": p["status"],
        "ready": p["ready"],
        "restarts": p["restarts"],
        "owner": p["owner"],
        "containers": [
            {
                "name": c["name"],
                "image": c["image"],
                "ready": c["ready"],
                "restart_count": c["restart_count"],
                "last_state": c.get("last_state"),
            }
            for c in p["containers"]
        ],
        "events": p["events"],
    }


async def query_logs(
    env: dict[str, Any],
    namespace: str,
    pod: str,
    container: str | None = None,
    level: str | None = None,
    tail: int = 200,
    **_: Any,
) -> dict[str, Any]:
    """查询容器日志。container 缺省时若 pod 只有一个容器则用该容器。"""
    p = _find_pod(env, namespace, pod)
    if p is None:
        return {
            "found": False,
            "error": f"pod {namespace}/{pod} 不存在",
            "query": {"namespace": namespace, "pod": pod, "container": container},
        }
    containers = [c["name"] for c in p["containers"]]
    if container is None:
        if len(containers) != 1:
            return {
                "found": False,
                "error": f"pod 有多个容器 {containers}，必须指定 container",
                "query": {"namespace": namespace, "pod": pod},
            }
        container = containers[0]
    if container not in containers:
        return {
            "found": False,
            "error": f"容器 {container} 不在 pod {namespace}/{pod} 的容器列表 {containers} 中",
            "query": {"namespace": namespace, "pod": pod, "container": container},
        }
    key = f"{namespace}/{pod}/{container}"
    lines = list(env["logs"].get(key, []))
    if level:
        lines = [ln for ln in lines if (ln["level"] == level.upper())]
    lines = lines[-tail:]
    return {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "level": level,
        "found": True,
        "log_lines": lines,
    }


async def query_metrics(
    env: dict[str, Any],
    namespace: str,
    pod: str,
    metric: str = "container_restarts",
    **_: Any,
) -> dict[str, Any]:
    p = _find_pod(env, namespace, pod)
    if p is None:
        return {
            "found": False,
            "error": f"pod {namespace}/{pod} 不存在",
            "query": {"namespace": namespace, "pod": pod, "metric": metric},
        }
    if metric != "container_restarts":
        return {
            "found": False,
            "error": f"不支持指标 {metric}（支持: container_restarts）",
            "query": {"namespace": namespace, "pod": pod, "metric": metric},
        }
    return {
        "namespace": namespace,
        "pod": pod,
        "metric": metric,
        "found": True,
        "series": [
            {"ts": "2026-09-08T01:00Z", "value": 2},
            {"ts": "2026-09-08T01:15Z", "value": 5},
            {"ts": "2026-09-08T01:22Z", "value": 7},
        ],
        "latest": p["restarts"],
    }


# ======================================================================
# 数据源工厂
# ======================================================================
ToolFunc = Callable[..., Any]


def build_datasources(env: dict[str, Any] | None = None) -> dict[str, ToolFunc]:
    """把只读工具绑定到指定环境，返回 name → async func。

    注意：闭包必须有**显式签名**（FunctionTool 靠签名推断参数 JSON schema，不能是
    ``lambda **kw``）。env 经闭包捕获，不暴露给模型。
    """
    env = env or crashloop_env()

    async def _list_pods(namespace: str) -> dict[str, Any]:
        return await list_pods(env, namespace)

    async def _describe_pod(namespace: str, pod: str) -> dict[str, Any]:
        return await describe_pod(env, namespace, pod)

    async def _query_logs(
        namespace: str,
        pod: str,
        container: str | None = None,
        level: str | None = None,
        tail: int = 200,
    ) -> dict[str, Any]:
        return await query_logs(env, namespace, pod, container=container, level=level, tail=tail)

    async def _query_metrics(
        namespace: str, pod: str, metric: str = "container_restarts"
    ) -> dict[str, Any]:
        return await query_metrics(env, namespace, pod, metric=metric)

    return {
        "list_pods": _list_pods,
        "describe_pod": _describe_pod,
        "query_logs": _query_logs,
        "query_metrics": _query_metrics,
    }


# 工具描述（FunctionTool description 用，需与函数签名对应参数说明）
TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_pods": (
        "列出命名空间下的所有 pod 及状态/重启次数（namespace: 必填）。先列后查，精确定位异常 pod。"
    ),
    "describe_pod": (
        "describe 一个 pod：状态、重启次数、owner(ReplicaSet/Deployment)、容器(镜像/restart_count/"
        "last_state/exit_code)、events（namespace: 必填, pod: 必填）。用于核对资源归属链。"
    ),
    "query_logs": (
        "查询容器应用日志行（namespace/pod 必填, container 可选，pod 单容器时可省略）。"
        "level 可过滤(ERROR/INFO...)，tail 控制返回行数。日志含精确错误消息（唯一错误码等）。"
    ),
    "query_metrics": (
        "查询指标，目前支持 metric=container_restarts"
        "（返回近 1 小时重启序列，namespace/pod 必填）。"
    ),
}
