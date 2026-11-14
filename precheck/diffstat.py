"""diffstat：git 改动量统计。

只调用 git 的只读子命令：status 与 diff --numstat（均在允许清单内），
绝不执行任何会改动仓库的命令。

输出每个仓库的已修改 / 已新增 / 已删除 / 未跟踪文件清单，
受跟踪文件相对基线（默认 HEAD）的增删行数，以及未跟踪文件的
行数（单独统计；二进制文件只标明是二进制，不数行数）。
"""

import json
import os
import subprocess

from .errors import PrecheckError
from .util import match_any, to_posix


def _git(repo, args):
    """在 repo 目录上执行只读 git 命令，返回 (returncode, stdout_bytes, stderr_text)。"""
    cmd = ["git", "-C", repo] + args
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    except OSError as exc:
        raise PrecheckError("无法执行 git：%s" % exc)
    stderr = " ".join(proc.stderr.decode("utf-8", errors="replace").split())
    return proc.returncode, proc.stdout, stderr


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


def repo_diffstat(repo, base, includes, excludes):
    if not os.path.isdir(repo):
        raise PrecheckError("目录不存在：%s" % repo)
    rc, status_out, stderr = _git(
        repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if rc != 0:
        raise PrecheckError("不是 git 工作区（或 git 报错）：%s %s"
                            % (repo, ("：" + stderr) if stderr else ""))
    rc, numstat_out, stderr = _git(
        repo, ["diff", "--numstat", "-z", base, "--"])
    if rc != 0:
        raise PrecheckError("基线 %s 在仓库 %s 中不可用%s"
                            % (base, repo, ("：" + stderr) if stderr else ""))
    numstat = parse_numstat_z(numstat_out)

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
            modified.append(item)

    tracked_ins = sum(i["insertions"] or 0 for i in
                      modified + added + deleted)
    tracked_del = sum(i["deletions"] or 0 for i in
                      modified + added + deleted)
    untracked_lines = sum(u["lines"] or 0 for u in untracked)
    return {
        "repo": repo, "base": base,
        "modified": modified, "added": added, "deleted": deleted,
        "untracked": untracked,
        "tracked_insertions": tracked_ins,
        "tracked_deletions": tracked_del,
        "untracked_lines": untracked_lines,
    }


def _fmt_tracked(item):
    text = item["path"]
    if item.get("renamed_from"):
        text = "%s（原 %s）" % (item["path"], item["renamed_from"])
    if item["insertions"] is None:
        return text + "（二进制）"
    return "%s（+%d -%d）" % (text, item["insertions"] or 0,
                             item["deletions"] or 0)


def run_diffstat(repos, base, includes, excludes, as_json, out, err):
    results = []
    errors = []
    for repo in repos:
        try:
            results.append(repo_diffstat(repo, base, includes, excludes))
        except PrecheckError as exc:
            errors.append(str(exc))

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
        for message in errors:
            err("错误：%s" % message)
    return 2 if errors else 0
