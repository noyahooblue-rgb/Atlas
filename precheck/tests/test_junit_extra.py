"""S1：junit-tally 属性不符警告与 flaky/rerun 计数的测试。"""

import json
import os
import tempfile
import unittest

from precheck import junit_tally

SUITE_ATTR_WRONG = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.ClaimTest" tests="5" failures="0" errors="0" skipped="0">
  <testcase classname="com.example.ClaimTest" name="a"/>
  <testcase classname="com.example.ClaimTest" name="b">
    <failure message="boom"/>
  </testcase>
</testsuite>
"""

SUITE_FLAKY = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.FlakyTest" tests="4" failures="0" errors="0" skipped="0">
  <testcase classname="com.example.FlakyTest" name="f1">
    <flakyFailure message="first run failed"/>
  </testcase>
  <testcase classname="com.example.FlakyTest" name="f2">
    <flakyError message="first run errored"/>
  </testcase>
  <testcase classname="com.example.FlakyTest" name="f3">
    <rerunFailure message="r1"/>
    <rerunError message="r2"/>
  </testcase>
  <testcase classname="com.example.FlakyTest" name="ok"/>
</testsuite>
"""


def no_expects():
    return {"tests": None, "failures": None, "errors": None,
            "skipped": None}


class JunitExtraTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def run_tally(self, paths, as_json=False):
        lines = []
        code = junit_tally.run_tally(paths, no_expects(), as_json,
                                     lines.append)
        return code, "\n".join(lines)

    def test_attribute_mismatch_warns_without_changing_exit(self):
        path = self.write("claim.xml", SUITE_ATTR_WRONG)
        result = junit_tally.tally([path])
        self.assertEqual(result["totals"]["tests"], 2)
        attrs = {(m["attribute"], m["claimed"], m["actual"])
                 for m in result["attribute_mismatches"]}
        self.assertEqual(attrs, {("tests", 5, 2), ("failures", 0, 1)})
        code, text = self.run_tally([path])
        self.assertEqual(code, 0)
        self.assertIn("属性与用例统计不符", text)

    def test_matching_attributes_no_warning(self):
        path = self.write("ok.xml", SUITE_FLAKY.replace("FlakyTest", "X")
                          .replace("<flakyFailure message=\"first run "
                                   "failed\"/>", ""))
        result = junit_tally.tally([path])
        self.assertEqual(result["attribute_mismatches"], [])

    def test_flaky_and_rerun_counted_and_warned(self):
        path = self.write("flaky.xml", SUITE_FLAKY)
        code, text = self.run_tally([path])
        self.assertEqual(code, 0)
        self.assertIn("重跑后才通过", text)
        code, js = self.run_tally([path], as_json=True)
        payload = json.loads(js)
        self.assertEqual(payload["flaky_total"], 3)
        self.assertEqual(payload["totals"]["failures"], 0)
        self.assertEqual(payload["files"][0]["flaky"], 3)


if __name__ == "__main__":
    unittest.main()
