"""扫描 .md 文件，建立编号定义与引用索引。

定义处识别（按优先级，分两遍）：
1. 标题行、列表项开头、行首加粗编号：``## R1.5 标题``、
   ``- AC-01 描述``、``**T3** 描述``；
2. 表格首列恰好等于编号、且编号后紧邻的单元格是文本列的行
   （编号后紧跟其他编号的行是追溯/引用行，不是定义）。

第一遍已定义的编号，第二遍表格不再覆盖；第二遍内部，
追溯总表一类「编号后跟编号」的行不算定义，这样正式定义
在表格里的编号也不会被追溯总表抢走。

同一编号若在多处各有一处定义且内容互不相同，记为
「同号异义」异常（配置白名单前缀除外）。scan 只保留第一处
作为正式定义，其余在 duplicates 中返回。

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


def _is_text_cell(config, cell):
    """单元格是否为「文本列」：去掉全部编号与分隔符后仍有内容。

    ``R1.1、R1.2`` 这类只有编号罗列的单元格不是文本列；
    ``遵循外部规则 BDR-EXT1。`` 这类正文里嵌编号的仍是文本列。
    """
    s = cell.strip()
    if not s:
        return False
    for cat in config.categories:
        s = cat.regex.sub(" ", s)
    s = re.sub(r"[*_`#>\[\]()|]", " ", s)
    s = re.sub(r"[\s、，,；;：:·/\\-—]+", "", s)
    return bool(s)


def _short_name(config, number, category, definition_line, cells, header=None):
    limit = config.short_name_length
    if cells:
        # 配置按类别指定了列：列名查表头、整数即列序号；指定列
        # 缺失或为空时回退默认规则。
        spec = category.short_name_column if category is not None else None
        if isinstance(spec, str) and header and spec in header:
            cell = cells[header.index(spec)].strip() if header.index(spec) < len(cells) else ""
            if cell:
                return _strip_markdown(cell)[:limit]
        elif isinstance(spec, int) and not isinstance(spec, bool):
            if 0 <= spec < len(cells) and cells[spec].strip():
                return _strip_markdown(cells[spec].strip())[:limit]
        # 默认：编号列之后第一个文本列。
        for cell in cells[1:]:
            if _is_text_cell(config, cell):
                return _strip_markdown(cell.strip())[:limit]
        return ""
    # 非表格：先剥标题 / 列表标记与加粗记号，再摘掉编号本身。
    s = definition_line.strip()
    s = re.sub(r"^#{1,6}\s*", "", s)
    s = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", s)
    s = s.lstrip("*").lstrip()
    if number and s.startswith(number):
        s = s[len(number):].lstrip("*").strip()
        s = s.lstrip(" :：、-").strip()
    else:
        s = _strip_markdown(s)
        for found, _cat in config.find_all(s):
            if s.startswith(found):
                s = s[len(found):].strip(" :：、-")
                break
    return _strip_markdown(s)[:limit]


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


def _normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def _scan_full(directory, config):
    """扫描并收集全部定义候选，返回 (definitions, references, duplicates)。

    definitions 只保留每个编号的第一处定义；duplicates 是同号
    异义清单：同一编号有多处定义且归一化后的内容不全相同
    （白名单前缀除外），每项为 {"number", "definitions"}。
    """
    files = sorted(Path(directory).rglob("*.md"))
    definitions = {}
    all_definitions = {}  # 编号 -> [Definition]（按出现顺序）
    definition_keys = set()  # (文件名, 行号, 编号)，仅正式定义处
    raw_lines = {}  # Path -> [(lineno, line)]
    for path in files:
        raw_lines[path] = list(_iter_lines(path))

    def record(number, cat, path, lineno, text, short_name):
        d = Definition(number, cat, path.name, lineno, text, short_name)
        all_definitions.setdefault(number, []).append(d)
        if number not in definitions:
            definitions[number] = d
            definition_keys.add((path.name, lineno, number))

    # 第一遍：标题 / 列表 / 行首定义（表格除外）。
    for path, lines in raw_lines.items():
        for lineno, line in lines:
            if line.strip().startswith("|"):
                continue
            cand = _definition_candidate(config, line)
            if cand is None:
                continue
            number, cat, _cells = cand
            record(number, cat, path, lineno, line.strip(),
                   _short_name(config, number, cat, line, None))
    # 第二遍：表格定义。逐行追踪当前表头；定义行要求编号后
    # 紧邻的单元格是文本列，否则视为追溯/引用行。
    for path, lines in raw_lines.items():
        header = None
        for idx, (lineno, line) in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("|"):
                header = None
                continue
            if TABLE_SEPARATOR.match(line):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # 下一行是分隔行时，本行是表头。
            if idx + 1 < len(lines) and TABLE_SEPARATOR.match(lines[idx + 1][1]):
                header = cells
                continue
            cand = _definition_candidate(config, line)
            if cand is None:
                continue
            number, cat, cells = cand
            if len(cells) < 2 or not _is_text_cell(config, cells[1]):
                continue
            record(number, cat, path, lineno, line.strip(),
                   _short_name(config, number, cat, line, cells, header))

    duplicates = []
    for number in sorted(all_definitions):
        defs = all_definitions[number]
        if len(defs) < 2 or config.duplicate_allowed(number):
            continue
        if len({_normalize(d.text) for d in defs}) > 1:
            duplicates.append({"number": number, "definitions": defs})

    references = []
    for path, lines in raw_lines.items():
        for lineno, line in lines:
            for number, cat in config.find_all(line):
                if (path.name, lineno, number) in definition_keys:
                    continue
                references.append(Reference(number, cat, path.name, lineno, line.strip()))
    return definitions, references, duplicates


def scan(directory, config):
    """扫描目录下所有 .md 文件，返回 (definitions, references)。

    definitions: {编号: Definition}；references: [Reference]。
    """
    definitions, references, _duplicates = _scan_full(directory, config)
    return definitions, references


def check(directory, config):
    """追溯检查（三类）加同号异义检查，返回 dict。"""
    definitions, references, duplicates = _scan_full(directory, config)
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
        "duplicates": duplicates,
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
    if result.get("duplicates"):
        lines.append("同号异义（同一编号多处定义且内容不同）：")
        for item in result["duplicates"]:
            places = " 与 ".join(d.location() for d in item["definitions"])
            lines.append("  %s：%s" % (item["number"], places))
    if not lines:
        lines.append("追溯检查通过：没有发现异常。")
    return "\n".join(lines)
