"""编号配置加载。

配置文件是一个 JSON 对象，例如::

    {
      "categories": [
        {"id": "R", "name": "业务规则",
         "pattern": "R\\d+(\\.\\d+)?"},
        {"id": "AC", "name": "验收标准", "pattern": "AC-\\d+"}
      ],
      "coverage": [
        {"from": "R", "to": "AC", "label": "没有被任何 AC 覆盖的规则"}
      ],
      "short_name_length": 24,
      "allow_duplicate_prefixes": ["EXT-"]
    }

类别条目可带 ``short_name_column``：表格定义取短名时用哪一列，
字符串表示表头列名，整数表示列序号（0 起，编号列为 0）；不写时
默认取编号列之后第一个「文本列」（去掉全部编号与分隔符后仍有
内容的列）。``allow_duplicate_prefixes``：同号异义检查的白名单，
编号以任一前缀开头时，多处内容不同的定义不算异常。

``pattern`` 是不带捕获组的正则（会被自动加上词边界保护）。
``coverage`` 的语义见 README：当某一行同时出现源编号与目标类别
编号时，视作一次追溯链接；源编号若从未出现在任何这样的行里，
即为覆盖缺口。
"""

import json
import re
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.json")


class Category(object):
    def __init__(self, data):
        self.id = data["id"]
        self.name = data["name"]
        self.pattern = data["pattern"]
        # 表格定义取短名的列：列名（str）或列序号（int），None 为默认规则。
        self.short_name_column = data.get("short_name_column")
        self.regex = re.compile(r"(?<![A-Za-z0-9_.-])(" + data["pattern"] + r")(?![A-Za-z0-9_.-])")

    def find(self, text):
        return [m.group(1) for m in self.regex.finditer(text)]


class Config(object):
    def __init__(self, data):
        self.categories = [Category(c) for c in data.get("categories", [])]
        self.by_id = {}
        for cat in self.categories:
            self.by_id[cat.id] = cat
        self.coverage = list(data.get("coverage", []))
        self.short_name_length = int(data.get("short_name_length", 24))
        self.allow_duplicate_prefixes = [
            str(p) for p in data.get("allow_duplicate_prefixes", [])
        ]

    def duplicate_allowed(self, number):
        """编号的同号异义是否在白名单前缀内。"""
        return any(number.startswith(p) for p in self.allow_duplicate_prefixes)

    def category_of(self, number):
        """返回编号所属类别，未匹配返回 None。"""
        for cat in self.categories:
            if cat.regex.fullmatch(number):
                return cat
        return None

    def find_all(self, text):
        """返回 (编号, 类别) 列表，按出现顺序，同一位置只取最长匹配。"""
        found = []
        for cat in self.categories:
            for m in cat.regex.finditer(text):
                found.append((m.start(), m.end(), m.group(1), cat))
        found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        result = []
        last_end = -1
        for start, end, number, cat in found:
            if start < last_end:
                continue
            result.append((number, cat))
            last_end = end
        return result


def load_config(path=None):
    """加载配置。path 为 None 时用包内默认配置。"""
    if path is None:
        text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8-sig")
    else:
        text = Path(path).read_text(encoding="utf-8-sig")
    return Config(json.loads(text))
