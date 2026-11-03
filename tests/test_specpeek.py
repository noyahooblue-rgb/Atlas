import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from specpeek.__main__ import main as cli_main
from specpeek.config import load_config
from specpeek.diff import compare, format_diff, generate_diff
from specpeek.htmlgen import generate
from specpeek.index import check, scan

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
DEFAULT_CONFIG = ROOT / "specpeek" / "default_config.json"


def write_files(base, files):
    """files: {文件名: str 或 bytes}，写入临时目录并返回 Path。"""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = base / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return base


def load_custom_config(base, mutate):
    data = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    mutate(data)
    path = Path(base) / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return load_config(str(path))


class TestShortName(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.definitions, _ = scan(EXAMPLES, self.config)

    def test_short_name_list_definition(self):
        # 列表项定义：短名不带列表符号，也不带编号本身。
        oi = self.definitions["OI-1"].short_name
        self.assertTrue(oi.startswith("逾期提醒"))
        self.assertNotIn("OI-1", oi)
        self.assertFalse(oi.startswith("-"))
        t1 = self.definitions["T1"].short_name
        self.assertEqual(t1, "实现借书数量上限校验。")

    def test_short_name_table_definition(self):
        # 表格定义：短名取描述列；定义不被设计文档的追溯总表抢走。
        ac = self.definitions["AC-01"]
        self.assertEqual(ac.path, "requirement.md")
        self.assertTrue(ac.short_name.startswith("读者已借满"))
        self.assertNotIn("R1.1", ac.short_name)

    def test_short_name_heading_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_files(tmp, {"a.md": "# 规格\n\n## R1.5 退款时限\n\n正文引用 R1.5。\n"})
            definitions, _ = scan(tmp, self.config)
            self.assertEqual(definitions["R1.5"].short_name, "退款时限")

    def test_short_name_config_column_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            def mutate(data):
                for cat in data["categories"]:
                    if cat["id"] == "R":
                        cat["short_name_column"] = "备注"
            config = load_custom_config(tmp, mutate)
            definitions, _ = scan(EXAMPLES, config)
            self.assertEqual(definitions["R1.1"].short_name, "上限含续借")

    def test_short_name_config_column_by_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            def mutate(data):
                for cat in data["categories"]:
                    if cat["id"] == "R":
                        cat["short_name_column"] = 2
            config = load_custom_config(tmp, mutate)
            definitions, _ = scan(EXAMPLES, config)
            self.assertEqual(definitions["R1.1"].short_name, "上限含续借")


class TestChecks(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.result = check(EXAMPLES, self.config)

    def test_definitions_and_references(self):
        definitions, references = scan(EXAMPLES, self.config)
        self.assertIn("R1.1", definitions)
        self.assertIn("AC-01", definitions)
        self.assertIn("T1", definitions)
        self.assertEqual(definitions["R1.1"].path, "requirement.md")
        self.assertTrue(any(r.number == "R1.1" for r in references))

    def test_undefined_reference(self):
        self.assertIn("R9.9", self.result["undefined"])

    def test_unused_definition(self):
        self.assertIn("T4", self.result["unused"].get("T", []))

    def test_coverage_gap(self):
        gaps = []
        for values in self.result["coverage_gaps"].values():
            gaps.extend(values)
        self.assertIn("R1.4", gaps)

    def test_examples_have_no_duplicates(self):
        self.assertEqual(self.result["duplicates"], [])


class TestDuplicates(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_duplicate_conflict_two_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_files(tmp, {
                "a.md": "# 甲\n\n## R1.1 借阅上限为五本\n",
                "b.md": "# 乙\n\n## R1.1 借阅上限为八本\n",
            })
            result = check(tmp, self.config)
            self.assertEqual(len(result["duplicates"]), 1)
            item = result["duplicates"][0]
            self.assertEqual(item["number"], "R1.1")
            places = {d.path for d in item["definitions"]}
            self.assertEqual(places, {"a.md", "b.md"})

    def test_duplicate_same_text_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_files(tmp, {
                "a.md": "# 甲\n\n## R1.1 借阅上限为五本\n",
                "b.md": "# 乙\n\n## R1.1 借阅上限为五本\n",
            })
            result = check(tmp, self.config)
            self.assertEqual(result["duplicates"], [])

    def test_duplicate_two_sections_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_files(tmp, {
                "a.md": "# 甲\n\n- T9 负责甲事项。\n\n## 乙\n\n- T9 负责乙事项。\n",
            })
            result = check(tmp, self.config)
            numbers = [d["number"] for d in result["duplicates"]]
            self.assertIn("T9", numbers)

    def test_duplicate_allowed_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            def mutate(data):
                data["allow_duplicate_prefixes"] = ["R"]
            config = load_custom_config(tmp, mutate)
            write_files(tmp, {
                "a.md": "# 甲\n\n## R1.1 借阅上限为五本\n",
                "b.md": "# 乙\n\n## R1.1 借阅上限为八本\n",
            })
            result = check(tmp, config)
            self.assertEqual(result["duplicates"], [])


class TestInputEncodings(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_utf8_bom_input(self):
        body = "# 规格\n\n- R1.1 带 BOM 的定义。\n\n正文引用 R1.1。\n"
        with tempfile.TemporaryDirectory() as tmp:
            write_files(tmp, {"a.md": b"\xef\xbb\xbf" + body.encode("utf-8")})
            definitions, _ = scan(tmp, self.config)
            self.assertIn("R1.1", definitions)
            self.assertEqual(definitions["R1.1"].short_name, "带 BOM 的定义。")

    def test_crlf_input(self):
        body = "# 规格\r\n\r\n- R1.1 换行符定义。\r\n\r\n正文引用 R1.1。\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            write_files(tmp, {"a.md": body.encode("utf-8")})
            definitions, _ = scan(tmp, self.config)
            self.assertIn("R1.1", definitions)
            self.assertEqual(definitions["R1.1"].short_name, "换行符定义。")
            self.assertNotIn("\r", definitions["R1.1"].text)


class TestHtml(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        result = check(EXAMPLES, self.config)
        self.text = generate(EXAMPLES, self.config, result)

    def test_html_generation(self):
        self.assertIn("def-R1-1", self.text)
        self.assertIn("requirement.md", self.text)

    def test_html_no_external_resources(self):
        self.assertNotIn("http://", self.text)
        self.assertNotIn("https://", self.text)
        self.assertNotIn("<script src", self.text)
        self.assertNotIn("<link", self.text)

    def test_html_localstorage_in_try_catch(self):
        self.assertRegex(self.text, r"try\s*\{[^}]*localStorage\.getItem")
        self.assertRegex(self.text, r"try\s*\{[^}]*localStorage\.setItem")

    def test_html_export_feature(self):
        self.assertIn('id="export-btn"', self.text)
        self.assertIn("导出审查意见", self.text)
        self.assertIn("new Blob", self.text)
        self.assertIn("review-comments.md", self.text)


OLD_SPEC = {
    "rules.md": "# 旧版\n\n| 编号 | 内容 |\n| --- | --- |\n"
                "| R1.1 | 甲规则。 |\n| R1.2 | 乙规则。 |\n| R1.3 | 丙规则。 |\n",
}
NEW_SPEC = {
    "rules.md": "# 新版\n\n| 编号 | 内容 |\n| --- | --- |\n"
                "| R1.1 | 甲规则。 |\n| R1.2 | 乙规则改版。 |\n| R1.4 | 丁规则。 |\n",
}


class TestDiff(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.old = write_files(base / "old", OLD_SPEC)
        self.new = write_files(base / "new", NEW_SPEC)
        self.result = compare(self.old, self.new, self.config)

    def tearDown(self):
        self._tmp.cleanup()

    def test_diff_groups(self):
        self.assertEqual([d.number for d in self.result["added"]], ["R1.4"])
        self.assertEqual([d.number for d in self.result["removed"]], ["R1.3"])
        self.assertEqual([o.number for o, _n in self.result["changed"]], ["R1.2"])
        self.assertEqual(self.result["unchanged"], ["R1.1"])

    def test_diff_format_shows_old_and_new(self):
        text = format_diff(self.result)
        self.assertIn("乙规则。", text)
        self.assertIn("乙规则改版。", text)
        self.assertIn("内容未变的编号数：1", text)

    def test_diff_html_no_external_resources(self):
        text = generate_diff(self.old, self.new, self.config, self.result)
        self.assertIn("新增定义", text)
        self.assertIn("内容改变", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("<script src", text)
        self.assertNotIn("<link", text)


class TestCli(unittest.TestCase):
    def test_check_exit_code_nonzero_on_examples(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["check", str(EXAMPLES)]), 1)

    def test_diff_exit_code_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old = write_files(base / "old", OLD_SPEC)
            new = write_files(base / "new", NEW_SPEC)
            out = base / "diff.html"
            with redirect_stdout(io.StringIO()):
                code = cli_main(["diff", str(old), str(new), "-o", str(out)])
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
