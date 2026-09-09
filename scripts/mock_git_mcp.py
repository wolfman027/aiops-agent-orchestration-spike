#!/usr/bin/env python3
"""Mock git MCP server（FastMCP v1, stdio）——只读工具集，跑在真实 git 仓库上。

服务端暴露比 enable_tools 更宽的**只读白名单**之外，还故意带一个**写工具**（git_demo_write，
**不标** readOnlyHint）——用来验证配置层的第二道闸：server 暴露它、但 client 的
``enable_tools`` 不含它 → agent 根本看不到，自然调不到。只读工具全标
``readOnlyHint=True`` → AgentScope 只读路径自动 ALLOW。

仓库目录：env ``MOCK_GIT_REPO``（缺省 = 本脚本所在 git 仓库）。所有工具是**只读** git
命令（list argv，无 shell），返回 JSON 字符串。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP(name="git")


def _repo() -> Path:
    repo = os.environ.get("MOCK_GIT_REPO")
    if repo:
        return Path(repo).resolve()
    # 缺省：脚本自身所在目录（通常是 spike 仓库）
    return Path(__file__).resolve().parent.parent


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(  # noqa: S603 —— list argv，无 shell
        ["git", *args],
        cwd=str(cwd or _repo()),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:500])
    return proc.stdout


def _out(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def git_rev_parse(ref: str = "HEAD") -> str:
    """解析某 ref 的 commit sha（缺省 HEAD）。返回 {repo, ref, sha}。"""
    sha = _git("rev-parse", ref).strip()
    return _out({"repo": str(_repo()), "ref": ref, "sha": sha})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def git_status() -> str:
    """仓库工作区状态：当前分支 + 是否脏。返回 {branch, head, clean, changed}。"""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    head = _git("rev-parse", "HEAD").strip()
    porc = _git("status", "--porcelain").strip()
    changed = [ln.split(None, 1)[0] for ln in porc.splitlines() if ln] if porc else []
    return _out({"repo": str(_repo()), "branch": branch, "head": head, "changed": changed})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def git_log_s(search: str, path: str | None = None, ref_range: str | None = None) -> str:
    """git log -S：找「新增/删除某字符串」的 commit。range 形如 A..B（缺省仓库默认区间）。
    返回 [{sha, subject, date}]；无命中 → found=false。"""
    rng = ref_range if ref_range else "HEAD"
    args = ["log", "--format=%H|%ad|%s", "--date=iso", f"-S{search}", rng]
    if path:
        args.append("--")
        args.append(path)
    try:
        out = _git(*args)
    except RuntimeError as exc:
        return _out({"found": False, "error": str(exc), "search": search})
    rows = []
    for ln in out.splitlines():
        if not ln:
            continue
        sha, date, subject = ln.split("|", 2)
        rows.append({"sha": sha, "date": date, "subject": subject})
    return _out({"found": bool(rows), "search": search, "path": path, "range": rng, "commits": rows})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def git_grep(pattern: str, path: str | None = None, ref: str = "HEAD") -> str:
    """在 ref 树里 grep pattern（RE2 语法）。返回 [{sha_ref, file, line}]。"""
    args = ["grep", "-n", "-I", pattern, ref]
    if path:
        args = ["grep", "-n", "-I", pattern, ref, "--", path]
    try:
        out = _git(*args)
    except RuntimeError as exc:
        return _out({"found": False, "error": str(exc), "pattern": pattern})
    rows = []
    for ln in out.splitlines():
        # git grep 行形如 ref:file:lineno:text；split 前两段取 file:line
        try:
            rest = ln.split(":", 2)
            file_, line = rest[1], rest[2]
            rows.append({"ref": ref, "file": file_, "line": line})
        except (IndexError, ValueError):
            rows.append({"raw": ln})
    return _out({"found": bool(rows), "pattern": pattern, "path": path, "matches": rows})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def git_show_file(path: str, ref: str = "HEAD") -> str:
    """显示 ref 上某文件的完整内容（供读定位代码）。文件不存在 → 错误。"""
    try:
        content = _git("show", f"{ref}:{path}")
    except RuntimeError as exc:
        return _out({"found": False, "error": str(exc), "path": path, "ref": ref})
    return _out({"found": True, "path": path, "ref": ref, "content": content})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def git_blame(path: str, ref: str = "HEAD") -> str:
    """blame 某文件：每行 → {sha, author, line_no, code}。"""
    try:
        out = _git("blame", "--line-porcelain", ref, "--", path)
    except RuntimeError as exc:
        return _out({"found": False, "error": str(exc), "path": path})
    # line-porcelain 逐块：<sha> <orig> <final> <count> 行起，author 行、code 行（tab 后）
    rows = []
    cur: dict[str, str] | None = None
    line_no = 0
    for ln in out.splitlines():
        if not ln:
            continue
        head = ln.split(" ", 1)[0]
        if len(head) == 40:
            parts = ln.split()
            if len(parts) >= 4:
                try:
                    line_no = int(parts[2])
                except ValueError:
                    pass
            cur = {"sha": head, "line_no": line_no}
            continue
        if cur is not None and ln.startswith("author "):
            cur["author"] = ln.split(" ", 1)[1]
        elif cur is not None and ln.startswith("\t"):
            cur["code"] = ln[1:]
            rows.append(cur)
            cur = None
    return _out({"found": bool(rows), "path": path, "ref": ref, "lines": rows})


@mcp.tool()  # 故意不加 readOnlyHint：演示"写工具不该被 enable"的边界
def git_demo_write(path: str, content: str) -> str:
    """演示写工具：绝不写真实仓库，只写入 MOCK_SCRATCH_DIR（缺省 /tmp/gitmock）。"""
    scratch = Path(os.environ.get("MOCK_SCRATCH_DIR", "/tmp/gitmock"))
    scratch.mkdir(parents=True, exist_ok=True)
    target = (scratch / path).resolve()
    if not str(target).startswith(str(scratch.resolve())):
        return _out({"ok": False, "error": "path escapes scratch"})
    target.write_text(content)
    return _out({"ok": True, "path": str(target)})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
