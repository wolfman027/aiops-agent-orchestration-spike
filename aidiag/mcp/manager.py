"""MCPClientManager：store 配置 → AgentScope MCPClient（移植 agentflow mcp_manager 语义）。

- ``load()``：启动读 ``store.list_enabled()`` 建 client（**不预连接**——stdio mock 子进程在
  首次真正下发的 ``clients_for`` 才拉起，让离线/机制测试零子进程开销）。
- ``clients_for(names)``：按 profile 的 server 名集合过滤 → 返回可用 client；stateful 未连接
  则**连接一次**（best-effort，失败跳过不阻塞一次 run）。
- ``allow_names_for(names)``：该 profile 可见 MCP 工具的 AgentScope 精确名
  （``mcp__{server}__{tool}``，经 enable/disable 过滤），供权限 allow 注入。
- ``test_connection(row)``：临时建 client 连一次列工具，返回 ``{ok, tools, error?}``。
- 约定：key 用 **server 名**（MCPClient.name 唯一，profile 即按名绑）；单条配置坏/连不上
  只 log，不拖垮启动。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig

from .store import MCPStore

log = logging.getLogger("aidiag.mcp.manager")


def _exc_message(exc: BaseException) -> str:
    """把异常压成一行（ExceptionGroup 递归取首子异常，露出真实连接错误）。"""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc).strip()
    return text or type(exc).__name__


class MCPClientManager:
    """MCP server 配置 → AgentScope MCPClient 缓存（按 server 名）。"""

    def __init__(self, store: MCPStore) -> None:
        self._store = store
        self._clients: dict[str, MCPClient] = {}
        self._rows: dict[str, dict[str, Any]] = {}
        self._allow_cache: dict[int, list[str]] = {}

    # ------------------------------------------------------------------
    # client 构造
    # ------------------------------------------------------------------
    @staticmethod
    def _build_client(row: dict[str, Any]) -> MCPClient:
        cfg = row.get("config") or {}
        if row["transport"] == "stdio":
            mcp_config = StdioMCPConfig(
                command=cfg["command"],
                args=cfg.get("args"),
                env=cfg.get("env"),
                cwd=cfg.get("cwd"),
            )
            stateful = True  # stdio 强制 stateful（AgentScope 约束）
        elif row["transport"] == "http":
            mcp_config = HttpMCPConfig(
                url=cfg["url"],
                headers=cfg.get("headers"),
                timeout=cfg.get("timeout"),
            )
            stateful = bool(row.get("is_stateful"))
        else:
            raise ValueError(f"不支持的 transport: {row['transport']!r}")
        return MCPClient(
            name=row["name"],
            is_stateful=stateful,
            mcp_config=mcp_config,
            enable_tools=row.get("enable_tools"),
            disable_tools=row.get("disable_tools"),
        )

    async def _ensure_connected(self, client: MCPClient) -> None:
        if client.is_stateful and not client.is_connected:
            await client.connect()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def load(self) -> None:
        """建 enabled 记录的 client（不连接，首个 run 再拉起）。"""
        rows = await self._store.list_enabled()
        for row in rows:
            name = row["name"]
            try:
                client = self._build_client(row)
            except Exception as e:  # noqa: BLE001 —— 单条配置坏不拖垮启动
                log.warning("MCP[%s] 配置解析失败，跳过加载: %s", name, e)
                continue
            self._rows[name] = row
            self._clients[name] = client

    async def close_all(self) -> None:
        for name in list(self._clients):
            client = self._clients.pop(name, None)
            self._rows.pop(name, None)
            if client is None:
                continue
            self._allow_cache.pop(id(client), None)
            if client.is_stateful and client.is_connected:
                try:
                    await client.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("MCP[%s] close 失败: %s", client.name, e)

    async def refresh_server(self, name: str) -> None:
        """CRUD 后重建某 server（enabled=0/删除 → 只 evict）。"""
        client = self._clients.pop(name, None)
        self._rows.pop(name, None)
        if client is not None:
            self._allow_cache.pop(id(client), None)
            if client.is_stateful and client.is_connected:
                try:
                    await client.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("MCP[%s] close 失败: %s", client.name, e)
        row = await self._store.get_by_name(name)
        if row is None or not row.get("enabled"):
            return
        try:
            new_client = self._build_client(row)
        except Exception as e:  # noqa: BLE001
            log.warning("MCP[%s] 配置重建失败: %s", name, e)
            return
        self._rows[name] = row
        self._clients[name] = new_client

    # ------------------------------------------------------------------
    # 运行时查询（resolve_specs 用）
    # ------------------------------------------------------------------
    async def clients_for(self, names: set[str]) -> list[MCPClient]:
        """返回 profile 命中的可用 client（stateful 未连接 → 连一次；失败跳过）。"""
        result: list[MCPClient] = []
        for name in names:
            client = self._clients.get(name)
            if client is None:
                continue
            if client.is_stateful and not client.is_connected:
                try:
                    await self._ensure_connected(client)
                    log.info("MCP[%s] connect 成功", name)
                except Exception as e:  # noqa: BLE001
                    log.warning("MCP[%s] connect 失败，本次 run 跳过: %s", name, _exc_message(e))
                    continue
            result.append(client)
        return result

    async def allow_names_for(self, names: set[str]) -> list[str]:
        """profile 可见 MCP 工具的 AgentScope 精确名（list_tools 已应用 enable/disable）。"""
        out: list[str] = []
        for client in await self.clients_for(names):
            cached = self._allow_cache.get(id(client))
            if cached is not None:
                out.extend(cached)
                continue
            try:
                tools = await client.list_tools()
            except Exception as e:  # noqa: BLE001
                log.warning("MCP[%s] list_tools 失败，跳过 allow: %s", client.name, _exc_message(e))
                continue
            tool_names = [t.name for t in tools]
            self._allow_cache[id(client)] = tool_names
            out.extend(tool_names)
        return out

    # ------------------------------------------------------------------
    # 测试连接（不落库）
    # ------------------------------------------------------------------
    async def test_connection(
        self, row: dict[str, Any], *, timeout: float = 10.0
    ) -> dict[str, Any]:
        try:
            client = self._build_client(row)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "tools": [], "error": f"配置解析失败：{e}"}

        async def _probe() -> dict[str, Any]:
            try:
                await self._ensure_connected(client)
                tools = await client.list_raw_tools()
                rows = [{"name": t.name, "description": t.description or ""} for t in tools]
                return {"ok": True, "tools": rows}
            except asyncio.CancelledError:
                msg = "连接被中断：目标可达但鉴权失败或非 MCP 端点"
                return {"ok": False, "tools": [], "error": msg}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "tools": [], "error": _exc_message(e)}
            finally:
                if client.is_stateful and client.is_connected:
                    try:
                        await client.close()
                    except Exception:  # noqa: BLE001, S110
                        pass

        probe = asyncio.create_task(_probe())
        try:
            return await asyncio.wait_for(asyncio.shield(probe), timeout=timeout)
        except TimeoutError:
            probe.cancel()
            try:
                await probe
            except BaseException:  # noqa: BLE001, S110
                pass
            return {"ok": False, "tools": [], "error": f"连接超时（{int(timeout)} 秒）"}
        except asyncio.CancelledError:
            probe.cancel()
            try:
                await probe
            except BaseException:  # noqa: BLE001, S110
                pass
            raise
