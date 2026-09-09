"""repo-state 工具（B7）：只读取仓库 HEAD，显式喂 sha 给 LLM。

版本对齐纪律要求 agent「只做选择/引用、绝不拼写 sha」。repo-state 是那条链的锚：
- 返回 ``head``（完整 sha）→ 进 recorder 的工具返回集 → 事后 B7 校验（plan 里出现的任何
  sha 必须命中工具返回中出现过的 sha）。
- 无模型可选路径：spec 在 resolve_specs 时按 ``issue.repo`` 绑定到某个 **允许的 repo cwd**，
  agent 拿不到任意路径（不给注入面），也不需要 ls-remote 之类多一跳。
- 全部只读（``git rev-parse`` / ``rev-parse --abbrev-ref``），不改环境。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any

from .registry import ToolSpec

log = logging.getLogger("aidiag.tools.repo_state")


def _git(cwd: Path, *args: str) -> str:
    """同步跑一条 git 只读命令（list argv，无 shell）。"""
    proc = subprocess.run(  # noqa: S603 —— list argv，无 shell 注入面
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:400])
    return proc.stdout.strip()


async def _git_async(cwd: Path, *args: str) -> str:
    """异步包一层：run_diagnose 在事件循环里调工具，避免阻塞。"""
    return await asyncio.to_thread(_git, cwd, *args)


def build_repo_state_spec(cwd: Path) -> ToolSpec:
    """构造绑定到单个 repo 工作区的 repo_state 工具。

    resolve_specs 按 ``issue.repo`` 从允许映射挑出该工作区路径再绑定；每个 spec 只认自己
    那个仓库，agent 不能传任意路径。
    """
    cwd = cwd.resolve()

    async def repo_state() -> dict[str, Any]:
        """只读取当前仓库 HEAD：返回 commit sha 与分支，供 diff 落点 / sha 校验锚定。"""
        if not (cwd / ".git").exists() and not (cwd / ".git").is_file():
            return {
                "found": False,
                "error": f"{cwd} 不是 git 仓库（无 .git）",
                "query": {"cwd": str(cwd)},
            }
        try:
            head, branch = await asyncio.gather(
                _git_async(cwd, "rev-parse", "HEAD"),
                _git_async(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
            )
        except Exception as exc:  # noqa: BLE001 —— git 失败走工具错误纪律，回传 query
            log.warning("repo_state git 失败 cwd=%s: %s", cwd, exc)
            return {
                "found": False,
                "error": str(exc),
                "query": {"cwd": str(cwd), "cmd": "git rev-parse HEAD"},
            }
        return {
            "found": True,
            "cwd": str(cwd),
            "head": head,
            "branch": branch,
            "note": "HEAD 是 diff 落点基准（base_commit）；只引用，不要凭记忆拼写 sha。",
        }

    return ToolSpec(
        name="repo_state",
        description=(
            "只读取当前仓库的 HEAD commit sha 与分支。诊断要出 code_fix / 引 sha 前先调它拿"
            "权威 HEAD；plan 里出现的任何 sha（base_commit / evidence.commit 等）必须来自"
            "工具返回，绝不自己拼写。"
        ),
        func=repo_state,
    )
