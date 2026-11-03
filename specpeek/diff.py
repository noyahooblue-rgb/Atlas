"""差异审查：按编号对比两个版本的规格目录。

``compare(old_dir, new_dir, config)`` 分别扫描两个目录，按编号
归类为新增、删除、内容改变与内容未变；内容改变的给出两处
定义原文，供「AI 改了一轮规格后，人只看变化」的审查场景。
比较的是每处编号的正式定义（扫描的第一处定义），内容按
空白归一化后比较。生成的 HTML 同样是单文件、无外部资源。
"""

import html as html_mod
import re

from .index import scan


def _normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def compare(old_dir, new_dir, config):
    """返回 {"added", "removed", "changed", "unchanged"}。

    added / removed：[Definition]（分别取新 / 旧目录的定义）；
    changed：[(旧 Definition, 新 Definition)]；unchanged：[编号]。
    """
    old_defs, _ = scan(old_dir, config)
    new_defs, _ = scan(new_dir, config)
    added, removed, changed, unchanged = [], [], [], []
    for number in sorted(new_defs):
        if number not in old_defs:
            added.append(new_defs[number])
        elif _normalize(old_defs[number].text) != _normalize(new_defs[number].text):
            changed.append((old_defs[number], new_defs[number]))
        else:
            unchanged.append(number)
    for number in sorted(old_defs):
        if number not in new_defs:
            removed.append(old_defs[number])
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
    }


def format_diff(result):
    """diff 命令的命令行文本输出。"""
    lines = []
    if result["added"]:
        lines.append("新增定义：")
        for d in result["added"]:
            lines.append("  %s（%s）%s" % (d.number, d.location(), d.short_name))
    if result["removed"]:
        lines.append("删除定义：")
        for d in result["removed"]:
            lines.append("  %s（%s）%s" % (d.number, d.location(), d.short_name))
    if result["changed"]:
        lines.append("内容改变：")
        for old, new in result["changed"]:
            lines.append("  %s：" % old.number)
            lines.append("    旧（%s）：%s" % (old.location(), old.text))
            lines.append("    新（%s）：%s" % (new.location(), new.text))
    lines.append("内容未变的编号数：%d" % len(result["unchanged"]))
    if not (result["added"] or result["removed"] or result["changed"]):
        lines.insert(0, "两个版本没有定义层面的差异。")
    return "\n".join(lines)


def generate_diff(old_dir, new_dir, config, result):
    """生成差异审查页 HTML（单文件、无外部资源）。"""
    esc = html_mod.escape
    parts = [
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<title>规格差异审查</title>",
        "<style>"
        "body{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;line-height:1.7}"
        "table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:4px 8px;vertical-align:top}"
        ".old{background:#fdf0f0}.new{background:#effaef}"
        "</style></head><body>",
        "<h1>规格差异审查</h1>",
        "<p>旧版本：%s<br>新版本：%s</p>" % (esc(str(old_dir)), esc(str(new_dir))),
        "<p>新增 %d、删除 %d、内容改变 %d、内容未变 %d。</p>" % (
            len(result["added"]), len(result["removed"]),
            len(result["changed"]), len(result["unchanged"])),
    ]
    if result["added"]:
        parts.append("<h2>新增定义</h2><table><tr><th>编号</th><th>定义处</th><th>原文</th></tr>")
        for d in result["added"]:
            parts.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                         % (esc(d.number), esc(d.location()), esc(d.text)))
        parts.append("</table>")
    if result["removed"]:
        parts.append("<h2>删除定义</h2><table><tr><th>编号</th><th>定义处</th><th>原文</th></tr>")
        for d in result["removed"]:
            parts.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                         % (esc(d.number), esc(d.location()), esc(d.text)))
        parts.append("</table>")
    if result["changed"]:
        parts.append("<h2>内容改变</h2><table><tr><th>编号</th><th>旧文</th><th>新文</th></tr>")
        for old, new in result["changed"]:
            parts.append(
                "<tr><td>%s</td>"
                "<td class=\"old\">%s<br><small>%s</small></td>"
                "<td class=\"new\">%s<br><small>%s</small></td></tr>"
                % (esc(old.number), esc(old.text), esc(old.location()),
                   esc(new.text), esc(new.location())))
        parts.append("</table>")
    if result["unchanged"]:
        parts.append("<h2>内容未变（%d）</h2><p>%s</p>"
                     % (len(result["unchanged"]),
                        esc("、".join(result["unchanged"]))))
    parts.append("</body></html>")
    return "\n".join(parts)
