"""宽容 JSON 提取：从 LLM 回复中抽出第一个完整 JSON 对象。

来源：agentflow ``agents/scopes.py::extract_json``（S-011 实测通过）。不用 AgentScope
结构化输出 API——提示词只要求「输出严格 JSON」，这里负责容忍围栏/前后缀噪音。
"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> dict[str, Any]:
    """从回复文本中提取第一个完整 JSON 对象（容忍 ``` 围栏与前后文噪音）。"""
    if not text:
        return {}
    text = re.sub(r"```(?:json)?|```", "", text)
    start = text.find("{")
    if start == -1:
        return {}
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                raw = text[start : i + 1]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {}
    return {}
