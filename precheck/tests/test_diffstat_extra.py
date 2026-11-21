"""M1 / M3 / S2 / S3：diffstat 新功能的测试（需要本机有 git；没有则跳过）。"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from precheck import cli, diffstat
from precheck.errors import PrecheckError
from precheck.util import declared_to_regex

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


def snapshot_tree(root):
    """目录树里所有文件的 (相对路径, mtime_ns, size) 集合，用于证明没写文件。"""
    state = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            st = os.stat(full)
            state.add((os.path.relpath(full, root), st.st_mtime_ns,
                       st.st_size))
    return state


class DeclaredPatternTests(unittest.TestCase):
    """不依赖 git 的通配规则测试。"""

    def check(self, pattern, path):
        return bool(declared_to_regex(pattern).match(path))

    def test_double_star_crosses_directories(self):
        self.assertTrue(self.check("src/**/*.java", "src/a/b/C.java"))
        self.assertTrue(self.check("src/**/*.java", "src/C.java"))
        self.assertTrue(self.check("docs/**", "docs/x/y.md"))

    def test_single_star_does_not_cross(self):
        self.assertFalse(self.check("src/*.py", "src/x/y.py"))
        self.assertTrue(self.check("src/*.py", "src/y.py"))

    def test_backslash_and_dir_suffix(self):
        self.assertTrue(self.check("docs\\guide\\a.md", "docs/guide/a.md"))
        self.assertTrue(self.check("build\\", "build/out/x.txt"))
        self.assertTrue(self.check("./README.md", "README.md"))

    def test_java_class_name_rules(self):
        f = diffstat.java_class_name
        self.assertEqual(f("src/main/java/com/example/demo/Foo.java"),
                         "com.example.demo.Foo")
        self.assertEqual(f("mod-a/src/main/java/org/sample/Bar.java"),
                         "org.sample.Bar")
        self.assertIsNone(f("src/test/java/com/example/FooTest.java"))
        self.assertIsNone(f("src/main/java/com/example/package-info.java"))
        self.assertIsNone(f("src/main/resources/x.java.txt"))

    def test_injected_base_rejected_before_git(self):
        for bad in ("--output=/tmp/x", "-p", "-"):
            with self.assertRaises(PrecheckError):
                diffstat.validate_base(bad)


@unittest.skipUnless(GIT, "本机没有 git，跳过 diffstat 新功能测试")
class DiffstatNewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "core.autocrlf", "false")
        self.write("src/app.py", "a\nb\nc\n")
        self.write("docs/guide.md", "guide\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")

    def write(self, rel, data, root=None):
        path = os.path.join(root or self.repo, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(path, mode) as fh:
            fh.write(data)
        return path

    def declared(self, text):
        return self.write("declared.txt", text.encode("utf-8"),
                          root=self.root)

    def ds(self, declared=None, excludes=None, java=False, as_json=False,
           base="HEAD"):
        lines, errs = [], []
        code = diffstat.run_diffstat([self.repo], base, [], excludes or [],
                                     as_json, lines.append, errs.append,
                                     declared, java)
        return code, "\n".join(lines), errs

    def run_cli(self, argv):
        with redirect_stdout(io.StringIO()) as out, \
                redirect_stderr(io.StringIO()) as err:
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    # ---- M1 ---------------------------------------------------------------

    def test_m1_injected_base_rejected_and_nothing_written(self):
        target = os.path.join(self.root, "INJECTED.txt")
        before = snapshot_tree(self.repo)
        for value in ("--output=" + target, "-o" + target):
            code, _out, err = self.run_cli(
                ["diffstat", self.repo, "--base=" + value])
            self.assertEqual(code, 2)
            self.assertIn("不能以 -", err)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(snapshot_tree(self.repo), before)

    def test_m1_status_does_not_touch_index(self):
        index = os.path.join(self.repo, ".git", "index")
        # 改动文件 mtime，正常 git status 会想刷新 index
        self.write("src/app.py", "a\nb\nc\n")
        st_before = os.stat(index)
        code, _t, _e = self.ds()
        self.assertEqual(code, 0)
        st_after = os.stat(index)
        self.assertEqual(st_before.st_mtime_ns, st_after.st_mtime_ns)
        self.assertEqual(st_before.st_size, st_after.st_size)

    def test_m1_commit_sha_base_still_works(self):
        sha = git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        self.write("src/app.py", "a\nb\nc\nd\n")
        result = diffstat.repo_diffstat(self.repo, sha, [], [])
        self.assertEqual(result["modified"][0]["insertions"], 1)
        self.assertEqual(result["base_commit"], sha)

    # ---- M3 ---------------------------------------------------------------

    def test_m3_all_declared_exit_0(self):
        self.write("src/app.py", "a\nb\nc\nd\n")
        self.write("src/new.py", "n\n")
        decl = self.declared("# 本轮登记\nsrc/app.py\n\nsrc/new.py\n"
                             "docs/guide.md\n")
        code, text, _ = self.ds(declared=decl)
        self.assertEqual(code, 0, text)
        self.assertIn("未声明改动 0", text)
        self.assertIn("声明未改动 1", text)
        self.assertIn("docs/guide.md", text)

    def test_m3_undeclared_modified_exit_1(self):
        self.write("src/app.py", "a\nb\nc\nd\n")
        self.write("docs/guide.md", "guide v2\n")
        decl = self.declared("src/app.py\n")
        code, text, _ = self.ds(declared=decl, as_json=True)
        self.assertEqual(code, 1)
        payload = json.loads(text)
        d = payload["repos"][0]["declared"]
        self.assertEqual(d["undeclared"],
                         [{"path": "docs/guide.md", "category": "modified"}])
        self.assertEqual(d["declared_unchanged"], [])

    def test_m3_undeclared_untracked_exit_1(self):
        self.write("src/app.py", "a\nb\nc\nd\n")
        self.write("scratch/notes.txt", "x\n")
        decl = self.declared("src/app.py\n")
        code, text, _ = self.ds(declared=decl)
        self.assertEqual(code, 1)
        self.assertIn("未跟踪 scratch/notes.txt", text)

    def test_m3_wildcard_match(self):
        self.write("src/pkg/deep/mod.py", "m\n")
        self.write("src/app.py", "a\n")
        decl = self.declared("src/**/*.py\n")
        code, text, _ = self.ds(declared=decl)
        self.assertEqual(code, 0, text)

    def test_m3_rename_matches_old_or_new(self):
        git(self.repo, "mv", "src/app.py", "src/renamed.py")
        for line in ("src/app.py\n", "src/renamed.py\n"):
            decl = self.declared(line)
            code, text, _ = self.ds(declared=decl)
            self.assertEqual(code, 0, text)
        decl = self.declared("docs/guide.md\n")
        code, text, _ = self.ds(declared=decl)
        self.assertEqual(code, 1)
        self.assertIn("重命名 src/renamed.py（原 src/app.py）", text)

    def test_m3_missing_declared_file_exit_2(self):
        code, _o, err = self.run_cli(
            ["diffstat", self.repo, "--declared",
             os.path.join(self.root, "nope.txt")])
        self.assertEqual(code, 2)
        self.assertIn("声明清单", err)

    def test_m3_backslash_paths(self):
        self.write("docs/guide.md", "guide v2\n")
        self.write("src/app.py", "z\n")
        decl = self.declared("docs\guide.md\r\nsrc\*.py\r\n")
        code, text, _ = self.ds(declared=decl)
        self.assertEqual(code, 0, text)

    def test_m3_exclude_applied_before_reconcile(self):
        self.write("src/app.py", "a\nb\nc\nd\n")
        self.write("target/classes/Gen.class", b"\xca\xfe\xba\xbe\x00")
        decl = self.declared("src/app.py\n")
        code, text, _ = self.ds(declared=decl)
        self.assertEqual(code, 1)
        code, text, _ = self.ds(declared=decl, excludes=["target/"])
        self.assertEqual(code, 0, text)

    def test_m3_deleted_file_must_be_declared(self):
        os.remove(os.path.join(self.repo, "docs", "guide.md"))
        decl = self.declared("src/app.py\n")
        code, text, _ = self.ds(declared=decl)
        self.assertEqual(code, 1)
        self.assertIn("已删除 docs/guide.md", text)

    # ---- S2 ---------------------------------------------------------------

    def test_s2_eol_only_marked(self):
        self.write("src/app.py", b"a\r\nb\r\nc\r\n")
        self.write("docs/guide.md", "guide changed\n")
        code, text, _ = self.ds(as_json=True)
        self.assertEqual(code, 0)
        rows = {m["path"]: m for m in
                json.loads(text)["repos"][0]["modified"]}
        if not json.loads(text)["repos"][0]["eol_check"]:
            self.skipTest("本机 git 不支持 --ignore-cr-at-eol")
        self.assertTrue(rows["src/app.py"]["eol_only"])
        self.assertFalse(rows["docs/guide.md"]["eol_only"])
        code, plain, _ = self.ds()
        self.assertIn("src/app.py（+3 -3）（只差行尾）", plain)

    # ---- S3 ---------------------------------------------------------------

    def test_s3_java_classes_line(self):
        self.write("src/main/java/com/example/demo/Foo.java",
                   "class Foo {}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "java")
        self.write("src/main/java/com/example/demo/Foo.java",
                   "class Foo { int x; }\n")
        self.write("src/main/java/com/example/demo/Bar.java",
                   "class Bar {}\n")
        self.write("src/test/java/com/example/demo/FooTest.java",
                   "class FooTest {}\n")
        code, text, _ = self.ds(java=True)
        self.assertEqual(code, 0)
        self.assertIn("com.example.demo.Bar,com.example.demo.Foo", text)
        self.assertNotIn("FooTest", text.split("Java 类")[1])
        code, js, _ = self.ds(java=True, as_json=True)
        payload = json.loads(js)
        self.assertEqual(payload["java_classes_line"],
                         "com.example.demo.Bar,com.example.demo.Foo")


if __name__ == "__main__":
    unittest.main()
