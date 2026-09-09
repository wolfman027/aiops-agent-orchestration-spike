"""MCP server 配置存储（内存版，移植 agentflow/api/mcp_store.py 的方法面）。

记录形状与参考库的 ``_from_row`` 输出一致：``id/name/transport/config/is_stateful/
enable_tools/disable_tools/tools/enabled/created_at/updated_at``。**进程内**实现：
- 不做 sqlite/Postgres 落库（spike 配置收敛在 profiles + env，真 server url 靠 env 覆盖）；
- 方法名/语义照参考：``save/list/list_enabled/get/update/update_tools/delete``，
  运行时 manager 只读 ``list_enabled``，CRUD 供测试/控制台用。

记录只描述 server；**不存 agent 绑定**（绑定在 profiles 按 case_type 建模）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MCPStore:
    """内存版 MCP server 配置存储（方法面与参考 MCPStore 一致）。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    # ---- 连接等价物（内存态无需 connect/close；保留空实现供统一调用） ----
    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self._rows.clear()

    async def save(self, data: dict[str, Any]) -> str:
        """保存一条配置，返回 id。name 冲突抛 ValueError。"""
        name = data["name"]
        if any(r["name"] == name for r in self._rows.values()):
            raise ValueError(f"MCP server name 已存在: {name}")
        now = _now()
        mid = uuid.uuid4().hex[:12]
        self._rows[mid] = {
            "id": mid,
            "name": name,
            "transport": data["transport"],  # 'stdio' | 'http'
            "config": dict(data["config"]),
            "is_stateful": bool(data.get("is_stateful")),
            "enable_tools": list(data["enable_tools"]) if data.get("enable_tools") else None,
            "disable_tools": list(data["disable_tools"]) if data.get("disable_tools") else None,
            "tools": list(data["tools"]) if data.get("tools") else None,
            "enabled": bool(data.get("enabled", True)),
            "created_at": now,
            "updated_at": now,
        }
        return mid

    async def list(self) -> list[dict[str, Any]]:
        rows = sorted(self._rows.values(), key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows]

    async def list_enabled(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows.values() if r["enabled"]]

    async def get(self, mid: str) -> dict[str, Any] | None:
        r = self._rows.get(mid)
        return dict(r) if r is not None else None

    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        for r in self._rows.values():
            if r["name"] == name:
                return dict(r)
        return None

    async def update(self, mid: str, data: dict[str, Any]) -> bool:
        row = self._rows.get(mid)
        if row is None:
            return False
        if "name" in data and any(
            r["name"] == data["name"] and r["id"] != mid for r in self._rows.values()
        ):
            raise ValueError(f"MCP server name 已存在: {data['name']}")
        now = _now()
        for key in ("name", "transport", "is_stateful", "enabled"):
            if key in data:
                row[key] = data[key]
        for key in ("config", "enable_tools", "disable_tools", "tools"):
            if key in data:
                v = data[key]
                row[key] = dict(v) if isinstance(v, dict) else (
                    list(v) if isinstance(v, list) else v
                )
        row["updated_at"] = now
        return True

    async def update_tools(self, mid: str, tools: list[dict[str, Any]] | None) -> bool:
        row = self._rows.get(mid)
        if row is None:
            return False
        row["tools"] = list(tools) if tools is not None else None
        row["updated_at"] = _now()
        return True

    async def delete(self, mid: str) -> bool:
        return self._rows.pop(mid, None) is not None

    async def seed_if_empty(self, rows: list[dict[str, Any]]) -> list[str]:
        """无任何记录时灌入 seed 行，返回新增 id 列表（幂等：已有记录则跳过）。"""
        if self._rows:
            return []
        ids = []
        for data in rows:
            ids.append(await self.save(data))
        return ids
