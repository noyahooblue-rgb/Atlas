"""M2：snapshot --strip-fenced / verify 剥离审批块后比对正文的测试。"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from precheck import cli, fingerprint

APPROVAL_1 = (b"```stage-approvals\n"
              b"stage: design\n"
              b"approved-by: reviewer-a\n"
              b"commit: 1111111\n"
              b"```\n")
APPROVAL_2 = (b"```stage-approvals\n"
              b"stage: design\n"
              b"approved-by: reviewer-a\n"
              b"commit: 2222222\n"
              b"stage: build\n"
              b"approved-by: reviewer-b\n"
              b"```\n")

DOC_BODY = (b"# Sample Design Doc\n"          # 1
            b"\n"                              # 2
            b"Line three of the body.\n"       # 3
            b"Line four of the body.\n"        # 4
            b"\n")                             # 5


class Collector:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


class StripFencedBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.manifest = os.path.join(self.root, "manifest.json")

    def write(self, rel, data):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def snapshot(self, path, names=("stage-approvals",)):
        code = fingerprint.run_snapshot([path], self.manifest, None, [],
                                        False, Collector(), list(names))
        self.assertEqual(code, 0)
        with open(self.manifest, encoding="utf-8") as fh:
            return json.load(fh)

    def verify(self, as_json=False):
        out = Collector()
        code = fingerprint.run_verify(self.manifest, None, as_json, out)
        return code, out.text

    def run_cli(self, argv):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(argv)
        return code, err.getvalue()


class StripFencedTests(StripFencedBase):

    def test_only_approval_block_changed(self):
        path = self.write("doc.md", DOC_BODY + APPROVAL_1)
        self.snapshot(path)
        self.write("doc.md", DOC_BODY + APPROVAL_2)
        code, text = self.verify()
        self.assertEqual(code, 0, text)
        self.assertIn("只差审批块", text)
        self.assertIn("doc.md", text)

    def test_only_approval_block_changed_json(self):
        path = self.write("doc.md", DOC_BODY + APPROVAL_1)
        self.snapshot(path)
        self.write("doc.md", DOC_BODY + APPROVAL_2)
        code, text = self.verify(as_json=True)
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["approval_only"], 1)
        self.assertEqual(payload["results"][0]["status"], "approval_only")

    def test_body_line_and_block_changed_reports_original_line(self):
        # 审批块放在正文中间，验证行号按原文件计而不是按正文计
        before = (b"# Title\n"          # 1
                  + APPROVAL_1          # 2-6
                  + b"alpha\n"          # 7
                  b"beta\n"             # 8
                  b"gamma\n")           # 9
        after = (b"# Title\n"           # 1
                 + APPROVAL_2           # 2-8
                 + b"alpha\n"           # 9
                 b"BETA-CHANGED\n"      # 10
                 b"gamma\n")            # 11
        path = self.write("doc.md", before)
        self.snapshot(path)
        self.write("doc.md", after)
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("实质内容变化", text)
        self.assertIn("第 10 行", text)
        self.assertNotIn("只差审批块", text.split("汇总")[0])

    def test_two_approval_blocks(self):
        before = b"intro\n" + APPROVAL_1 + b"middle\n" + APPROVAL_1 + b"end\n"
        after = b"intro\n" + APPROVAL_2 + b"middle\n" + APPROVAL_2 + b"end\n"
        path = self.write("doc.md", before)
        data = self.snapshot(path)
        entry = data["files"][0]
        self.assertEqual(entry["strip"]["block_count"], 2)
        self.assertEqual(
            [(b["start_line"], b["end_line"])
             for b in entry["strip"]["blocks"]],
            [(2, 6), (8, 12)])
        self.write("doc.md", after)
        code, text = self.verify()
        self.assertEqual(code, 0, text)
        self.assertIn("只差审批块", text)

    def test_unclosed_block_snapshot_exit_2(self):
        path = self.write("doc.md", DOC_BODY
                          + b"```stage-approvals\nstage: x\n")
        code, err = self.run_cli(["snapshot", path, "-o", self.manifest,
                                  "--strip-fenced", "stage-approvals"])
        self.assertEqual(code, 2)
        self.assertIn("第 6 行", err)
        self.assertFalse(os.path.exists(self.manifest))

    def test_unclosed_block_verify_exit_2(self):
        path = self.write("doc.md", DOC_BODY + APPROVAL_1)
        self.snapshot(path)
        self.write("doc.md", DOC_BODY + b"```stage-approvals\nstage: x\n")
        code, err = self.run_cli(["verify", self.manifest])
        self.assertEqual(code, 2)
        self.assertIn("第 6 行", err)

    def test_crlf_file(self):
        crlf = lambda b: b.replace(b"\n", b"\r\n")  # noqa: E731
        path = self.write("doc.md", crlf(DOC_BODY + APPROVAL_1))
        data = self.snapshot(path)
        self.assertEqual(data["files"][0]["strip"]["block_count"], 1)
        self.write("doc.md", crlf(DOC_BODY + APPROVAL_2))
        code, text = self.verify()
        self.assertEqual(code, 0, text)
        self.assertIn("只差审批块", text)
        # 正文改行尾（CRLF→LF）仍按「只差行尾」细分，不被审批块吞掉
        self.write("doc.md", DOC_BODY + crlf(APPROVAL_2))
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("只差行尾", text)

    def test_other_language_fence_untouched(self):
        java_v1 = b"```java\nclass Demo {}\n```\n"
        java_v2 = b"```java\nclass Demo { int x; }\n```\n"
        path = self.write("doc.md", b"# Doc\n" + java_v1 + APPROVAL_1)
        data = self.snapshot(path)
        self.assertEqual(data["files"][0]["strip"]["block_count"], 1)
        self.write("doc.md", b"# Doc\n" + java_v2 + APPROVAL_2)
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("实质内容变化", text)
        self.assertIn("第 3 行", text)

    def test_approval_fence_inside_other_fence_is_not_stripped(self):
        inner = (b"````markdown\n"
                 b"```stage-approvals\n"
                 b"example only\n"
                 b"```\n"
                 b"````\n")
        path = self.write("doc.md", b"# Doc\n" + inner)
        data = self.snapshot(path)
        self.assertEqual(data["files"][0]["strip"]["block_count"], 0)

    def test_approval_block_deleted_entirely(self):
        path = self.write("doc.md", DOC_BODY + APPROVAL_1)
        self.snapshot(path)
        self.write("doc.md", DOC_BODY)
        code, text = self.verify()
        self.assertEqual(code, 0, text)
        self.assertIn("只差审批块", text)

    def test_manifest_records_both_hashes_and_rule(self):
        import hashlib
        content = DOC_BODY + APPROVAL_1
        path = self.write("doc.md", content)
        data = self.snapshot(path)
        self.assertEqual(data["strip_fenced"], ["stage-approvals"])
        entry = data["files"][0]
        self.assertEqual(entry["sha256"],
                         hashlib.sha256(content).hexdigest())
        self.assertEqual(entry["strip"]["sha256_body"],
                         hashlib.sha256(DOC_BODY).hexdigest())
        self.assertEqual(entry["strip"]["blocks"],
                         [{"name": "stage-approvals",
                           "start_line": 6, "end_line": 10}])

    def test_body_keeps_bom_and_eol_bytes(self):
        body, blocks, _ = fingerprint.strip_fenced(
            b"\xef\xbb\xbfa\r\n" + APPROVAL_1 + b"b\r",
            ["stage-approvals"])
        self.assertEqual(body, b"\xef\xbb\xbfa\r\nb\r")
        self.assertEqual(len(blocks), 1)

    def test_without_rule_behaviour_unchanged(self):
        path = self.write("doc.md", DOC_BODY + APPROVAL_1)
        self.snapshot(path, names=())
        self.write("doc.md", DOC_BODY + APPROVAL_2)
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("实质内容变化", text)


if __name__ == "__main__":
    unittest.main()
