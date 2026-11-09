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


if __name__ == "__main__":
    unittest.main()
