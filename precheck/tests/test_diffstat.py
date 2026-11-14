"""diffstat 的测试（需要本机有 git；没有则整体跳过）。"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from precheck import cli, diffstat

GIT = shutil.which("git")
GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME="precheck-test",
               GIT_AUTHOR_EMAIL="precheck@example.invalid",
               GIT_COMMITTER_NAME="precheck-test",
               GIT_COMMITTER_EMAIL="precheck@example.invalid")


def git(repo, *args):
    proc = subprocess.run([GIT, "-C", repo] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=GIT_ENV)
    if proc.returncode != 0:
        raise AssertionError("git %s 失败：%s" % (
            " ".join(args),
            proc.stderr.decode("utf-8", errors="replace")))
    return proc


@unittest.skipUnless(GIT, "本机没有 git，跳过 diffstat 测试")
class DiffstatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        self.write("src/app.py", "a\nb\nc\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")

    def write(self, rel, data):
        path = os.path.join(self.repo, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(path, mode) as fh:
            fh.write(data)
        return path

    def stat(self, base="HEAD", includes=None, excludes=None):
        return diffstat.repo_diffstat(self.repo, base,
                                      includes or [], excludes or [])

    def test_clean_repo_has_no_changes(self):
        result = self.stat()
        self.assertEqual(result["modified"], [])
        self.assertEqual(result["added"], [])
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["untracked"], [])
        self.assertEqual(result["tracked_insertions"], 0)

    def test_modified_file_line_counts(self):
        self.write("src/app.py", "a\nb\nc\nd\ne\n")  # +2 行
        result = self.stat()
        self.assertEqual(len(result["modified"]), 1)
        item = result["modified"][0]
        self.assertEqual(item["path"], "src/app.py")
        self.assertEqual(item["insertions"], 2)
        self.assertEqual(item["deletions"], 0)
        self.assertEqual(result["tracked_insertions"], 2)

    def test_staged_new_file_is_added(self):
        self.write("src/new.py", "x\ny\n")
        git(self.repo, "add", "src/new.py")
        result = self.stat()
        self.assertEqual([i["path"] for i in result["added"]],
                         ["src/new.py"])
        self.assertEqual(result["added"][0]["insertions"], 2)

    def test_deleted_file(self):
        os.remove(os.path.join(self.repo, "src", "app.py"))
        result = self.stat()
        self.assertEqual([i["path"] for i in result["deleted"]],
                         ["src/app.py"])
        self.assertEqual(result["deleted"][0]["deletions"], 3)

    def test_untracked_text_and_binary_files(self):
        self.write("notes.txt", "one\ntwo\nthree")  # 末行无换行也算一行
        self.write("blob.bin", b"\x00\x01\x02\xff" * 10)
        result = self.stat()
        rows = {u["path"]: u for u in result["untracked"]}
        self.assertEqual(rows["notes.txt"]["lines"], 3)
        self.assertFalse(rows["notes.txt"]["binary"])
        self.assertTrue(rows["blob.bin"]["binary"])
        self.assertIsNone(rows["blob.bin"]["lines"])
        self.assertEqual(result["untracked_lines"], 3)

    def test_rename_reported_as_modified_with_origin(self):
        git(self.repo, "mv", "src/app.py", "src/renamed.py")
        result = self.stat()
        self.assertEqual(len(result["modified"]), 1)
        item = result["modified"][0]
        self.assertEqual(item["path"], "src/renamed.py")
        self.assertEqual(item.get("renamed_from"), "src/app.py")

    def test_exclude_directory(self):
        self.write("target/gen.py", "g\n")
        self.write("keep.txt", "k\n")
        result = self.stat(excludes=["target/"])
        paths = [u["path"] for u in result["untracked"]]
        self.assertEqual(paths, ["keep.txt"])

    def test_include_filter(self):
        self.write("a.txt", "a\n")
        self.write("b.py", "b\n")
        result = self.stat(includes=["*.py"])
        paths = [u["path"] for u in result["untracked"]]
        self.assertEqual(paths, ["b.py"])

    def test_invalid_base_raises(self):
        from precheck.errors import PrecheckError
        with self.assertRaises(PrecheckError):
            self.stat(base="no-such-ref")

    def test_chinese_filename_untracked(self):
        self.write("中文 文件.txt", "内容\n第二行\n")
        result = self.stat()
        rows = {u["path"]: u for u in result["untracked"]}
        self.assertIn("中文 文件.txt", rows)
        self.assertEqual(rows["中文 文件.txt"]["lines"], 2)

    def test_json_output_shape(self):
        self.write("notes.txt", "one\n")
        lines, errs = [], []
        code = diffstat.run_diffstat([self.repo], "HEAD", [], [], True,
                                     lines.append, errs.append)
        self.assertEqual(code, 0)
        payload = json.loads("\n".join(lines))
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["repos"][0]["untracked"][0]["path"],
                         "notes.txt")


class DiffstatCliErrorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    @unittest.skipUnless(GIT, "本机没有 git")
    def test_non_git_directory_exit_2(self):
        plain = os.path.join(self.root, "plain")
        os.makedirs(plain)
        with redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()):
            code = cli.main(["diffstat", plain])
        self.assertEqual(code, 2)

    def test_missing_directory_exit_2(self):
        with redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()):
            code = cli.main(["diffstat",
                             os.path.join(self.root, "ghost")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
