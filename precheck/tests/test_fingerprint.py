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
            kwargs.get("excludes", []), False, out)
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
