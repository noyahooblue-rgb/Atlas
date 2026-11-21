"""diffstat：git 改动量统计。

只调用 git 的只读子命令：status 与 diff --numstat（均在允许清单内），
绝不执行任何会改动仓库的命令。所有 git 调用都带
GIT_OPTIONAL_LOCKS=0，避免 status 刷新索引时写 .git/index。

基线（--base）不允许以 "-" 开头，且在 diff 调用里给它拼上 ^{tree}
后缀再传入：拼后缀后整个参数不可能被 git 当成选项解析（例如
--base=--output=... 这类注入会在第一步就被拒绝），不依赖
--end-of-options 这类新版本才有的选项。

输出每个仓库的已修改 / 已新增 / 已删除 / 未跟踪文件清单，
受跟踪文件相对基线（默认 HEAD）的增删行数，以及未跟踪文件的
行数（单独统计；二进制文件只标明是二进制，不数行数）。

可选 --declared FILE 做「声明清单对账」：实际改动（先经过
--include/--exclude 过滤）凡不在声明里的列为未声明改动，退出 1；
声明了却没改的只提示。可选 --java-classes 把 src/main/java/ 下的
改动 .java 文件转成全限定类名一行输出。
"""

import json
import os
import re
import subprocess

from .errors import PrecheckError
from .util import has_glob_chars, match_any, to_posix


def _git_env():
    """git 子进程环境：GIT_OPTIONAL_LOCKS=0 让只读命令不写索引。"""
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(repo, args):
    """在 repo 目录上执行只读 git 命令，返回 (returncode, stdout_bytes, stderr_text)。"""
    cmd = ["git", "-C", repo] + args
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=_git_env())
    except OSError as exc:
        raise PrecheckError("无法执行 git：%s" % exc)
    stderr = " ".join(proc.stderr.decode("utf-8", errors="replace").split())
    return proc.returncode, proc.stdout, stderr


def validate_base(base):
    """拒绝会被 git 当成选项解析的基线值（参数注入防护）。

    以 "-" 开头的值（例如 --output=/tmp/x）一律报错；空值同样拒绝。
    """
    if not base or not base.strip():
        raise PrecheckError("基线不能为空")
    if base.startswith("-"):
        raise PrecheckError(
            "基线不能以 - 开头（会被 git 当成选项解析）：%s" % base)
    return base


def _decode_path(raw):
    return raw.decode("utf-8", errors="replace")


def parse_status_z(data):
    """解析 git status --porcelain=v1 -z 输出。

    返回 [(xy_code, new_path, old_path_or_None), ...]，路径为 posix 形式。
    -z 模式下重命名条目顺序为：新路径 NUL 旧路径（与非 -z 相反）。
    """
    tokens = data.split(b"\x00")
    entries = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        idx += 1
        if len(tok) < 4:
            continue
        code = tok[:2].decode("ascii", errors="replace")
        path = _decode_path(tok[3:])
        old = None
        if "R" in code or "C" in code:
            if idx < len(tokens):
                old = _decode_path(tokens[idx])
                idx += 1
        entries.append((code, to_posix(path), to_posix(old) if old else None))
    return entries


def parse_numstat_z(data):
    """解析 git diff --numstat -z 输出，返回 {path: (ins, del)}。

    二进制文件的增删记为 (None, None)。重命名条目格式为
    "ins TAB del TAB" 空路径，随后两个 NUL 分隔的旧/新路径。
    """
    tokens = data.split(b"\x00")
    result = {}
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        idx += 1
        if not tok:
            continue
        parts = tok.split(b"\t")
        if len(parts) != 3:
            continue
        ins_raw, del_raw, path_raw = parts
        if path_raw == b"":
            if idx + 1 < len(tokens):
                _old = _decode_path(tokens[idx])
                new = _decode_path(tokens[idx + 1])
                idx += 2
                path = to_posix(new)
            else:
                continue
        else:
            path = to_posix(_decode_path(path_raw))
        if ins_raw == b"-" or del_raw == b"-":
            result[path] = (None, None)
        else:
            try:
                result[path] = (int(ins_raw), int(del_raw))
            except ValueError:
                result[path] = (None, None)
    return result


def count_untracked_lines(abs_path):
    """返回 (行数 或 None, 是否二进制)。读不到时行数为 None、二进制 False。"""
    try:
        with open(abs_path, "rb") as fh:
            head = fh.read(8192)
            if b"\x00" in head:
                return None, True
            rest = fh.read()
    except OSError:
        return None, False
    data = head + rest
    if not data:
        return 0, False
    lines = data.count(b"\n")
    if not data.endswith(b"\n"):
        lines += 1
    return lines, False


# ---------------------------------------------------------------------------
# 声明清单对账（--declared）
# ---------------------------------------------------------------------------

def load_declared(path):
    """读取声明文件，返回归一化后的模式列表（posix 分隔）。

    文件为 UTF-8 文本，每行一个相对仓库根目录的路径；# 开头为注释，
    空行忽略；路径分隔符 / 与 \\ 都接受。文件不存在或读不了时抛
    PrecheckError（退出 2）。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise PrecheckError("无法读取声明文件 %s：%s" % (path, exc))
    except UnicodeDecodeError as exc:
        raise PrecheckError("声明文件 %s 不是 UTF-8 文本：%s" % (path, exc))
    patterns = []
    for line in text.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        patterns.append(item.replace("\\", "/"))
    return patterns


def _glob_to_regex(pat):
    """把 fnmatch 风格模式转成正则：* 不跨目录，** 跨目录。"""
    out = []
    idx = 0
    size = len(pat)
    while idx < size:
        ch = pat[idx]
        if ch == "*":
            if pat[idx:idx + 2] == "**":
                if pat[idx:idx + 3] == "**/":
                    out.append("(?:.*/)?")
                    idx += 3
                else:
                    out.append(".*")
                    idx += 2
            else:
                out.append("[^/]*")
                idx += 1
        elif ch == "?":
            out.append("[^/]")
            idx += 1
        elif ch == "[":
            end = idx + 1
            if end < size and pat[end] == "!":
                end += 1
            if end < size and pat[end] == "]":
                end += 1
            while end < size and pat[end] != "]":
                end += 1
            if end >= size:
                out.append(re.escape(ch))
                idx += 1
            else:
                stuff = pat[idx + 1:end]
                if stuff.startswith("!"):
                    stuff = "^" + stuff[1:]
                out.append("[%s]" % stuff)
                idx = end + 1
        else:
            out.append(re.escape(ch))
            idx += 1
    return re.compile("^%s$" % "".join(out))


_GLOB_CACHE = {}


def _pattern_hit(path, pattern):
    """单个声明模式是否命中 posix 相对路径 path。"""
    if has_glob_chars(pattern):
        regex = _GLOB_CACHE.get(pattern)
        if regex is None:
            regex = _glob_to_regex(pattern)
            _GLOB_CACHE[pattern] = regex
        return regex.match(path) is not None
    core = pattern.rstrip("/")
    if not core:
        return False
    # 无通配符：精确命中，或作为目录前缀命中其下全部文件
    return path == core or path.startswith(core + "/")


def reconcile(changed, declared):
    """对账。changed 为 [(kind, path, [备选路径...]), ...]。

    返回 (未声明改动 [{kind, path, renamed_from}], 声明未改动 [模式...])。
    重命名的新旧路径任一命中即算已声明。
    """
    matched = set()
    undeclared = []
    for kind, path, alternates in changed:
        hit = None
        for idx, pattern in enumerate(declared):
            if any(_pattern_hit(cand, pattern)
                   for cand in [path] + list(alternates)):
                hit = idx
                break
        if hit is None:
            item = {"path": path, "kind": kind}
            if alternates:
                item["renamed_from"] = alternates[0]
            undeclared.append(item)
        else:
            matched.add(hit)
    not_changed = [pat for idx, pat in enumerate(declared)
                   if idx not in matched]
    return undeclared, not_changed


JAVA_SRC_PREFIX = "src/main/java/"


def java_class_name(path):
    """src/main/java/ 下的 .java 路径转全限定类名；不合约定返回 None。"""
    if path.startswith(JAVA_SRC_PREFIX) and path.endswith(".java"):
        return path[len(JAVA_SRC_PREFIX):-len(".java")].replace("/", ".")
    return None


def repo_diffstat(repo, base, includes, excludes, declared=None,
                  java_classes=False):
    if not os.path.isdir(repo):
        raise PrecheckError("目录不存在：%s" % repo)
    validate_base(base)
    rc, status_out, stderr = _git(
        repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if rc != 0:
        raise PrecheckError("不是 git 工作区（或 git 报错）：%s %s"
                            % (repo, ("：" + stderr) if stderr else ""))
    # 给基线拼 ^{tree} 后缀：拼完的整体是一个树对象引用，不可能被
    # git 当成选项解析（与 validate_base 的拒绝规则双重防护）。
    tree_ref = base + "^{tree}"
    rc, numstat_out, stderr = _git(
        repo, ["diff", "--numstat", "-z", tree_ref, "--"])
    if rc != 0:
        raise PrecheckError("基线 %s 在仓库 %s 中不可用%s"
                            % (base, repo, ("：" + stderr) if stderr else ""))
    numstat = parse_numstat_z(numstat_out)
    # 第二次 diff 忽略行尾 CR 差异：在普通 numstat 里出现、但这次
    # 不出现的文件即「只差行尾」。旧版 git 不支持该选项时跳过标注。
    rc, eol_out, _stderr = _git(
        repo, ["diff", "--numstat", "-z", "--ignore-cr-at-eol",
               tree_ref, "--"])
    eol_numstat = parse_numstat_z(eol_out) if rc == 0 else None

    def wanted(path):
        if includes and not match_any(path, includes):
            return False
        if match_any(path, excludes):
            return False
        return True

    modified, added, deleted, untracked = [], [], [], []
    for code, path, old in parse_status_z(status_out):
        if not wanted(path):
            continue
        if code == "??":
            lines, binary = count_untracked_lines(
                os.path.join(repo, *path.split("/")))
            untracked.append({"path": path, "lines": lines,
                              "binary": binary})
        elif "A" in code:
            ins, dele = numstat.get(path, (None, None))
            added.append({"path": path, "insertions": ins,
                          "deletions": dele})
        elif "D" in code:
            ins, dele = numstat.get(path, (None, None))
            deleted.append({"path": path, "insertions": ins,
                            "deletions": dele})
        else:
            ins, dele = numstat.get(path, (None, None))
            item = {"path": path, "insertions": ins, "deletions": dele}
            if old:
                item["renamed_from"] = old
            if (ins is not None and eol_numstat is not None
                    and path not in eol_numstat):
                item["eol_only"] = True
            modified.append(item)

    tracked_ins = sum(i["insertions"] or 0 for i in
                      modified + added + deleted)
    tracked_del = sum(i["deletions"] or 0 for i in
                      modified + added + deleted)
    untracked_lines = sum(u["lines"] or 0 for u in untracked)
    result = {
        "repo": repo, "base": base,
        "modified": modified, "added": added, "deleted": deleted,
        "untracked": untracked,
        "tracked_insertions": tracked_ins,
        "tracked_deletions": tracked_del,
        "untracked_lines": untracked_lines,
    }
    if declared is not None:
        changed = []
        for item in modified:
            alts = [item["renamed_from"]] if item.get("renamed_from") \
                else []
            changed.append(("modified", item["path"], alts))
        for item in added:
            changed.append(("added", item["path"], []))
        for item in deleted:
            changed.append(("deleted", item["path"], []))
        for item in untracked:
            changed.append(("untracked", item["path"], []))
        undeclared, not_changed = reconcile(changed, declared)
        result["undeclared_changes"] = undeclared
        result["declared_not_changed"] = not_changed
    if java_classes:
        names = set()
        for item in modified + added:
            name = java_class_name(item["path"])
            if name:
                names.add(name)
        for item in untracked:
            name = java_class_name(item["path"])
            if name:
                names.add(name)
        result["java_classes"] = sorted(names)
    return result


def _fmt_tracked(item):
    text = item["path"]
    if item.get("renamed_from"):
        text = "%s（原 %s）" % (item["path"], item["renamed_from"])
    if item["insertions"] is None:
        text += "（二进制）"
    else:
        text += "（+%d -%d）" % (item["insertions"] or 0,
                                 item["deletions"] or 0)
    if item.get("eol_only"):
        text += "（只差行尾）"
    return text


_KIND_LABELS = {"modified": "已修改", "added": "已新增",
                "deleted": "已删除", "untracked": "未跟踪"}


def run_diffstat(repos, base, includes, excludes, as_json, out, err,
                 declared_path=None, java_classes=False):
    errors = []
    declared = None
    if declared_path is not None:
        try:
            declared = load_declared(declared_path)
        except PrecheckError as exc:
            errors.append(str(exc))
    results = []
    if not errors:
        for repo in repos:
            try:
                results.append(repo_diffstat(
                    repo, base, includes, excludes, declared=declared,
                    java_classes=java_classes))
            except PrecheckError as exc:
                errors.append(str(exc))
    undeclared_total = sum(len(r.get("undeclared_changes", []))
                           for r in results)

    if as_json:
        out(json.dumps({"repos": results, "errors": errors},
                       ensure_ascii=False))
    else:
        for r in results:
            out("仓库：%s（基线 %s）" % (r["repo"], r["base"]))
            out("  已修改 %d：" % len(r["modified"]))
            for item in r["modified"]:
                out("    %s" % _fmt_tracked(item))
            out("  已新增 %d：" % len(r["added"]))
            for item in r["added"]:
                out("    %s" % _fmt_tracked(item))
            out("  已删除 %d：" % len(r["deleted"]))
            for item in r["deleted"]:
                out("    %s" % _fmt_tracked(item))
            out("  未跟踪 %d：" % len(r["untracked"]))
            for item in r["untracked"]:
                if item["binary"]:
                    out("    %s（二进制）" % item["path"])
                elif item["lines"] is None:
                    out("    %s（无法读取）" % item["path"])
                else:
                    out("    %s（%d 行）" % (item["path"], item["lines"]))
            out("  汇总：受跟踪文件 +%d -%d 行；未跟踪文件 %d 行"
                "（二进制不计行数）" % (r["tracked_insertions"],
                                        r["tracked_deletions"],
                                        r["untracked_lines"]))
            if "undeclared_changes" in r:
                out("  未声明改动 %d：" % len(r["undeclared_changes"]))
                for item in r["undeclared_changes"]:
                    label = _KIND_LABELS.get(item["kind"], item["kind"])
                    text = item["path"]
                    if item.get("renamed_from"):
                        text = "%s（原 %s）" % (text,
                                                item["renamed_from"])
                    out("    %s（%s）" % (text, label))
                out("  声明未改动 %d：" % len(r["declared_not_changed"]))
                for pattern in r["declared_not_changed"]:
                    out("    %s" % pattern)
            if java_classes and r.get("java_classes"):
                out(",".join(r["java_classes"]))
        for message in errors:
            err("错误：%s" % message)
    if errors:
        return 2
    return 1 if undeclared_total else 0
