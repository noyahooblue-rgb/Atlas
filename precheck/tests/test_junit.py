"""junit-tally 的测试。"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from precheck import cli, junit_tally

SUITE_BASIC = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.LoginTest" tests="4" failures="1" errors="1" skipped="1">
  <testcase classname="com.example.LoginTest" name="ok"/>
  <testcase classname="com.example.LoginTest" name="bad">
    <failure message="boom"/>
  </testcase>
  <testcase classname="com.example.LoginTest" name="err">
    <error message="bang"/>
  </testcase>
  <testcase classname="com.example.LoginTest" name="skip">
    <skipped/>
  </testcase>
</testsuite>
"""

SUITES_TWO = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="com.example.ATest">
    <testcase classname="com.example.ATest" name="a1"/>
    <testcase classname="com.example.ATest" name="a2"/>
  </testsuite>
  <testsuite name="com.example.BTest">
    <testcase classname="com.example.BTest" name="b1">
      <failure message="x"/>
    </testcase>
  </testsuite>
</testsuites>
"""

SUITES_NESTED = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="all">
  <testsuites name="module-x">
    <testsuite name="com.example.DeepTest">
      <testcase classname="com.example.DeepTest" name="d1"/>
      <testcase classname="com.example.DeepTest" name="d2">
        <skipped/>
      </testcase>
    </testsuite>
  </testsuites>
  <testsuite name="com.example.TopTest">
    <testcase classname="com.example.TopTest" name="t1"/>
  </testsuite>
</testsuites>
"""

SUITE_ATTRS_ONLY = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.SummaryOnly" tests="7" failures="2" errors="1" skipped="3"/>
"""

SUITE_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.Empty" tests="0" failures="0" errors="0" skipped="0"/>
"""


def no_expects():
    return {"tests": None, "failures": None, "errors": None,
            "skipped": None}


class TallyTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def tally_run(self, paths, expects=None, as_json=False):
        lines = []
        code = junit_tally.run_tally(paths, expects or no_expects(),
                                     as_json, lines.append)
        return code, "\n".join(lines)


class TallyTests(TallyTestBase):

    def test_testsuite_root_counts_from_testcases(self):
        path = self.write("TEST-login.xml", SUITE_BASIC)
        result = junit_tally.tally([path])
        self.assertEqual(result["totals"],
                         {"tests": 4, "failures": 1, "errors": 1,
                          "skipped": 1})
        self.assertEqual(result["classes"][0]["class"],
                         "com.example.LoginTest")

    def test_testsuites_root_multiple_suites(self):
        path = self.write("all.xml", SUITES_TWO)
        result = junit_tally.tally([path])
        self.assertEqual(result["totals"]["tests"], 3)
        self.assertEqual(result["totals"]["failures"], 1)
        self.assertEqual(len(result["classes"]), 2)

    def test_nested_testsuites(self):
        path = self.write("nested.xml", SUITES_NESTED)
        result = junit_tally.tally([path])
        self.assertEqual(result["totals"],
                         {"tests": 3, "failures": 0, "errors": 0,
                          "skipped": 1})

    def test_attributes_fallback_when_no_testcases(self):
        path = self.write("summary.xml", SUITE_ATTRS_ONLY)
        result = junit_tally.tally([path])
        self.assertEqual(result["totals"],
                         {"tests": 7, "failures": 2, "errors": 1,
                          "skipped": 3})

    def test_expect_match_passes(self):
        path = self.write("TEST-login.xml", SUITE_BASIC)
        expects = no_expects()
        expects["tests"] = 4
        expects["failures"] = 1
        code, _ = self.tally_run([path], expects)
        self.assertEqual(code, 0)

    def test_expect_mismatch_reports_difference(self):
        path = self.write("TEST-login.xml", SUITE_BASIC)
        expects = no_expects()
        expects["tests"] = 6
        code, text = self.tally_run([path], expects)
        self.assertEqual(code, 1)
        self.assertIn("声称 6", text)
        self.assertIn("实际 4", text)
        self.assertIn("差 -2", text)

    def test_same_class_in_two_modules_counted_separately(self):
        body = SUITE_BASIC
        p1 = self.write("module-a/TEST-login.xml", body.replace(
            'tests="4"', 'tests="4"'))
        p2 = self.write("module-b/TEST-login.xml", body)
        # 两份内容相同会被去重；改一处用例名让第二份内容不同
        with open(p2, encoding="utf-8") as fh:
            text2 = fh.read().replace('name="ok"', 'name="ok2"')
        with open(p2, "w", encoding="utf-8") as fh:
            fh.write(text2)
        result = junit_tally.tally([os.path.join(self.root, "module-a"),
                                    os.path.join(self.root, "module-b")])
        self.assertEqual(result["totals"]["tests"], 8)
        rows = [c for c in result["classes"]
                if c["class"] == "com.example.LoginTest"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("同名测试类" in w
                            for w in result["warnings"]))

    def test_duplicate_report_deduped_by_content(self):
        p1 = self.write("r1.xml", SUITE_BASIC)
        p2 = self.write("copy/r1-copy.xml", SUITE_BASIC)
        result = junit_tally.tally([p1, p2, p1])
        self.assertEqual(result["totals"]["tests"], 4)
        self.assertEqual(len(result["duplicates"]), 2)
        self.assertTrue(any("重复报告已去重" in w
                            for w in result["warnings"]))

    def test_empty_directory_is_zero_cases(self):
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        code, text = self.tally_run([empty])
        self.assertEqual(code, 1)
        self.assertIn("零用例", text)

    def test_zero_testcase_report_is_zero_cases(self):
        path = self.write("empty.xml", SUITE_EMPTY)
        code, text = self.tally_run([path])
        self.assertEqual(code, 1)
        self.assertIn("零用例", text)

    def test_directory_scan_recursive(self):
        self.write("reports/sub/TEST-login.xml", SUITE_BASIC)
        self.write("reports/readme.txt", "not xml")
        code, text = self.tally_run([os.path.join(self.root, "reports")])
        self.assertEqual(code, 0)
        self.assertIn("tests=4", text)

    def test_json_output(self):
        path = self.write("TEST-login.xml", SUITE_BASIC)
        code, text = self.tally_run([path], as_json=True)
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertEqual(payload["totals"]["tests"], 4)
        self.assertEqual(payload["classes"][0]["failures"], 1)
        self.assertFalse(payload["zero_cases"])


class TallyCliErrorTests(TallyTestBase):

    def run_cli(self, argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.main(argv)

    def test_malformed_xml_exit_2_no_traceback(self):
        bad = self.write("broken.xml", "<testsuite><testcase></testsuite")
        code = self.run_cli(["junit-tally", bad])
        self.assertEqual(code, 2)

    def test_non_junit_xml_exit_2(self):
        other = self.write("other.xml", "<project><build/></project>")
        code = self.run_cli(["junit-tally", other])
        self.assertEqual(code, 2)

    def test_nonexistent_path_exit_2(self):
        code = self.run_cli(["junit-tally",
                             os.path.join(self.root, "ghost.xml")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
