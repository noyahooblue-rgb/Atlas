import unittest
from pathlib import Path

from specpeek.config import load_config
from specpeek.htmlgen import generate
from specpeek.index import check, scan

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_definitions_and_references(self):
        definitions, references = scan(EXAMPLES, self.config)
        self.assertIn("R1.1", definitions)
        self.assertIn("AC-01", definitions)
        self.assertIn("T1", definitions)
        self.assertEqual(definitions["R1.1"].path, "requirement.md")
        self.assertTrue(any(r.number == "R1.1" for r in references))

    def test_three_check_kinds(self):
        result = check(EXAMPLES, self.config)
        # ① 被引用但找不到定义
        self.assertIn("R9.9", result["undefined"])
        # ② 定义了但从未被引用（按类别）
        self.assertIn("T4", result["unused"].get("T", []))
        # ③ 覆盖缺口：R1.4 被提及但从未与 AC 同行出现
        gaps = []
        for values in result["coverage_gaps"].values():
            gaps.extend(values)
        self.assertIn("R1.4", gaps)


class TestHtml(unittest.TestCase):
    def test_html_generation(self):
        import tempfile

        config = load_config()
        result = check(EXAMPLES, config)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "review.html"
            out.write_text(generate(EXAMPLES, config, result), encoding="utf-8")
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            # 悬停数据：定义原文与位置出现在 title 里
            self.assertIn("def-R1-1", text)
            self.assertIn("requirement.md", text)
            # 不含外部链接
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)
            self.assertNotIn("<script src", text)
            self.assertNotIn("<link", text)


if __name__ == "__main__":
    unittest.main()
