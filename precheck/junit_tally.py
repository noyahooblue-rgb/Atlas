"""junit-tally：JUnit XML 报告用例对账。

输入一个或多个报告文件或目录（目录递归找 *.xml），支持 testsuite
与 testsuites 两种根元素（含嵌套）。按文件、按测试类汇总
tests / failures / errors / skipped 并给出总计。

规则：
- 同一报告内容被重复传入（同文件两次、复制件、目录与文件重叠）
  按内容 sha256 去重并提示；
- 同名测试类出现在多个报告文件时分别计数并提示；
- 空目录或合计零用例：明确报「零用例」，退出码 1，绝不当通过；
- XML 损坏或根元素不是 JUnit：报错退出 2，不打印堆栈；
- --expect-* 与声称值比对，不一致退出 1 并指出差多少。
"""

import hashlib
import json
import os
import xml.etree.ElementTree as ET

from .errors import PrecheckError

COUNT_KEYS = ("tests", "failures", "errors", "skipped")


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_report(path):
    """解析一个报告文件，返回 [(类名, {计数...}), ...]（按 testsuite）。"""
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise PrecheckError("XML 解析失败：%s（%s）" % (path, exc))
    except OSError as exc:
        raise PrecheckError("无法读取报告 %s：%s" % (path, exc))
    root = tree.getroot()
    tag = root.tag.rsplit("}", 1)[-1]  # 容忍命名空间前缀
    if tag == "testsuite":
        suite_elems = [root]
    elif tag == "testsuites":
        suite_elems = [e for e in root.iter()
                       if e.tag.rsplit("}", 1)[-1] == "testsuite"]
    else:
        raise PrecheckError(
            "不是 JUnit 报告（根元素是 <%s>）：%s" % (tag, path))
    suites = []
    for elem in suite_elems:
        cases = [c for c in elem
                 if c.tag.rsplit("}", 1)[-1] == "testcase"]
        if cases:
            counts = {"tests": len(cases), "failures": 0,
                      "errors": 0, "skipped": 0}
            for case in cases:
                child_tags = [ch.tag.rsplit("}", 1)[-1] for ch in case]
                if "failure" in child_tags:
                    counts["failures"] += 1
                if "error" in child_tags:
                    counts["errors"] += 1
                if "skipped" in child_tags:
                    counts["skipped"] += 1
        else:
            # 没有 testcase 子元素的报告退回用 testsuite 属性计数
            counts = {k: _safe_int(elem.get(k)) for k in COUNT_KEYS}
        name = elem.get("name")
        if not name and cases:
            name = cases[0].get("classname")
        suites.append((name or "(未命名)", counts))
    return suites


def discover(paths):
    """把输入展开成报告文件列表；路径不存在直接报错退出 2。"""
    files = []
    for spec in paths:
        if os.path.isfile(spec):
            files.append(spec)
        elif os.path.isdir(spec):
            for dirpath, dirnames, filenames in os.walk(spec):
                dirnames.sort()
                for name in sorted(filenames):
                    if name.lower().endswith(".xml"):
                        files.append(os.path.join(dirpath, name))
        else:
            raise PrecheckError("路径不存在或不是文件/目录：%s" % spec)
    return files


def tally(paths):
    """汇总，返回 dict：files / classes / totals / duplicates / warnings。"""
    files = discover(paths)
    seen_hashes = {}
    duplicates = []
    per_file = []
    class_rows = {}  # (类名, 文件) -> counts
    for path in files:
        try:
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError as exc:
            raise PrecheckError("无法读取报告 %s：%s" % (path, exc))
        if digest in seen_hashes:
            duplicates.append({"file": path, "same_as": seen_hashes[digest]})
            continue
        seen_hashes[digest] = path
        suites = parse_report(path)
        file_counts = {k: 0 for k in COUNT_KEYS}
        for class_name, counts in suites:
            for k in COUNT_KEYS:
                file_counts[k] += counts[k]
            key = (class_name, path)
            row = class_rows.setdefault(key, {k: 0 for k in COUNT_KEYS})
            for k in COUNT_KEYS:
                row[k] += counts[k]
        per_file.append({"file": path, "counts": file_counts})

    totals = {k: 0 for k in COUNT_KEYS}
    for item in per_file:
        for k in COUNT_KEYS:
            totals[k] += item["counts"][k]

    classes = [{"class": name, "file": path, "counts": counts}
               for (name, path), counts in sorted(class_rows.items())]

    warnings = []
    files_by_class = {}
    for name, path in class_rows:
        files_by_class.setdefault(name, set()).add(path)
    for name, where in sorted(files_by_class.items()):
        if len(where) > 1:
            warnings.append(
                "同名测试类「%s」出现在 %d 个报告文件中，已分别计数"
                % (name, len(where)))
    for dup in duplicates:
        warnings.append("重复报告已去重：%s 与 %s 内容相同" % (
            dup["file"], dup["same_as"]))

    return {"files": per_file, "classes": classes, "totals": totals,
            "duplicates": duplicates, "warnings": warnings,
            "files_found": len(files)}


def run_tally(paths, expects, as_json, out):
    """expects: {tests: N 或 None, failures: ..., errors:..., skipped:...}"""
    result = tally(paths)
    totals = result["totals"]

    mismatches = []
    for key in COUNT_KEYS:
        expect = expects.get(key)
        if expect is not None and expect != totals[key]:
            mismatches.append({"metric": key, "expected": expect,
                               "actual": totals[key],
                               "diff": totals[key] - expect})
    zero_cases = totals["tests"] == 0

    if as_json:
        out(json.dumps({
            "totals": totals,
            "files": [{"file": f["file"], **f["counts"]}
                      for f in result["files"]],
            "classes": [{"class": c["class"], "file": c["file"],
                         **c["counts"]} for c in result["classes"]],
            "duplicates": result["duplicates"],
            "warnings": result["warnings"],
            "expect_mismatches": mismatches,
            "zero_cases": zero_cases,
        }, ensure_ascii=False))
    else:
        out("按文件汇总：")
        if result["files"]:
            for f in result["files"]:
                c = f["counts"]
                out("  %s：tests=%d failures=%d errors=%d skipped=%d"
                    % (f["file"], c["tests"], c["failures"],
                       c["errors"], c["skipped"]))
        else:
            out("  （没有找到报告文件）")
        out("按测试类汇总：")
        for c in result["classes"]:
            cc = c["counts"]
            out("  %s（%s）：tests=%d failures=%d errors=%d skipped=%d"
                % (c["class"], c["file"], cc["tests"], cc["failures"],
                   cc["errors"], cc["skipped"]))
        t = totals
        out("总计：tests=%d failures=%d errors=%d skipped=%d"
            % (t["tests"], t["failures"], t["errors"], t["skipped"]))
        for warning in result["warnings"]:
            out("提示：%s" % warning)
        for m in mismatches:
            out("对不上：%s 声称 %d，实际 %d，差 %+d"
                % (m["metric"], m["expected"], m["actual"], m["diff"]))
        if zero_cases:
            out("零用例：未统计到任何测试用例，不能当作通过")

    if zero_cases or mismatches:
        return 1
    return 0
