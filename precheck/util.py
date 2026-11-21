"""路径匹配等小工具：供 fingerprint 与 diffstat 共用。"""

import fnmatch
import os
import re


def to_posix(path):
    """把本机路径分隔符统一成 /，便于跨平台比对与展示。"""
    posix = path.replace(os.sep, "/")
    if os.altsep:
        posix = posix.replace(os.altsep, "/")
    return posix


def has_glob_chars(text):
    return any(ch in text for ch in "*?[")


def match_any(rel_posix, patterns):
    """判断相对路径（posix 形式）是否命中任一 include/exclude 模式。

    匹配规则（任一命中即算命中）：
    - 模式以 / 结尾或不含 / 且无通配符时，按目录名匹配任意一层路径段
      （例如 target/ 或 target 能排除任意层级下的 target 目录）；
    - 用 fnmatch 对完整相对路径匹配；
    - 用 fnmatch 对文件名（最后一段）匹配。
    """
    if not patterns:
        return False
    name = rel_posix.rsplit("/", 1)[-1]
    segments = rel_posix.split("/")
    for pat in patterns:
        p = pat.replace("\\", "/")
        core = p.rstrip("/")
        if not core:
            continue
        if "/" not in core and not has_glob_chars(core):
            if core in segments:
                return True
        if fnmatch.fnmatch(rel_posix, p) or fnmatch.fnmatch(rel_posix, core):
            return True
        if fnmatch.fnmatch(name, core):
            return True
        # 形如 "src/*.py" 的模式也允许匹配任意前缀层级
        if fnmatch.fnmatch(rel_posix, "*/" + core):
            return True
    return False


def normalize_declared(pattern):
    """声明清单里的一行：去首尾空白，反斜杠转 /，去掉开头的 ./ 与 /。"""
    p = pattern.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def declared_to_regex(pattern):
    """把声明清单模式转成正则（对相对仓库根的完整 posix 路径整串匹配）。

    - ``**`` 跨目录（可为零层）："a/**/b" 命中 a/b、a/x/y/b；"a/**" 命中 a 下全部；
    - ``*`` 与 ``?`` 不跨 ``/``；
    - ``[...]`` 为字符类，``[!...]`` 为取反；
    - 以 ``/`` 结尾表示整个目录（等价于 "目录/**"）；
    - 区分大小写。
    """
    p = normalize_declared(pattern)
    if p.endswith("/"):
        p += "**"
    out = []
    i, n = 0, len(p)
    while i < n:
        ch = p[i]
        if ch == "*":
            if i + 1 < n and p[i + 1] == "*":
                i += 2
                if i < n and p[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            start = i + 1
            if start < n and p[start] in "!^":
                start += 1
            if start < n and p[start] == "]":
                start += 1
            j = p.find("]", start)
            if j == -1:
                out.append(re.escape(ch))
            else:
                body = p[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)
