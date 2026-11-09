# -*- coding: utf-8 -*-
"""confluence.py 的 unittest 测试套件。

用 mock_server 的模拟 Confluence（带 context path）真实跑 CLI：
回归（check/page/search/children/递归导出/认证）、find、排除清单、
AI 评论分组、引用标识、stale、gbk 控制台、Python 3.8 语法与源码只读自检。
运行：python -m unittest discover -s tests -v
"""
import ast
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "scripts", "confluence.py"))
sys.path.insert(0, HERE)

import mock_server  # noqa: E402

_spec = importlib.util.spec_from_file_location("confluence", SCRIPT)
conf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conf)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = mock_server.MockConfluence().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.reset_log()
        # 清掉可能干扰的环境变量，tearDown 时恢复
        self._env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("CONFLUENCE_")}
        self.tmp = tempfile.mkdtemp(prefix="conf_test_")
        self.outdir = os.path.join(self.tmp, "out")

    def tearDown(self):
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, extra="", **kw):
        # 写一份指向模拟服务器的 config.ini；kw 可覆盖 username/password/token/cookie
        lines = ["[confluence]",
                 "base_url = " + self.server.base_url,
                 "username = " + kw.get("username", "alice"),
                 "password = " + kw.get("password", "pw1"),
                 "output_dir = " + self.outdir,
                 "workers = 4"]
        if "token" in kw:
            lines.append("token = " + kw["token"])
        if "cookie" in kw:
            lines.append("cookie = " + kw["cookie"])
            lines.append("auth_mode = cookie")
        if extra:
            lines.append(extra)
        path = os.path.join(self.tmp, "config.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def run_cli(self, cfg, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = conf.main(["--config", cfg] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def read_page_md(self, pid, suffix=""):
        path = os.path.join(self.outdir, pid + suffix, "page.md")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def run_find(self, cfg, *argv):
        code, out, err = self.run_cli(cfg, "find", *argv)
        data = None
        jp = os.path.join(self.outdir, "candidates.json")
        if os.path.exists(jp):
            with open(jp, encoding="utf-8") as f:
                data = json.load(f)
        return code, out, err, data


class TestRegression(Base):
    def test_check(self):
        code, out, _ = self.run_cli(self.write_config(), "check")
        self.assertEqual(code, 0)
        self.assertIn("登录成功", out)
        self.assertIn("Mock Confluence", out)
        self.assertIn("开发空间", out)

    def test_page_full(self):
        cfg = self.write_config()
        code, out, _ = self.run_cli(cfg, "page", "1001")
        self.assertEqual(code, 0)
        md = self.read_page_md("1001")
        self.assertIn("# 支付接口规范", md)
        self.assertIn("支付接口用于订单支付", md)
        self.assertIn("## 附件（2）", md)
        self.assertTrue(os.path.exists(os.path.join(self.outdir, "1001", "att", "notes.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.outdir, "1001", "att", "pic.png")))
        txt_dir = os.path.join(self.outdir, "1001", "att_text")
        self.assertTrue(any(n.endswith(".md") for n in os.listdir(txt_dir)))
        self.assertIn("[完成] 页面已保存", out)

    def test_page_attachments_none_downloads_nothing(self):
        code, _, _ = self.run_cli(self.write_config(), "page", "1001", "--attachments", "none")
        self.assertEqual(code, 0)
        self.assertEqual(self.server.count("/download/"), 0)

    def test_page_historical_version(self):
        code, _, _ = self.run_cli(self.write_config(), "page", "1012", "--page-version", "1")
        self.assertEqual(code, 0)
        md = self.read_page_md("1012", "_v1")
        self.assertIn("第一版内容", md)
        with open(os.path.join(self.outdir, "1012_v1", "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["version"], 1)

    def test_search(self):
        cfg = self.write_config()
        code, out, _ = self.run_cli(cfg, "search", "支付")
        self.assertEqual(code, 0)
        self.assertIn("CQL:", out)
        self.assertIn("支付接口规范", out)
        code, out, _ = self.run_cli(cfg, "search", "支付", "--space", "DEV")
        self.assertEqual(code, 0)
        self.assertIn("支付接口规范", out)
        self.assertNotIn("运维手册", out)

    def test_children(self):
        code, out, _ = self.run_cli(self.write_config(), "children", "1008", "--depth", "2")
        self.assertEqual(code, 0)
        for t in ("根页面甲", "子页面乙", "孙页面丙"):
            self.assertIn(t, out)

    def test_recursive_export(self):
        code, out, _ = self.run_cli(self.write_config(), "page", "1008", "--recursive", "--depth", "2")
        self.assertEqual(code, 0)
        self.assertIn("共导出 3 个页面", out)
        for pid in ("1008", "1009", "1010"):
            self.assertTrue(os.path.exists(os.path.join(self.outdir, pid, "page.md")))


class TestAuth(Base):
    def test_wrong_password_stops_after_one_request(self):
        cfg = self.write_config(password="wrong-pw")
        code, _, err = self.run_cli(cfg, "page", "1001")
        self.assertEqual(code, 3)
        self.assertIn("认证错误", err)
        self.assertEqual(len(self.server.log), 1)
        self.assertEqual(self.server.count("/rest/api/content/1001"), 1)

    def test_bad_cookie_gets_login_page(self):
        cfg = self.write_config(cookie="bad-session")
        code, _, err = self.run_cli(cfg, "page", "1001")
        self.assertEqual(code, 3)
        self.assertIn("认证错误", err)

    def test_bearer_token(self):
        code, out, _ = self.run_cli(self.write_config(token="tok-123"), "check")
        self.assertEqual(code, 0)
        self.assertIn("登录成功", out)

    def test_form_login_fallback_when_basic_disabled(self):
        cfg = self.write_config(username="carol", password="pw4")
        code, _, _ = self.run_cli(cfg, "page", "1001")
        self.assertEqual(code, 0)
        self.assertGreaterEqual(self.server.count("/dologin.action"), 1)
        self.assertTrue(os.path.exists(os.path.join(self.outdir, "1001", "page.md")))


class TestFind(Base):
    def test_merge_dedupe_and_three_level_sort(self):
        code, _, _, data = self.run_find(self.write_config(), "--term", "苹果", "--term", "香蕉")
        self.assertEqual(code, 0)
        ids = [c["id"] for c in data["candidates"]]
        # 命中词数(2>1) → 标题命中(2001>2002；2003/2004/2005) → 最近修改(2003>2004>2005)
        self.assertEqual(ids, ["2001", "2002", "2003", "2004", "2005"])
        self.assertEqual(len(ids), len(set(ids)))  # 按 ID 去重
        by_id = {c["id"]: c for c in data["candidates"]}
        self.assertEqual(sorted(by_id["2001"]["matched_terms"]), ["苹果", "香蕉"])
        self.assertTrue(by_id["2001"]["title_hit"])
        self.assertFalse(by_id["2002"]["title_hit"])
        self.assertEqual(data["count"], 5)

    def test_candidates_files_content(self):
        code, out, _, data = self.run_find(self.write_config(), "--term", "渠道")
        self.assertEqual(code, 0)
        self.assertEqual([c["id"] for c in data["candidates"]], ["1002"])
        c0 = data["candidates"][0]
        self.assertEqual(c0["title"], "支付渠道接入")
        self.assertEqual(c0["space_key"], "DEV")
        self.assertEqual(c0["version"], 1)
        self.assertEqual(c0["last_modified"], "2026-11-06 09:00")
        self.assertEqual(c0["matched_terms"], ["渠道"])
        self.assertEqual(c0["breadcrumb"], "支付接口规范")
        self.assertIn("pageId=1002", c0["url"])
        with open(os.path.join(self.outdir, "candidates.md"), encoding="utf-8") as f:
            md = f.read()
        for col in ("页面ID", "标题", "空间", "版本", "最后修改", "命中词", "面包屑", "URL"):
            self.assertIn(col, md)
        self.assertIn("支付渠道接入", md)
        self.assertIn("候选表已写入", out)

    def test_since(self):
        _, _, _, data = self.run_find(self.write_config(), "--term", "苹果", "--term", "香蕉",
                                      "--since", "2026-01-01")
        self.assertEqual([c["id"] for c in data["candidates"]], ["2001", "2002", "2003", "2004"])

    def test_space_single_and_multiple(self):
        cfg = self.write_config()
        _, _, _, data = self.run_find(cfg, "--term", "支付", "--space", "OPS")
        self.assertEqual([c["id"] for c in data["candidates"]], ["1005"])
        _, _, _, data = self.run_find(cfg, "--term", "支付", "--space", "DEV", "--space", "OPS")
        ids = {c["id"] for c in data["candidates"]}
        self.assertNotIn("1006", ids)
        self.assertIn("1005", ids)

    def test_limit(self):
        _, _, _, data = self.run_find(self.write_config(), "--term", "苹果", "--term", "香蕉",
                                      "--limit", "2")
        self.assertEqual([c["id"] for c in data["candidates"]], ["2001", "2002"])
        self.assertEqual(data["count"], 5)
        self.assertEqual(data["shown"], 2)

    def test_terms_file(self):
        tf = os.path.join(self.tmp, "terms.txt")
        with open(tf, "w", encoding="utf-8") as f:
            f.write("苹果\n香蕉\n")
        _, _, _, data = self.run_find(self.write_config(), "--terms-file", tf)
        self.assertEqual([c["id"] for c in data["candidates"]],
                         ["2001", "2002", "2003", "2004", "2005"])

    def test_type_blogpost(self):
        _, _, _, data = self.run_find(self.write_config(), "--term", "支付", "--type", "blogpost")
        self.assertEqual([c["id"] for c in data["candidates"]], ["1014"])


class TestDeny(Base):
    def test_deny_space_in_find(self):
        cfg = self.write_config(extra="deny_spaces = SECRET")
        code, out, _, data = self.run_find(cfg, "--term", "支付")
        self.assertEqual(code, 0)
        ids = {c["id"] for c in data["candidates"]}
        self.assertNotIn("1006", ids)
        self.assertIn("1001", ids)
        self.assertEqual(data["denied"], {"total": 1, "by_reason": {"space": 1}})
        self.assertIn("已排除 1 条", out)
        with open(os.path.join(self.outdir, "candidates.md"), encoding="utf-8") as f:
            self.assertIn("已排除 1 条（空间 1）", f.read())

    def test_deny_page_id_in_search(self):
        cfg = self.write_config(extra="deny_page_ids = 1007")
        code, out, _ = self.run_cli(cfg, "search", "支付")
        self.assertEqual(code, 0)
        self.assertNotIn("被排除的页面", out)
        self.assertIn("已按排除清单过滤 1 条", out)

    def test_deny_page_id_page_exit5_no_download(self):
        cfg = self.write_config(extra="deny_page_ids = 1001")
        code, _, err = self.run_cli(cfg, "page", "1001")
        self.assertEqual(code, 5)
        self.assertIn("排除清单", err)
        self.assertFalse(os.path.exists(os.path.join(self.outdir, "1001")))
        self.assertEqual(self.server.count("/download/"), 0)

    def test_deny_title_keyword_page_exit5(self):
        cfg = self.write_config(extra="deny_title_keywords = 草稿")
        code, _, err = self.run_cli(cfg, "page", "1004")
        self.assertEqual(code, 5)
        self.assertIn("草稿", err)

    def test_deny_ancestor_subtree_in_find(self):
        cfg = self.write_config(extra="deny_ancestor_ids = 1008")
        code, out, _, data = self.run_find(cfg, "--term", "祖先词")
        self.assertEqual(code, 0)
        self.assertEqual(data["candidates"], [])
        self.assertEqual(data["denied"]["total"], 3)
        self.assertEqual(data["denied"]["by_reason"], {"ancestor": 3})
        self.assertIn("已排除 3 条", out)

    def test_deny_ancestor_children_root_exit5(self):
        cfg = self.write_config(extra="deny_ancestor_ids = 1008")
        code, _, err = self.run_cli(cfg, "children", "1008")
        self.assertEqual(code, 5)
        self.assertIn("排除清单", err)

    def test_deny_child_filtered_in_children(self):
        cfg = self.write_config(extra="deny_page_ids = 1009")
        code, out, _ = self.run_cli(cfg, "children", "1008", "--depth", "2")
        self.assertEqual(code, 0)
        self.assertIn("根页面甲", out)
        self.assertNotIn("子页面乙", out)
        self.assertNotIn("孙页面丙", out)
        self.assertIn("已按排除清单过滤 1 个子页面", out)

    def test_deny_in_recursive_export_skips(self):
        cfg = self.write_config(extra="deny_page_ids = 1009")
        code, out, err = self.run_cli(cfg, "page", "1008", "--recursive", "--depth", "2")
        self.assertEqual(code, 0)
        self.assertIn("共导出 1 个页面", out)
        self.assertIn("已跳过", err)
        self.assertFalse(os.path.exists(os.path.join(self.outdir, "1009")))

    def test_deny_file(self):
        df = os.path.join(self.tmp, "deny.txt")
        with open(df, "w", encoding="utf-8") as f:
            f.write("# 追加排除\nid:1007\nspace:SECRET\n")
        cfg = self.write_config()
        code, _, err = self.run_cli(cfg, "page", "1007", "--deny-file", df)
        self.assertEqual(code, 5)
        self.assertIn("排除清单", err)
        _, _, _, data = self.run_find(cfg, "--term", "支付", "--deny-file", df)
        ids = {c["id"] for c in data["candidates"]}
        self.assertNotIn("1007", ids)
        self.assertNotIn("1006", ids)
        self.assertEqual(data["denied"]["total"], 2)
        self.assertEqual(data["denied"]["by_reason"], {"page_id": 1, "space": 1})


class TestAiComments(Base):
    def test_ai_comments_grouped_separately(self):
        code, _, _ = self.run_cli(self.write_config(), "page", "1011")
        self.assertEqual(code, 0)
        md = self.read_page_md("1011")
        self.assertIn("## 评论（2）", md)
        self.assertIn("## AI 自动核对评论（不得作为证据）", md)
        human, _, ai = md.partition("## AI 自动核对评论（不得作为证据）")
        # 人工评论与人工回复留在评论区
        self.assertIn("人工评论：请确认费率字段", human)
        self.assertIn("人工回复：费率以代码为准", human)
        # AI 评论本体、同账号回复链（含隔代回复）进 AI 区
        self.assertIn("费率字段与代码不一致", ai)
        self.assertIn("补充：第二处也对过了", ai)
        self.assertIn("收到，我再复核一遍", ai)
        # AI 内容不得混进人工评论区
        self.assertNotIn("费率字段与代码不一致", human)
        self.assertNotIn("补充：第二处也对过了", human)

    def test_include_ai_comments_merges(self):
        code, _, _ = self.run_cli(self.write_config(), "page", "1011", "--include-ai-comments")
        self.assertEqual(code, 0)
        md = self.read_page_md("1011")
        self.assertIn("## 评论（5）", md)
        self.assertNotIn("AI 自动核对评论（不得作为证据）", md)
        self.assertIn("费率字段与代码不一致", md)

    def test_no_comments(self):
        code, _, _ = self.run_cli(self.write_config(), "page", "1011", "--no-comments")
        self.assertEqual(code, 0)
        md = self.read_page_md("1011")
        self.assertNotIn("## 评论", md)
        self.assertNotIn("人工评论", md)


class TestCitation(Base):
    def test_citation_line_and_meta(self):
        code, _, _ = self.run_cli(self.write_config(), "page", "1001")
        self.assertEqual(code, 0)
        md = self.read_page_md("1001")
        url = self.server.base_url + "/pages/viewpage.action?pageId=1001"
        line = "引用：Confluence 页面 1001 版本 3（最后修改 2026-11-05 10:30）" + url
        self.assertIn(line, md)
        with open(os.path.join(self.outdir, "1001", "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["id"], "1001")
        self.assertEqual(meta["version"], 3)
        self.assertEqual(meta["last_modified"], "2026-11-05 10:30")
        self.assertEqual(meta["url"], url)
        self.assertEqual(meta["citation"], line)


class TestStale(Base):
    def test_stale_three_outcomes(self):
        refs = os.path.join(self.tmp, "refs.txt")
        with open(refs, "w", encoding="utf-8") as f:
            f.write("# 知识库引用清单\n1012 1\n1012 2\n1001 3\n1013 1\n无法解析的行\n")
        code, out, err = self.run_cli(self.write_config(), "stale", "--file", refs)
        self.assertEqual(code, 0)
        self.assertIn("已更新（记录 1 → 当前 2）", out)
        self.assertIn("未变", out)
        self.assertIn("已删除或无权限", out)
        self.assertIn("共 4 条：已更新 1，未变 2，已删除或无权限 1", out)
        self.assertIn("忽略无法解析的行", err)

    def test_stale_error_classification(self):
        # S3：只有 404/403 归「已删除或无权限」；500 等其他错误单列「查询失败」
        refs = os.path.join(self.tmp, "refs.txt")
        with open(refs, "w", encoding="utf-8") as f:
            f.write("1015 1\n1016 1\n1013 1\n1001 3\n")
        code, out, _ = self.run_cli(self.write_config(), "stale", "--file", refs)
        self.assertEqual(code, 0)
        # 输出行层面：500 的行是「查询失败」，403 与 404 的行是「已删除或无权限」
        lines = {ln.split("|")[1].strip(): ln for ln in out.splitlines() if ln.startswith("| 10")}
        self.assertIn("查询失败", lines["1015"])
        self.assertIn("已删除或无权限", lines["1016"])
        self.assertIn("已删除或无权限", lines["1013"])
        self.assertIn("未变", lines["1001"])
        self.assertIn("共 4 条：已更新 0，未变 1，已删除或无权限 2，查询失败 1", out)


class TestMeta(Base):
    def test_gbk_console(self):
        cfg = self.write_config(extra="console_encoding = gbk")
        p = subprocess.run([sys.executable, SCRIPT, "--config", cfg, "check"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0)
        self.assertIn("登录成功", p.stdout.decode("gbk", "replace"))

    def test_python38_syntax(self):
        for path in (SCRIPT, os.path.join(HERE, "mock_server.py"), os.path.abspath(__file__)):
            with open(path, encoding="utf-8") as f:
                ast.parse(f.read(), feature_version=(3, 8))

    def test_source_has_no_write_requests_except_form_login(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count('method="POST"'), 1)
        self.assertNotIn('method="PUT"', src)
        self.assertNotIn('method="DELETE"', src)
        i = src.index('method="POST"')
        self.assertIn("/dologin.action", src[max(0, i - 600):i])

    def test_source_all_content_commands_pass_deny_check(self):
        # 接线检查（M4）：会读页面内容或附件的子命令入口，直接或经
        # PageExporter.export / export_tree，必须能到达 DenyFilter 的 check / check_page。
        # 「到达」的判定要精确：某个函数里存在对「DenyFilter 实例」的 check / check_page
        # 调用（形如 deny.check*(...) 或 self.deny.check_page(...)）才算该函数设了关卡，
        # 再沿调用图看每个子命令能否到达设了关卡的函数；删掉任何一处的接线这里都会失败
        with open(SCRIPT, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        calls = {}   # 函数名 -> 它调用到的名字集合（调用图遍历用）
        gated = set()  # 内部设有排除关卡的函数名

        def func_has_gate(node):
            deny_vars = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call) \
                        and isinstance(sub.value.func, ast.Name) and sub.value.func.id == "DenyFilter":
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            deny_vars.add(t.id)
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
                    continue
                if sub.func.attr not in ("check", "check_page"):
                    continue
                recv = sub.func.value
                if isinstance(recv, ast.Name) and recv.id in deny_vars:
                    return True
                if isinstance(recv, ast.Attribute) and recv.attr == "deny":
                    return True
            return False

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        if isinstance(fn, ast.Attribute):
                            names.add(fn.attr)
                        elif isinstance(fn, ast.Name):
                            names.add(fn.id)
                calls.setdefault(node.name, set()).update(names)
                if func_has_gate(node):
                    gated.add(node.name)

        def reaches(start):
            seen, stack = set(), [start]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(calls.get(cur, ()))
            return seen

        for fn in ("cmd_page", "cmd_children", "cmd_search", "cmd_find",
                   "cmd_space", "cmd_attachments", "cmd_download"):
            self.assertTrue(gated & reaches(fn), fn + " 未接入排除清单检查")
        # page 的单页与递归导出都经 PageExporter.export，那里本身必须设有 check_page 关卡
        self.assertIn("export", gated)


class TestAttachmentsDownloadDeny(Base):
    """M4：attachments 与 download 不得绕过排除清单。"""

    ATT_URL = "/download/attachments/1001/notes.txt"

    def att_url(self):
        return self.server.base_url + self.ATT_URL

    def test_attachments_denied_page_exit5(self):
        cfg = self.write_config(extra="deny_page_ids = 1001")
        code, out, err = self.run_cli(cfg, "attachments", "1001")
        self.assertEqual(code, 5)
        self.assertIn("排除清单", err)
        self.assertNotIn("notes.txt", out)
        # 拒绝发生在列附件之前：服务器没有收到附件列表请求
        self.assertEqual(self.server.count("/child/attachment"), 0)

    def test_attachments_deny_file_exit5(self):
        df = os.path.join(self.tmp, "deny.txt")
        with open(df, "w", encoding="utf-8") as f:
            f.write("id:1001\n")
        code, _, err = self.run_cli(self.write_config(), "attachments", "1001", "--deny-file", df)
        self.assertEqual(code, 5)
        self.assertIn("排除清单", err)

    def test_attachments_allowed_lists_files(self):
        code, out, _ = self.run_cli(self.write_config(), "attachments", "1001")
        self.assertEqual(code, 0)
        self.assertIn("notes.txt", out)
        self.assertIn("pic.png", out)

    def test_download_denied_page_attachment_rejected(self):
        cfg = self.write_config(extra="deny_page_ids = 1001")
        code, _, err = self.run_cli(cfg, "download", self.att_url())
        self.assertEqual(code, 5)
        self.assertIn("已拒绝", err)
        self.assertIn("排除清单", err)
        # 一个字节都没下：服务器没收到下载请求，本地也没有文件
        self.assertEqual(self.server.count("/download/attachments/1001"), 0)
        self.assertFalse(os.path.exists(os.path.join(self.outdir, "downloads", "notes.txt")))

    def test_download_allowed_when_other_page_denied(self):
        cfg = self.write_config(extra="deny_page_ids = 1007")
        code, out, _ = self.run_cli(cfg, "download", self.att_url())
        self.assertEqual(code, 0)
        self.assertIn("已下载", out)
        self.assertTrue(os.path.exists(os.path.join(self.outdir, "downloads", "notes.txt")))

    def test_download_unparseable_url_rejected_when_deny_active(self):
        # 排除清单生效时，解析不出页面 ID 的下载地址无法确认归属，拒绝下载
        cfg = self.write_config(extra="deny_page_ids = 1007")
        url = self.server.base_url + "/download/resources/logo.png"
        code, _, err = self.run_cli(cfg, "download", url)
        self.assertEqual(code, 5)
        self.assertIn("已拒绝", err)
        self.assertIn("解析不出页面 ID", err)
        self.assertEqual(self.server.count("/download/resources"), 0)

    def test_download_unparseable_url_unchanged_when_deny_inactive(self):
        # 排除清单未启用时 download 行为与原来一致：照常发请求（模拟服务器回 404）
        url = self.server.base_url + "/download/resources/logo.png"
        code, _, err = self.run_cli(self.write_config(), "download", url)
        self.assertEqual(code, 5)
        self.assertIn("请求错误", err)
        self.assertNotIn("已拒绝", err)
        self.assertEqual(self.server.count("/download/resources"), 1)


class TestTreeDeny(Base):
    """S2：children / space --tree 沿树传递祖先链，被排除节点的整棵子树不列、只计数。"""

    def test_children_denied_middle_node_prunes_subtree(self):
        cfg = self.write_config(extra="deny_page_ids = 3002")
        code, out, _ = self.run_cli(cfg, "children", "3001", "--depth", "2")
        self.assertEqual(code, 0)
        self.assertIn("树根页面", out)
        self.assertNotIn("树枝甲", out)
        self.assertNotIn("树叶甲", out)  # 被排除节点的子孙也不出现
        self.assertIn("树枝乙", out)     # 未命中分支正常列出
        self.assertIn("树叶乙", out)
        self.assertIn("已按排除清单过滤 1 个子页面", out)

    def test_children_denied_ancestor_prunes_subtree(self):
        cfg = self.write_config(extra="deny_ancestor_ids = 3002")
        code, out, _ = self.run_cli(cfg, "children", "3001", "--depth", "2")
        self.assertEqual(code, 0)
        self.assertNotIn("树枝甲", out)
        self.assertNotIn("树叶甲", out)
        self.assertIn("树枝乙", out)
        self.assertIn("树叶乙", out)
        self.assertIn("已按排除清单过滤 1 个子页面", out)

    def test_children_no_deny_lists_whole_tree(self):
        code, out, _ = self.run_cli(self.write_config(), "children", "3001", "--depth", "2")
        self.assertEqual(code, 0)
        for t in ("树根页面", "树枝甲", "树叶甲", "树枝乙", "树叶乙"):
            self.assertIn(t, out)

    def test_space_tree_denied_subtree_filtered(self):
        cfg = self.write_config(extra="deny_page_ids = 3002")
        code, out, _ = self.run_cli(cfg, "space", "DEV", "--tree", "--depth", "3")
        self.assertEqual(code, 0)
        self.assertIn("树根页面", out)
        self.assertNotIn("树枝甲", out)
        self.assertNotIn("树叶甲", out)
        self.assertIn("树枝乙", out)
        self.assertIn("树叶乙", out)
        self.assertIn("已按排除清单过滤 1 个页面", out)

    def test_space_tree_denied_root_filtered(self):
        cfg = self.write_config(extra="deny_page_ids = 3001")
        code, out, _ = self.run_cli(cfg, "space", "DEV", "--tree", "--depth", "3")
        self.assertEqual(code, 0)
        for t in ("树根页面", "树枝甲", "树叶甲", "树枝乙", "树叶乙"):
            self.assertNotIn(t, out)
        self.assertIn("已按排除清单过滤 1 个页面", out)

    def test_space_tree_no_deny_lists_whole_tree(self):
        code, out, _ = self.run_cli(self.write_config(), "space", "DEV", "--tree", "--depth", "3")
        self.assertEqual(code, 0)
        for t in ("树根页面", "树枝甲", "树叶甲", "树枝乙", "树叶乙"):
            self.assertIn(t, out)
        self.assertNotIn("已按排除清单过滤", out)


class TestChinese(Base):
    """S4：标题、正文、检索词含中文时的读取与检索。"""

    def test_chinese_page_read(self):
        code, _, _ = self.run_cli(self.write_config(), "page", "4001")
        self.assertEqual(code, 0)
        md = self.read_page_md("4001")
        self.assertIn("# 中文标题页面", md)
        self.assertIn("中文正文独特短语", md)

    def test_chinese_search_hits_body_and_title(self):
        cfg = self.write_config()
        code, out, _ = self.run_cli(cfg, "search", "中文正文独特短语")
        self.assertEqual(code, 0)
        self.assertIn("中文标题页面", out)
        code, out, _ = self.run_cli(cfg, "search", "中文标题", "--title")
        self.assertEqual(code, 0)
        self.assertIn("中文标题页面", out)

    def test_chinese_find(self):
        cfg = self.write_config()
        _, _, _, data = self.run_find(cfg, "--term", "中文正文独特短语")
        self.assertEqual([c["id"] for c in data["candidates"]], ["4001"])
        _, _, _, data = self.run_find(cfg, "--term", "中文标题")
        self.assertEqual([c["id"] for c in data["candidates"]], ["4001"])
        self.assertTrue(data["candidates"][0]["title_hit"])


if __name__ == "__main__":
    unittest.main()
