"""扫描 .md 文件，建立编号定义与引用索引。

定义处识别（按优先级，分两遍）：
1. 标题行、列表项开头、行首加粗编号：``## R1.5 标题``、
   ``- AC-01 描述``、``**T3** 描述``；
2. 表格首列恰好等于编号的行。

第一遍已定义的编号，第二遍表格不再覆盖，这样追溯总表
（首列也是编号）不会抢走需求文档里的正式定义。

引用处：除定义编号在其定义行上的那一次出现外，文档中每一次
出现都算一次引用。围栏代码块内的内容不计入定义与引用。
只读输入文件，绝不修改。
"""

import re
from pathlib import Path

TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


class Definition(object):
    def __init__(self, number, category, path, lineno, text, short_name):
        self.number = number
        self.category = category
        self.path = path
        self.lineno = lineno
        self.text = text
        self.short_name = short_name

    def location(self):
        return "%s:%d" % (self.path, self.lineno)


class Reference(object):
    def __init__(self, number, category, path, lineno, text):
        self.number = number
        self.category = category
        self.path = path
        self.lineno = lineno
        self.text = text


def _strip_markdown(text):
    text = re.sub(r"[*_`#>\[\]()|]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _short_name(config, definition_line, cells):
    if cells:
        # 表格：短名取首列之后第一个非编号单元格。
        for cell in cells[1:]:
            cell = cell.strip()
            if cell and not config.category_of(cell):
                return _strip_markdown(cell)[: config.short_name_length]
        return ""
    text = _strip_markdown(definition_line)
    # 去掉行首的编号本身，剩下即标题或首句。
    for number, _cat in config.find_all(text):
        if text.startswith(number):
            text = text[len(number):].strip(" :：、-")
            break
    return text[: config.short_name_length]


def _definition_candidate(config, line):
    """返回 (编号, 类别, 表格单元格) 或 None。

    kind 由调用方区分：表格行返回 cells，标题/列表/行首返回 None。
    """
    stripped = line.strip()
    if stripped.startswith("|"):
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and config.category_of(cells[0]):
            return cells[0], config.category_of(cells[0]), cells
        return None
    # 标题行
    if stripped.startswith("#"):
        body = stripped.lstrip("#").strip()
        found = config.find_all(body)
        if found and body.startswith(found[0][0]):
            return found[0][0], found[0][1], None
    # 列表项开头
    m = re.match(r"^[-*+]\s+(.*)$", stripped) or re.match(r"^\d+[.)]\s+(.*)$", stripped)
    if m:
        found = config.find_all(m.group(1))
        if found and m.group(1).startswith(found[0][0]):
            return found[0][0], found[0][1], None
    # 行首（可加粗）编号，后跟冒号、空格或中文标点
    m = re.match(r"^(?:\*\*)?([A-Za-z]+[-\d][A-Za-z0-9.-]*)(?:\*\*)?[\s:：、]", stripped)
    if m and config.category_of(m.group(1)):
        return m.group(1), config.category_of(m.group(1)), None
    return None


def _iter_lines(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    in_fence = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        yield lineno, raw


def scan(directory, config):
    """扫描目录下所有 .md 文件，返回 (definitions, references)。

    definitions: {编号: Definition}；references: [Reference]。
    """
    files = sorted(Path(directory).rglob("*.md"))
    definitions = {}
    definition_keys = set()  # (文件名, 行号, 编号)
    raw_lines = {}  # Path -> [(lineno, line)]
    for path in files:
        raw_lines[path] = list(_iter_lines(path))

    # 第一遍：标题 / 列表 / 行首定义（表格除外）。
    for path, lines in raw_lines.items():
        for lineno, line in lines:
            if line.strip().startswith("|"):
                continue
            cand = _definition_candidate(config, line)
            if cand is None:
                continue
            number, cat, _cells = cand
            if number in definitions:
                continue
            definitions[number] = Definition(
                number, cat, path.name, lineno, line.strip(),
                _short_name(config, line, None),
            )
            definition_keys.add((path.name, lineno, number))
    # 第二遍：表格首列定义，只补未定义编号。
    for path, lines in raw_lines.items():
        for lineno, line in lines:
            if not line.strip().startswith("|"):
                continue
            if TABLE_SEPARATOR.match(line):
                continue
            cand = _definition_candidate(config, line)
            if cand is None:
                continue
            number, cat, cells = cand
            if number in definitions:
                continue
            definitions[number] = Definition(
                number, cat, path.name, lineno, line.strip(),
                _short_name(config, line, cells),
            )
            definition_keys.add((path.name, lineno, number))

    references = []
    for path, lines in raw_lines.items():
        for lineno, line in lines:
            for number, cat in config.find_all(line):
                if (path.name, lineno, number) in definition_keys:
                    continue
                references.append(Reference(number, cat, path.name, lineno, line.strip()))
    return definitions, references


def check(directory, config):
    """三类追溯检查，返回 dict。"""
    definitions, references = scan(directory, config)
    referenced = {}
    for ref in references:
        referenced.setdefault(ref.number, []).append(ref)

    undefined = sorted(
        (n for n in referenced if n not in definitions),
        key=lambda n: (config.category_of(n).id if config.category_of(n) else "", n),
    )
    unused = {}
    for number, definition in definitions.items():
        if number not in referenced:
            unused.setdefault(definition.category.id, []).append(number)
    for numbers in unused.values():
        numbers.sort()

    # 覆盖：某一行同时出现源编号与目标类别编号，视作追溯链接。
    files = sorted(Path(directory).rglob("*.md"))
    covered = set()
    for path in files:
        for _lineno, line in _iter_lines(path):
            found = config.find_all(line)
            cats = {cat.id for _n, cat in found}
            for rel in config.coverage:
                if rel["to"] in cats:
                    for number, cat in found:
                        if cat.id == rel["from"]:
                            covered.add(number)
    coverage_gaps = {}
    for rel in config.coverage:
        gaps = sorted(
            n for n, d in definitions.items()
            if d.category.id == rel["from"] and n not in covered
        )
        if gaps:
            coverage_gaps[rel["label"]] = gaps
    return {
        "definitions": definitions,
        "references": references,
        "undefined": undefined,
        "unused": unused,
        "coverage_gaps": coverage_gaps,
    }


def format_check(result, config):
    """check 命令的输出文本（同时用作 HTML 顶部的异常汇总）。"""
    lines = []
    if result["undefined"]:
        lines.append("被引用但找不到定义：")
        for number in result["undefined"]:
            cat = config.category_of(number)
            lines.append("  %s（%s）" % (number, cat.name if cat else "未知类别"))
    if result["unused"]:
        lines.append("定义了但从未被引用：")
        for cat_id in sorted(result["unused"]):
            cat = config.by_id.get(cat_id)
            name = cat.name if cat else cat_id
            lines.append("  %s：%s" % (name, "、".join(result["unused"][cat_id])))
    if result["coverage_gaps"]:
        lines.append("覆盖关系缺口：")
        for label, gaps in result["coverage_gaps"].items():
            lines.append("  %s：%s" % (label, "、".join(gaps)))
    if not lines:
        lines.append("追溯检查通过：没有发现异常。")
    return "\n".join(lines)
