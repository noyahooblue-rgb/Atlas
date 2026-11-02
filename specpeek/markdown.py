"""最小 Markdown 渲染：标题、表格、列表、代码块、段落。

只覆盖审查页需要的子集，不追求完整 CommonMark。编号链接化
在 htmlgen 里完成：这里先做转义与行内格式，编号替换由
htmlgen 在转义后的文本上进行，避免破坏 HTML 标签。
"""

import html
import re


def escape(text):
    return html.escape(text, quote=False)


def inline(text):
    """行内格式：行内代码、加粗。返回转义后的 HTML。"""
    parts = text.split("`")
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append("<code>%s</code>" % escape(part))
        else:
            part = escape(part)
            part = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", part)
            out.append(part)
    return "".join(out)


def render(lines, link_ids):
    """渲染一组行。link_ids(text) 返回已做编号链接的 HTML。"""
    out = []
    i = 0
    in_fence = False
    fence_buf = []
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            if not in_fence:
                in_fence = True
                fence_buf = []
            else:
                in_fence = False
                out.append("<pre><code>%s</code></pre>" % escape("\n".join(fence_buf)))
            i += 1
            continue
        if in_fence:
            fence_buf.append(line)
            i += 1
            continue
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            out.append("<h%d>%s</h%d>" % (min(level, 6), link_ids(inline(stripped.lstrip("#").strip())), min(level, 6)))
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            header = [c.strip() for c in stripped.strip("|").split("|")]
            out.append("<table><thead><tr>%s</tr></thead><tbody>" % "".join(
                "<th>%s</th>" % link_ids(inline(c)) for c in header))
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip("|").split("|")]
                out.append("<tr>%s</tr>" % "".join(
                    "<td>%s</td>" % link_ids(inline(c)) for c in cells))
                i += 1
            out.append("</tbody></table>")
            continue
        if re.match(r"^[-*+]\s+", stripped) or re.match(r"^\d+[.)]\s+", stripped):
            items = []
            while i < len(lines) and (re.match(r"^[-*+]\s+", lines[i].strip()) or re.match(r"^\d+[.)]\s+", lines[i].strip())):
                items.append(re.sub(r"^[-*+]\s+|^\d+[.)]\s+", "", lines[i].strip()))
                i += 1
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % link_ids(inline(t)) for t in items))
            continue
        out.append("<p>%s</p>" % link_ids(inline(stripped)))
        i += 1
    return "\n".join(out)
