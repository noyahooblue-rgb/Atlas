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

    # ---- M1：--base 参数注入防护 ----

    def test_base_injection_rejected_and_writes_nothing(self):
        from precheck.errors import PrecheckError
        target = os.path.join(self._tmp.name, "INJECTED.txt")
        with self.assertRaises(PrecheckError):
            self.stat(base="--output=%s" % target)
        self.assertFalse(os.path.exists(target))

    def test_base_starting_with_dash_rejected(self):
        from precheck.errors import PrecheckError
        for bad in ("-x", "--numstat", "--output=/tmp/evil"):
            with self.assertRaises(PrecheckError):
                self.stat(base=bad)

    def test_git_calls_use_optional_locks_env(self):
        captured = []
        real_run = diffstat.subprocess.run

        class FakeProc(object):
            returncode = 0
            stdout = b""
            stderr = b""

        def fake_run(cmd, **kwargs):
            captured.append(kwargs.get("env") or {})
            return FakeProc()

        diffstat.subprocess.run = fake_run
        try:
            self.stat()
        finally:
            diffstat.subprocess.run = real_run
        self.assertTrue(captured)
        for env in captured:
            self.assertEqual(env.get("GIT_OPTIONAL_LOCKS"), "0")

    # ---- S2：只差行尾标注 ----

    def test_eol_only_change_annotated(self):
        git(self.repo, "config", "core.autocrlf", "false")
        self.write("src/app.py", b"a\r\nb\r\nc\r\n")  # 仅行尾 LF→CRLF
        result = self.stat()
        self.assertEqual(len(result["modified"]), 1)
        self.assertTrue(result["modified"][0].get("eol_only"))

    def test_content_change_not_annotated_eol_only(self):
        git(self.repo, "config", "core.autocrlf", "false")
        self.write("src/app.py", b"a\r\nB\r\nc\r\n")  # 行尾 + 内容都变
        result = self.stat()
        self.assertEqual(len(result["modified"]), 1)
        self.assertFalse(result["modified"][0].get("eol_only", False))

    def test_eol_only_text_output(self):
        git(self.repo, "config", "core.autocrlf", "false")
        self.write("src/app.py", b"a\r\nb\r\nc\r\n")
        lines, errs = [], []
        code = diffstat.run_diffstat([self.repo], "HEAD", [], [], False,
                                     lines.append, errs.append)
        self.assertEqual(code, 0)
        self.assertIn("只差行尾", "\n".join(lines))

    # ---- M3：声明清单对账 ----

    def declare(self, text):
        path = os.path.join(self._tmp.name, "declared.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def run_declared(self, declared_path, excludes=None, as_json=False):
        lines, errs = [], []
        code = diffstat.run_diffstat(
            [self.repo], "HEAD", [], excludes or [], as_json,
            lines.append, errs.append, declared_path=declared_path)
        return code, "\n".join(lines), errs

    def test_declared_all_changes_covered_exit_0(self):
        self.write("src/app.py", "a\nb\nc\nd\n")
        self.write("new.txt", "n\n")
        declared = self.declare("src/app.py\nnew.txt\n")
        code, _text, _errs = self.run_declared(declared)
        self.assertEqual(code, 0)

    def test_declared_undeclared_modified_exit_1(self):
        self.write("src/app.py", "a\nb\nc\nd\n")
        self.write("other.py", "o\n")
        git(self.repo, "add", "other.py")
        git(self.repo, "commit", "-qm", "add other")
        self.write("other.py", "o\np\n")  # 未声明的已修改文件
        declared = self.declare("src/app.py\n")
        code, text, _errs = self.run_declared(declared)
        self.assertEqual(code, 1)
        self.assertIn("未声明改动 1", text)
        self.assertIn("other.py", text)

    def test_declared_undeclared_untracked_exit_1(self):
        self.write("stray.txt", "s\n")
        declared = self.declare("src/app.py\n")
        code, text, _errs = self.run_declared(declared)
        self.assertEqual(code, 1)
        self.assertIn("stray.txt", text)

    def test_declared_glob_match(self):
        self.write("src/app.py", "a\nb\nc\nd\n")
        self.write("src/deep/nested.py", "n\n")
        declared = self.declare("src/**\n")
        code, _text, _errs = self.run_declared(declared)
        self.assertEqual(code, 0)

    def test_declared_glob_star_does_not_cross_directories(self):
        self.write("src/deep/nested.py", "n\n")
        declared = self.declare("src/*.py\n")
        code, text, _errs = self.run_declared(declared)
        self.assertEqual(code, 1)
        self.assertIn("nested.py", text)

    def test_declared_rename_old_or_new_path_counts(self):
        git(self.repo, "mv", "src/app.py", "src/renamed.py")
        declared_old = self.declare("src/app.py\n")
        code, _t, _e = self.run_declared(declared_old)
        self.assertEqual(code, 0)
        declared_new = self.declare("src/renamed.py\n")
        code, _t, _e = self.run_declared(declared_new)
        self.assertEqual(code, 0)

    def test_declared_file_missing_exit_2(self):
        ghost = os.path.join(self._tmp.name, "no-such-declared.txt")
        code, _text, errs = self.run_declared(ghost)
        self.assertEqual(code, 2)
        self.assertTrue(errs)

    def test_declared_backslash_paths_and_comments(self):
        self.write("src/app.py", "a\nb\nc\nd\n")
        declared = self.declare("# 本轮声明\n\nsrc\\app.py\n")
        code, _text, _errs = self.run_declared(declared)
        self.assertEqual(code, 0)

    def test_declared_but_unchanged_warns_only(self):
        self.write("src/app.py", "a\nb\nc\nd\n")
        declared = self.declare("src/app.py\nuntouched.txt\n")
        code, text, _errs = self.run_declared(declared)
        self.assertEqual(code, 0)
        self.assertIn("声明未改动 1", text)
        self.assertIn("untouched.txt", text)

    def test_declared_exclude_applied_first(self):
        self.write("target/gen.py", "g\n")  # 未跟踪构建产物
        declared = self.declare("src/app.py\n")
        code, _text, _errs = self.run_declared(
            declared, excludes=["target/"])
        self.assertEqual(code, 0)

    def test_declared_json_lists(self):
        self.write("stray.txt", "s\n")
        declared = self.declare("src/app.py\nuntouched.txt\n")
        code, text, _errs = self.run_declared(declared, as_json=True)
        self.assertEqual(code, 1)
        payload = json.loads(text)
        repo = payload["repos"][0]
        self.assertEqual([u["path"] for u in repo["undeclared_changes"]],
                         ["stray.txt"])
        self.assertEqual(repo["declared_not_changed"],
                         ["src/app.py", "untouched.txt"])

    # ---- S3：--java-classes ----

    def test_java_classes_conversion(self):
        self.write("src/main/java/com/example/Foo.java",
                   "package com.example;\nclass Foo {}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "add foo")
        self.write("src/main/java/com/example/Foo.java",
                   "package com.example;\nclass Foo { int x; }\n")
        self.write("src/main/java/com/example/Bar.java",
                   "package com.example;\nclass Bar {}\n")  # 未跟踪
        self.write("src/test/java/com/example/FooTest.java",
                   "class FooTest {}\n")  # 不在 src/main/java 下
        self.write("src/main/java/com/example/readme.txt", "x\n")
        result = diffstat.repo_diffstat(self.repo, "HEAD", [], [],
                                        java_classes=True)
        self.assertEqual(result["java_classes"],
                         ["com.example.Bar", "com.example.Foo"])

    def test_java_classes_text_output_one_line(self):
        self.write("src/main/java/org/demo/App.java",
                   "package org.demo;\nclass App {}\n")
        lines, errs = [], []
        code = diffstat.run_diffstat([self.repo], "HEAD", [], [], False,
                                     lines.append, errs.append,
                                     java_classes=True)
        self.assertEqual(code, 0)
        self.assertIn("org.demo.App", lines)

    def test_java_classes_multi_module_paths(self):
        # 根模块：src/main/java/ 位于仓库根（已跟踪文件改动）
        self.write("src/main/java/com/root/Root.java",
                   "package com.root;\nclass Root {}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "add root class")
        self.write("src/main/java/com/root/Root.java",
                   "package com.root;\nclass Root { int x; }\n")
        # 一层模块与两层模块（未跟踪）
        self.write("mod-a/src/main/java/com/a/A.java",
                   "package com.a;\nclass A {}\n")
        self.write("x/y/src/main/java/com/xy/XY.java",
                   "package com.xy;\nclass XY {}\n")
        # 模块下的测试源码不算
        self.write("mod-a/src/test/java/com/a/ATest.java",
                   "class ATest {}\n")
        result = diffstat.repo_diffstat(self.repo, "HEAD", [], [],
                                        java_classes=True)
        self.assertEqual(result["java_classes"],
                         ["com.a.A", "com.root.Root", "com.xy.XY"])
        self.assertEqual(result["java_classes_by_module"], {
            ".": ["com.root.Root"],
            "mod-a": ["com.a.A"],
            "x/y": ["com.xy.XY"],
        })

    def test_java_classes_module_group_text_output(self):
        self.write("mod-a/src/main/java/com/a/A.java",
                   "package com.a;\nclass A {}\n")
        self.write("src/main/java/com/root/Root.java",
                   "package com.root;\nclass Root {}\n")
        lines, errs = [], []
        code = diffstat.run_diffstat([self.repo], "HEAD", [], [], False,
                                     lines.append, errs.append,
                                     java_classes=True)
        self.assertEqual(code, 0)
        self.assertIn("com.a.A,com.root.Root", lines)  # 原有汇总行不变
        self.assertIn("模块 .：com.root.Root", lines)
        self.assertIn("模块 mod-a：com.a.A", lines)

    def test_java_classes_deleted_file_excluded(self):
        self.write("src/main/java/com/example/Gone.java",
                   "package com.example;\nclass Gone {}\n")
        self.write("mod-a/src/main/java/com/a/Kept.java",
                   "package com.a;\nclass Kept {}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "add classes")
        os.remove(os.path.join(
            self.repo, "src", "main", "java", "com", "example",
            "Gone.java"))
        self.write("mod-a/src/main/java/com/a/Kept.java",
                   "package com.a;\nclass Kept { int x; }\n")
        result = diffstat.repo_diffstat(self.repo, "HEAD", [], [],
                                        java_classes=True)
        self.assertEqual([i["path"] for i in result["deleted"]],
                         ["src/main/java/com/example/Gone.java"])
        self.assertEqual(result["java_classes"], ["com.a.Kept"])
        self.assertEqual(result["java_classes_by_module"],
                         {"mod-a": ["com.a.Kept"]})

    def test_java_class_name_last_segment_wins(self):
        name = diffstat.java_class_name
        self.assertEqual(name("src/main/java/com/example/Foo.java"),
                         "com.example.Foo")
        self.assertEqual(name("mod-a/src/main/java/com/a/A.java"),
                         "com.a.A")
        self.assertEqual(name("a/b/src/main/java/com/xy/XY.java"),
                         "com.xy.XY")
        # 同一路径出现两次片段时以最后一次为准
        self.assertEqual(
            name("a/src/main/java/b/src/main/java/com/deep/Deep.java"),
            "com.deep.Deep")
        self.assertEqual(
            diffstat.java_class_module(
                "a/src/main/java/b/src/main/java/com/deep/Deep.java"),
            "a/src/main/java/b")
        self.assertIsNone(name("src/test/java/com/example/T.java"))
        self.assertIsNone(name("mod-a/src/test/java/com/a/T.java"))
        self.assertIsNone(name("xsrc/main/java/com/example/F.java"))
        self.assertIsNone(name("src/main/java/com/example/readme.txt"))


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
