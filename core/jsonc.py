# core/jsonc.py
"""MaaFramework Pipeline 使用 JSONC 格式，标准 json 模块不支持注释。
提供与 json 模块一致的 loads / load 接口，先剥离注释再解析。"""
from __future__ import annotations

import json
from typing import Any, IO


def strip_comments(text: str) -> str:
    result: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escape = False

    while i < n:
        c = text[i]

        if in_string:
            result.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue

        # 非字符串状态
        if c == '"':
            in_string = True
            result.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            # 行注释：跳到行尾，保留 \n
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            # 块注释：跳到 */
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            result.append(c)
            i += 1

    return "".join(result)


def loads(text: str, **kwargs: Any) -> Any:
    return json.loads(strip_comments(text), **kwargs)


def load(fp: IO[str], **kwargs: Any) -> Any:
    return loads(fp.read(), **kwargs)

def dump(obj: Any, fp: IO[str], **kwargs: Any) -> None:
    json.dump(obj, fp, **kwargs)