"""路径匹配等小工具：供 fingerprint 与 diffstat 共用。"""

import fnmatch
import os


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
