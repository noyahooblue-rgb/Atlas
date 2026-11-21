"""diffstat：git 改动量统计。

只调用 git 的只读子命令：status、rev-parse --verify 与 diff --numstat，
绝不执行任何会改动仓库的命令。所有 git 调用都带环境变量
GIT_OPTIONAL_LOCKS=0，避免 git status 顺手刷新并写回 .git/index。

--base 的防注入（M1）：
- 以 "-" 开头的值直接拒绝（退出 2），不会交给 git；
- 先用 `git rev-parse --verify --quiet <base>^{commit}` 把基线解析成
  完整提交号，再把这个十六进制提交号（不可能以 "-" 开头）交给
  `git diff`，因此 diff 永远不会把用户输入当作选项解析。
  不依赖 --end-of-options（老版本 git 没有）。

输出每个仓库的已修改 / 已新增 / 已删除 / 未跟踪文件清单，
受跟踪文件相对基线（默认 HEAD）的增删行数，以及未跟踪文件的
行数（单独统计；二进制文件只标明是二进制，不数行数）。

可选：
- --declared FILE：与「本轮声明要改的文件」对账，列出未声明改动
  （退出 1）与声明未改动（只提示）；
- --java-classes：把 src/main/java/ 下的 .java 改动转成全限定类名，
  逗号分隔一行输出，便于填 PIT 的 -DtargetClasses。
"""

import json
import os
import re
import subprocess

from .errors import PrecheckError
from .util import declared_to_regex, match_any, normalize_declared, to_posix

_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
JAVA_ROOT = "src/main/java/"


def _git_env():
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # 不让 git 弹出分页器或交互式提示
    env["GIT_PAGER"] = "cat"
    env["GIT_TERMINAL_PROMPT"] = "0"
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


def _decode_path(raw):
    return raw.decode("utf-8", errors="replace")


def validate_base(base):
    """拒绝空值和以 - 开头的基线（防止被 git 当作选项）。"""
    if base is None or not str(base).strip():
        raise PrecheckError("--base 不能为空")
    if str(base).startswith("-"):
        raise PrecheckError(
            "--base 的值不能以 - 开头（疑似选项注入，已拒绝）：%s" % base)
    if "\x00" in str(base) or "\n" in str(base):
        raise PrecheckError("--base 含非法字符")
    return str(base)


def resolve_base(repo, base):
    """把基线解析成完整提交号；解析失败报错。"""
    rc, out, stderr = _git(
        repo, ["rev-parse", "--verify", "--quiet", base + "^{commit}"])
    sha = out.decode("ascii", errors="replace").strip()
    if rc != 0 or not _SHA_RE.match(sha):
        raise PrecheckError("基线 %s 在仓库 %s 中不可用%s"
                            % (base, repo, ("：" + stderr) if stderr else ""))
    return sha


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
# 声明清单（M3）
# ---------------------------------------------------------------------------

def load_declared(path):
    """读声明清单，返回 [(原始行, 规范化模式, 正则), ...]。"""
    if not os.path.isfile(path):
        raise PrecheckError("声明清单不存在或不是文件：%s" % path)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise PrecheckError("无法读取声明清单 %s：%s" % (path, exc))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PrecheckError("声明清单 %s 不是 UTF-8 文本：%s" % (path, exc))
    patterns = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        norm = normalize_declared(stripped)
        if not norm:
            continue
        patterns.append((stripped, norm, declared_to_regex(stripped)))
    return patterns


def reconcile_declared(result, patterns):
    """对账：返回 {"undeclared": [...], "declared_unchanged": [...]}。"""
    changes = []
    for item in result["modified"]:
        paths = [item["path"]]
        if item.get("renamed_from"):
            paths.append(item["renamed_from"])
        changes.append(("renamed" if item.get("renamed_from") else "modified",
                        item["path"], paths))
    for item in result["added"]:
        changes.append(("added", item["path"], [item["path"]]))
    for item in result["deleted"]:
        changes.append(("deleted", item["path"], [item["path"]]))
    for item in result["untracked"]:
        changes.append(("untracked", item["path"], [item["path"]]))

    used = [False] * len(patterns)
    undeclared = []
    for category, shown, paths in changes:
        hit = False
        for idx, (_raw, _norm, regex) in enumerate(patterns):
            if any(regex.match(p) for p in paths):
                used[idx] = True
                hit = True
        if not hit:
            row = {"path": shown, "category": category}
            if category == "renamed":
                row["renamed_from"] = paths[1]
            undeclared.append(row)
    unchanged = [patterns[i][0] for i in range(len(patterns)) if not used[i]]
    return {"undeclared": undeclared, "declared_unchanged": unchanged}


# ---------------------------------------------------------------------------
# Java 类名（S3）
# ---------------------------------------------------------------------------

def java_class_name(path):
    """src/main/java/ 下的 .java 路径 → 全限定类名；不符合约定返回 None。

    允许前面有模块目录（module-a/src/main/java/...）；
    package-info.java 与 module-info.java 不是类，跳过。
    """
    if not path.endswith(".java"):
        return None
    if path.startswith(JAVA_ROOT):
        rest = path[len(JAVA_ROOT):]
    else:
        marker = "/" + JAVA_ROOT
        pos = path.find(marker)
        if pos < 0:
            return None
        rest = path[pos + len(marker):]
    stem = rest[:-len(".java")]
    if not stem or stem.rsplit("/", 1)[-1] in ("package-info", "module-info"):
        return None
    parts = stem.split("/")
    if any(not p for p in parts):
        return None
    return ".".join(parts)


def java_classes(result):
    """已修改（含重命名后路径）、已新增、未跟踪文件中的类名，去重排序。

    已删除文件不再有类可测，不输出。
    """
    names = set()
    for item in result["modified"] + result["added"] + result["untracked"]:
        name = java_class_name(item["path"])
        if name:
            names.add(name)
    return sorted(names)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def _eol_only_paths(repo, sha, numstat):
    """S2：用 --ignore-cr-at-eol 再跑一次 numstat，原本有增删、忽略行尾
    CR 后增删为 0（或不再出现）的文件即「只差行尾」。

    git 版本不支持该选项时返回 None（不标注，不报错）。
    """
    rc, out, _stderr = _git(
        repo, ["diff", "--numstat", "-z", "--no-ext-diff", "--no-textconv",
               "--ignore-cr-at-eol", sha, "--"])
    if rc != 0:
        return None
    ignored = parse_numstat_z(out)
    eol_only = set()
    for path, (ins, dele) in numstat.items():
        if ins is None or (ins + dele) == 0:
            continue
        again = ignored.get(path, (0, 0))
        if again == (0, 0):
            eol_only.add(path)
    return eol_only


def repo_diffstat(repo, base, includes, excludes):
    base = validate_base(base)
    if not os.path.isdir(repo):
        raise PrecheckError("目录不存在：%s" % repo)
    rc, status_out, stderr = _git(
        repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if rc != 0:
        raise PrecheckError("不是 git 工作区（或 git 报错）：%s %s"
                            % (repo, ("：" + stderr) if stderr else ""))
    sha = resolve_base(repo, base)
    rc, numstat_out, stderr = _git(
        repo, ["diff", "--numstat", "-z", "--no-ext-diff", "--no-textconv",
               sha, "--"])
    if rc != 0:
        raise PrecheckError("基线 %s 在仓库 %s 中不可用%s"
                            % (base, repo, ("：" + stderr) if stderr else ""))
    numstat = parse_numstat_z(numstat_out)
    eol_only = _eol_only_paths(repo, sha, numstat)

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
            if eol_only is not None:
                item["eol_only"] = path in eol_only
            modified.append(item)

    tracked_ins = sum(i["insertions"] or 0 for i in
                      modified + added + deleted)
    tracked_del = sum(i["deletions"] or 0 for i in
                      modified + added + deleted)
    untracked_lines = sum(u["lines"] or 0 for u in untracked)
    return {
        "repo": repo, "base": base, "base_commit": sha,
        "modified": modified, "added": added, "deleted": deleted,
        "untracked": untracked,
        "tracked_insertions": tracked_ins,
        "tracked_deletions": tracked_del,
        "untracked_lines": untracked_lines,
        "eol_check": eol_only is not None,
    }


def _fmt_tracked(item):
    text = item["path"]
    if item.get("renamed_from"):
        text = "%s（原 %s）" % (item["path"], item["renamed_from"])
    if item["insertions"] is None:
        return text + "（二进制）"
    text = "%s（+%d -%d）" % (text, item["insertions"] or 0,
                             item["deletions"] or 0)
    if item.get("eol_only"):
        text += "（只差行尾）"
    return text


_CATEGORY_TEXT = {"modified": "已修改", "renamed": "重命名", "added": "已新增",
                  "deleted": "已删除", "untracked": "未跟踪"}


def run_diffstat(repos, base, includes, excludes, as_json, out, err,
                 declared=None, show_java_classes=False):
    # 参数级错误（注入的 --base、声明清单不存在）在任何 git 调用之前报出
    validate_base(base)
    patterns = load_declared(declared) if declared else None

    results = []
    errors = []
    for repo in repos:
        try:
            r = repo_diffstat(repo, base, includes, excludes)
        except PrecheckError as exc:
            errors.append(str(exc))
            continue
        if patterns is not None:
            r["declared"] = dict(file=declared,
                                 **reconcile_declared(r, patterns))
        if show_java_classes:
            r["java_classes"] = java_classes(r)
        results.append(r)

    all_classes = sorted(set(c for r in results
                             for c in r.get("java_classes", [])))
    has_undeclared = any(r.get("declared", {}).get("undeclared")
                         for r in results)

    if as_json:
        payload = {"repos": results, "errors": errors}
        if patterns is not None:
            payload["undeclared_total"] = sum(
                len(r["declared"]["undeclared"]) for r in results)
        if show_java_classes:
            payload["java_classes"] = all_classes
            payload["java_classes_line"] = ",".join(all_classes)
        out(json.dumps(payload, ensure_ascii=False))
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
            if not r["eol_check"]:
                out("  提示：本机 git 不支持 --ignore-cr-at-eol，"
                    "未做「只差行尾」标注")
            if "declared" in r:
                d = r["declared"]
                out("  声明对账（%s）：" % d["file"])
                out("    未声明改动 %d：" % len(d["undeclared"]))
                for row in d["undeclared"]:
                    text = "      %s %s" % (_CATEGORY_TEXT[row["category"]],
                                            row["path"])
                    if row.get("renamed_from"):
                        text += "（原 %s）" % row["renamed_from"]
                    out(text)
                out("    声明未改动 %d（仅提示）：" %
                    len(d["declared_unchanged"]))
                for pat in d["declared_unchanged"]:
                    out("      %s" % pat)
        if show_java_classes:
            out("Java 类（可直接填 -DtargetClasses）：")
            out(",".join(all_classes) if all_classes else "（没有）")
        for message in errors:
            err("错误：%s" % message)
    if errors:
        return 2
    if has_undeclared:
        return 1
    return 0
