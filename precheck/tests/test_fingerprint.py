"""snapshot / verify 的测试。"""

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from precheck import cli, fingerprint


class Collector:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


class FingerprintTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def write(self, rel, data):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def snapshot(self, *specs, **kwargs):
        out = Collector()
        manifest = os.path.join(self.root, "manifest.json")
        code = fingerprint.run_snapshot(
            list(specs), manifest, kwargs.get("label"),
            kwargs.get("excludes", []), False, out,
            strip_fenced=kwargs.get("strip_fenced", []))
        self.assertEqual(code, 0)
        return manifest

    def verify(self, manifest, base=None):
        out = Collector()
        code = fingerprint.run_verify(manifest, base, False, out)
        return code, out.text


class SnapshotVerifyTests(FingerprintTestBase):

    def test_unchanged_tree_verifies_ok(self):
        self.write("docs/a.txt", b"hello\nworld\n")
        self.write("docs/b.txt", b"second file\n")
        manifest = self.snapshot(os.path.join(self.root, "docs"))
        code, text = self.verify(manifest)
        self.assertEqual(code, 0)
        self.assertIn("一致 2", text)

    def test_content_change_reports_first_diff_line(self):
        self.write("a.txt", b"line1\nline2\nline3\n")
        manifest = self.snapshot(os.path.join(self.root, "a.txt"))
        self.write("a.txt", b"line1\nCHANGED\nline3\n")
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("实质内容变化", text)
        self.assertIn("第 2 行", text)

    def test_only_eol_change_lf_to_crlf(self):
        self.write("a.txt", b"one\ntwo\nthree\n")
        manifest = self.snapshot(os.path.join(self.root, "a.txt"))
        self.write("a.txt", b"one\r\ntwo\r\nthree\r\n")
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("只差行尾", text)

    def test_only_eol_change_cr_to_lf(self):
        self.write("a.txt", b"one\rtwo\rthree\r")
        manifest = self.snapshot(os.path.join(self.root, "a.txt"))
        self.write("a.txt", b"one\ntwo\nthree\n")
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("只差行尾", text)

    def test_only_trailing_blank_line_added(self):
        self.write("a.txt", b"content\n")
        manifest = self.snapshot(os.path.join(self.root, "a.txt"))
        self.write("a.txt", b"content\n\n\n")
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("只差文件末尾空行或空白", text)

    def test_only_trailing_spaces_at_eof(self):
        self.write("a.txt", b"content\nlast")
        manifest = self.snapshot(os.path.join(self.root, "a.txt"))
        self.write("a.txt", b"content\nlast   \n")
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("只差文件末尾空行或空白", text)

    def test_bom_added(self):
        self.write("a.txt", b"plain\n")
        manifest = self.snapshot(os.path.join(self.root, "a.txt"))
        self.write("a.txt", b"\xef\xbb\xbfplain\n")
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("只差 BOM", text)

    def test_bom_removed(self):
        self.write("a.txt", b"\xef\xbb\xbfplain\n")
        manifest = self.snapshot(os.path.join(self.root, "a.txt"))
        self.write("a.txt", b"plain\n")
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("只差 BOM", text)

    def test_eol_and_bom_combo_is_not_content_change(self):
        self.write("a.txt", b"one\ntwo\n")
        manifest = self.snapshot(os.path.join(self.root, "a.txt"))
        self.write("a.txt", b"\xef\xbb\xbfone\r\ntwo\r\n")
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("仅行尾与 BOM 差异", text)
        self.assertNotIn("（实质内容变化）", text)

    def test_missing_and_extra_files(self):
        docs = os.path.join(self.root, "docs")
        self.write("docs/a.txt", b"a\n")
        self.write("docs/b.txt", b"b\n")
        manifest = self.snapshot(docs)
        os.remove(os.path.join(docs, "b.txt"))
        self.write("docs/c.txt", b"c\n")
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("缺失    b.txt", text)
        self.assertIn("多出    c.txt", text)

    def test_exclude_directory_and_extension(self):
        docs = os.path.join(self.root, "proj")
        self.write("proj/src/main.py", b"x = 1\n")
        self.write("proj/target/out.py", b"y = 2\n")
        self.write("proj/notes.tmp", b"tmp\n")
        manifest = self.snapshot(docs, excludes=["target/", "*.tmp"])
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
        paths = [e["path"] for e in data["files"]]
        self.assertEqual(paths, ["src/main.py"])

    def test_glob_pattern(self):
        self.write("docs/a.md", b"# a\n")
        self.write("docs/b.md", b"# b\n")
        self.write("docs/c.txt", b"c\n")
        manifest = self.snapshot(os.path.join(self.root, "docs", "*.md"))
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual([e["path"] for e in data["files"]],
                         ["a.md", "b.md"])

    def test_chinese_path_and_space_in_name(self):
        folder = os.path.join(self.root, "中文 目录")
        self.write("中文 目录/说明 文档.txt", "你好\n世界\n".encode("utf-8"))
        manifest = self.snapshot(folder)
        code, text = self.verify(manifest)
        self.assertEqual(code, 0)
        self.write("中文 目录/说明 文档.txt", "你好\n改了\n".encode("utf-8"))
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("说明 文档.txt", text)

    def test_binary_file_roundtrip_and_change(self):
        blob = bytes(range(256)) + b"\x00\xff\r\n"
        self.write("bin/data.bin", blob)
        manifest = self.snapshot(os.path.join(self.root, "bin"))
        code, _ = self.verify(manifest)
        self.assertEqual(code, 0)
        mutated = bytearray(blob)
        mutated[10] ^= 0xFF
        self.write("bin/data.bin", bytes(mutated))
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("变了", text)

    def test_manifest_fields(self):
        content = b"\xef\xbb\xbfone\r\ntwo\nthree\r"
        self.write("a.txt", content)
        manifest = self.snapshot(os.path.join(self.root, "a.txt"),
                                 label="批准版 v1")
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["label"], "批准版 v1")
        self.assertIn("created", data)
        entry = data["files"][0]
        self.assertEqual(entry["sha256"],
                         hashlib.sha256(content).hexdigest())
        self.assertEqual(entry["size"], len(content))
        self.assertTrue(entry["bom"])
        self.assertEqual(entry["line_endings"],
                         {"crlf": 1, "lf": 1, "cr": 1})
        self.assertEqual(entry["path"], "a.txt")

    def test_verify_with_base_override(self):
        import shutil
        src = os.path.join(self.root, "tree")
        self.write("tree/x.txt", b"x\n")
        self.write("tree/sub/y.txt", b"y\n")
        manifest = self.snapshot(src)
        dst = os.path.join(self.root, "copy")
        shutil.copytree(src, dst)
        code, text = self.verify(manifest, base=dst)
        self.assertEqual(code, 0, text)
        self.write("copy/extra.txt", b"new\n")
        code, text = self.verify(manifest, base=dst)
        self.assertEqual(code, 1)
        self.assertIn("多出    extra.txt", text)

    def test_gbk_byte_filename_does_not_crash(self):
        # 文件名是 GBK 字节（在 UTF-8 文件系统上是非法 UTF-8 序列）时，
        # snapshot/verify 不能崩，清单要能原样读回。
        raw_root = os.fsencode(self.root)
        folder = os.path.join(raw_root, b"gbk")
        os.makedirs(folder, exist_ok=True)
        name = "报告".encode("gbk") + b".txt"
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(b"data\n")
        manifest = self.snapshot(os.path.join(self.root, "gbk"))
        code, _text = self.verify(manifest)
        self.assertEqual(code, 0)

    def test_duplicate_specs_deduped(self):
        target = self.write("a.txt", b"a\n")
        manifest = self.snapshot(target, target,
                                 os.path.join(self.root))
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
        paths = [e["path"] for e in data["files"]]
        self.assertEqual(paths.count("a.txt"), 1)


DOC_V1 = (b"# Spec\n"
          b"body line one\n"
          b"body line two\n"
          b"```stage-approvals\n"
          b"approved by Alice 2026-01-01\n"
          b"```\n")


class StripFencedTests(FingerprintTestBase):
    """M2：正文指纹（剥离审批块）。"""

    def snap(self, rel="doc.md", data=DOC_V1):
        path = self.write(rel, data)
        return self.snapshot(path, strip_fenced=["stage-approvals"])

    def test_approval_block_only_change_is_ok(self):
        manifest = self.snap()
        self.write("doc.md", DOC_V1.replace(
            b"approved by Alice 2026-01-01",
            b"approved by Bob 2026-01-02"))
        code, text = self.verify(manifest)
        self.assertEqual(code, 0)
        self.assertIn("只差审批块", text)
        self.assertIn("doc.md", text)

    def test_manifest_records_body_fingerprint_and_blocks(self):
        manifest = self.snap()
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["strip_fenced"], ["stage-approvals"])
        entry = data["files"][0]
        self.assertIn("body_sha256", entry)
        self.assertNotEqual(entry["body_sha256"], entry["sha256"])
        self.assertEqual(entry["stripped_blocks"],
                         [{"name": "stage-approvals",
                           "start_line": 4, "end_line": 6}])

    def test_body_and_approval_change_reports_original_line(self):
        manifest = self.snap()
        changed = DOC_V1.replace(b"body line two", b"body line TWO")
        changed = changed.replace(b"approved by Alice",
                                  b"approved by Carol")
        self.write("doc.md", changed)
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("实质内容变化", text)
        self.assertIn("第 3 行", text)

    def test_diff_line_after_block_counts_original_lines(self):
        data = (b"intro\n"
                b"```stage-approvals\n"
                b"approved by Alice\n"
                b"```\n"
                b"tail line\n")
        manifest = self.snap(data=data)
        self.write("doc.md", data.replace(b"tail line", b"tail LINE"))
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("第 5 行", text)

    def test_two_approval_blocks(self):
        data = (b"head\n"
                b"```stage-approvals\n"
                b"first\n"
                b"```\n"
                b"middle\n"
                b"```stage-approvals\n"
                b"second\n"
                b"```\n")
        manifest = self.snap(data=data)
        with open(manifest, encoding="utf-8") as fh:
            recorded = json.load(fh)
        self.assertEqual(len(recorded["files"][0]["stripped_blocks"]), 2)
        self.write("doc.md", data.replace(b"second", b"SECOND"))
        code, text = self.verify(manifest)
        self.assertEqual(code, 0)
        self.assertIn("只差审批块", text)

    def test_unclosed_approval_block_snapshot_exit_2(self):
        from precheck.errors import PrecheckError
        path = self.write("doc.md",
                          b"body\n```stage-approvals\nnever closed\n")
        out = Collector()
        with self.assertRaises(PrecheckError) as ctx:
            fingerprint.run_snapshot(
                [path], os.path.join(self.root, "m.json"), None, [],
                False, out, strip_fenced=["stage-approvals"])
        self.assertIn("第 2 行", str(ctx.exception))

    def test_unclosed_approval_block_verify_exit_2(self):
        from precheck.errors import PrecheckError
        manifest = self.snap()
        self.write("doc.md",
                   b"# Spec\nbody line one\nbody line two\n"
                   b"```stage-approvals\napproved by Alice 2026-01-01\n")
        with self.assertRaises(PrecheckError) as ctx:
            fingerprint.run_verify(manifest, None, False, Collector())
        self.assertIn("第 4 行", str(ctx.exception))

    def test_crlf_file_approval_change(self):
        data = DOC_V1.replace(b"\n", b"\r\n")
        manifest = self.snap(data=data)
        self.write("doc.md", data.replace(b"Alice", b"Bob"))
        code, text = self.verify(manifest)
        self.assertEqual(code, 0)
        self.assertIn("只差审批块", text)

    def test_other_language_fence_unaffected(self):
        data = (b"# Doc\n"
                b"```java\n"
                b"class A {}\n"
                b"```\n"
                b"```stage-approvals\n"
                b"approved by Alice\n"
                b"```\n")
        manifest = self.snap(data=data)
        # 只改审批块 → 只差审批块
        self.write("doc.md", data.replace(b"Alice", b"Bob"))
        code, text = self.verify(manifest)
        self.assertEqual(code, 0)
        self.assertIn("只差审批块", text)
        # 改 java 块内容 → 正文变了，退出 1
        self.write("doc.md", data.replace(b"class A {}", b"class B {}"))
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("实质内容变化", text)

    def test_approval_block_deleted_entirely(self):
        manifest = self.snap()
        self.write("doc.md",
                   b"# Spec\nbody line one\nbody line two\n")
        code, text = self.verify(manifest)
        self.assertEqual(code, 0)
        self.assertIn("只差审批块", text)

    def test_similar_info_string_not_stripped(self):
        # info 字符串只是包含 NAME 子串时不算命中，不得剥离
        data = (b"body\n"
                b"```stage-approvals-draft\n"
                b"draft content\n"
                b"```\n")
        manifest = self.snap(data=data)
        self.write("doc.md", data.replace(b"draft content",
                                          b"draft CHANGED"))
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("实质内容变化", text)

    def test_approval_marker_inside_other_block_is_content(self):
        # ```java 块内部出现的一行 ```stage-approvals 只是内容
        data = (b"```java\n"
                b"String s = \"x\";\n"
                b"```stage-approvals\n"
                b"more code\n"
                b"```\n")
        manifest = self.snap(data=data)
        self.write("doc.md", data.replace(b"more code", b"more CODE"))
        code, text = self.verify(manifest)
        self.assertEqual(code, 1)
        self.assertIn("实质内容变化", text)


class CliErrorTests(FingerprintTestBase):
    def run_cli(self, argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.main(argv)

    def test_verify_missing_manifest_exit_2(self):
        code = self.run_cli(["verify", os.path.join(self.root, "nope.json")])
        self.assertEqual(code, 2)

    def test_verify_corrupt_manifest_exit_2(self):
        bad = self.write("bad.json", b"{not json")
        code = self.run_cli(["verify", bad])
        self.assertEqual(code, 2)

    def test_snapshot_no_match_exit_2(self):
        code = self.run_cli(["snapshot",
                             os.path.join(self.root, "*.nothing")])
        self.assertEqual(code, 2)

    def test_snapshot_missing_path_exit_2(self):
        code = self.run_cli(["snapshot", os.path.join(self.root, "ghost")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
