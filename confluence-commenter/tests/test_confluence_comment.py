#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""confluence_comment.py 的 unittest 套件（配合 fake_confluence 模拟服务器）。

运行：在 confluence-commenter/ 目录下执行  python -m unittest discover -s tests -v
"""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(TESTS_DIR)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "confluence_comment.py")
sys.path.insert(0, TESTS_DIR)

from fake_confluence import FakeConfluenceServer  # noqa: E402

GOOD_BODY = "文档说提交后立即生效，代码里实际是次日零点生效，两处口径不一致。"
GOOD_BODY_B = "文档说审批通过后自动归档，代码里需要手工触发归档，这一步不一致。"


class CliTestCase(unittest.TestCase):
    """每个用例：一个全新模拟服务器 + 一个临时目录（配置与日志互相隔离）。"""

    def setUp(self):
        self.server = FakeConfluenceServer().start()
        self.addCleanup(self.server.stop)
        self.tmp = tempfile.mkdtemp(prefix="commenter-test-")
        self.log_dir = os.path.join(self.tmp, "logs")

    # ---------- 工具方法 ----------
    def make_config(self, base_url=None, token="test-token", username="", password="",
                    extra="", limits="", deny="", encoding="utf-8"):
        lines = [
            "[confluence]",
            "base_url = {0}".format(base_url if base_url is not None else self.server.base_url),
            "token = {0}".format(token),
            "username = {0}".format(username),
            "password = {0}".format(password),
            "console_encoding = {0}".format(encoding),
            "[limits]",
            limits,
            "[deny]",
            deny,
            "[paths]",
            "log_dir = {0}".format(self.log_dir),
            extra,
        ]
        path = os.path.join(self.tmp, "config.ini")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def run_cli(self, cli_args, config_path=None, env_extra=None):
        if config_path is None:
            config_path = self.make_config()
        env = dict(os.environ)
        for key in ("CONFLUENCE_W_BASE_URL", "CONFLUENCE_W_TOKEN", "CONFLUENCE_W_USERNAME",
                    "CONFLUENCE_W_PASSWORD", "CONFLUENCE_W_CONFIG"):
            env.pop(key, None)
        env["CONFLUENCE_W_RUN_ID"] = "run-" + self.id().replace(".", "-")
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--config", config_path] + cli_args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        return proc

    @staticmethod
    def out(proc, encoding="utf-8"):
        return proc.stdout.decode(encoding, errors="replace")

    @staticmethod
    def combined(proc, encoding="utf-8"):
        return (proc.stdout + proc.stderr).decode(encoding, errors="replace")

    def read_log(self):
        path = os.path.join(self.log_dir, "comments.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def send_ok(self, page="1001", body=GOOD_BODY, extra_args=None):
        args = ["send", page, "--verdict", "wrong", "--text", body, "--yes"]
        if extra_args:
            args += extra_args
        proc = self.run_cli(args)
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        return proc


class CheckTests(CliTestCase):
    def test_check_success_bearer(self):
        proc = self.run_cli(["check"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertIn("测试用户", self.out(proc))

    def test_check_success_basic(self):
        cfg = self.make_config(token="", username="tester", password="test-pass")
        proc = self.run_cli(["check"], config_path=cfg)
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertIn("测试用户", self.out(proc))

    def test_auth_failure_single_request_exit3(self):
        cfg = self.make_config(token="wrong-token")
        proc = self.run_cli(["check"], config_path=cfg)
        self.assertEqual(proc.returncode, 3, self.combined(proc))
        self.assertEqual(self.server.request_count(), 1)  # 认证失败绝不重试

    def test_login_page_is_auth_failure(self):
        self.server.state["force_login_page"] = True
        proc = self.run_cli(["check"])
        self.assertEqual(proc.returncode, 3, self.combined(proc))
        self.assertEqual(self.server.request_count(), 1)

    def test_env_var_overrides_config_token(self):
        cfg = self.make_config(token="wrong-token")
        proc = self.run_cli(["check"], config_path=cfg,
                            env_extra={"CONFLUENCE_W_TOKEN": "test-token"})
        self.assertEqual(proc.returncode, 0, self.combined(proc))

    def test_network_error_exit4(self):
        cfg = self.make_config(base_url="http://127.0.0.1:1")
        proc = self.run_cli(["check"], config_path=cfg)
        self.assertEqual(proc.returncode, 4, self.combined(proc))


class PreviewSendTests(CliTestCase):
    def test_preview_sends_no_post(self):
        proc = self.run_cli(["preview", "1001", "--verdict", "wrong", "--text", GOOD_BODY])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        text = self.out(proc)
        self.assertIn("订单流程说明", text)
        self.assertIn("【AI 核对】", text)
        self.assertIn("如有不同意见请直接回复本评论", text)
        self.assertEqual(self.server.post_count(), 0)

    def test_preview_accepts_page_url(self):
        url = "http://conf.example.com/confluence/pages/1001/订单流程说明"
        proc = self.run_cli(["preview", url, "--verdict", "partial", "--text", GOOD_BODY])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertIn("订单流程说明", self.out(proc))
        self.assertEqual(self.server.post_count(), 0)

    def test_send_without_yes_rejected_no_post(self):
        proc = self.run_cli(["send", "1001", "--verdict", "wrong", "--text", GOOD_BODY])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("preview", self.combined(proc))
        self.assertEqual(self.server.post_count(), 0)

    def test_send_with_yes_exactly_one_post_and_structure(self):
        body = "文档写的是甲流程，代码里实际走乙流程<b>不一致</b>。"
        proc = self.run_cli(["send", "1001", "--verdict", "wrong", "--text", body,
                             "--code-ref", "feature/demo@abc123", "--yes"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(self.server.post_count(), 1)
        payload = self.server.state["posts"][0]
        self.assertEqual(payload["type"], "comment")
        self.assertEqual(payload["container"], {"id": "1001", "type": "page"})
        storage = payload["body"]["storage"]
        self.assertEqual(storage["representation"], "storage")
        value = storage["value"]
        self.assertIn("【AI 核对】", value)
        self.assertIn("（以上由 AI 依据当前代码核对生成，代码版本：feature/demo@abc123；"
                      "如有不同意见请直接回复本评论）", value)
        self.assertIn("&lt;b&gt;", value)       # 正文已 HTML 转义
        self.assertNotIn("<b>", value)
        # 日志写了一行，关键字段齐全
        records = self.read_log()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["page_id"], "1001")
        self.assertEqual(record["title"], "订单流程说明")
        self.assertEqual(record["space"], "DEV")
        self.assertEqual(record["page_version"], 3)
        self.assertEqual(record["comment_id"], "5001")
        self.assertEqual(record["verdict"], "wrong")
        self.assertEqual(record["code_ref"], "feature/demo@abc123")
        self.assertEqual(len(record["content_hash"]), 64)

    def test_body_too_long_rejected(self):
        proc = self.run_cli(["send", "1001", "--verdict", "wrong",
                             "--text", "不" * 301, "--yes"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("上限", self.combined(proc))
        self.assertEqual(self.server.post_count(), 0)

    def test_body_too_many_lines_rejected(self):
        body = "\n".join("第{0}行口径不一致".format(i) for i in range(6))
        proc = self.run_cli(["send", "1001", "--verdict", "wrong", "--text", body, "--yes"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.server.post_count(), 0)

    def test_code_block_rejected(self):
        body = "结论如下：\n```\nint a = 1;\n```"
        proc = self.run_cli(["send", "1001", "--verdict", "wrong", "--text", body, "--yes"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("代码块", self.combined(proc))
        self.assertEqual(self.server.post_count(), 0)

    def test_verdict_correct_rejected(self):
        proc = self.run_cli(["send", "1001", "--verdict", "correct",
                             "--text", GOOD_BODY, "--yes"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("correct", self.combined(proc))
        self.assertEqual(self.server.post_count(), 0)


class SensitiveScanTests(CliTestCase):
    # (规则名, 命中正文, 原始敏感串, 不误报正文)
    CASES = [
        ("IPv4 地址", "文中写的服务地址 192.168.1.10 与代码不一致", "192.168.1.10",
         "文中写的版本号 2.300.1.1 与代码不一致"),
        ("IPv6 地址", "文中写的地址 fe80::1 与代码不一致", "fe80::1",
         "文中写的比例 1:2:3 与代码不一致"),
        ("带主机名的 URL", "文中引用 http://wiki.example.com/page 的说明与代码不一致",
         "wiki.example.com", "文中引用用户手册第三节的说明与代码不一致"),
        ("邮箱", "文中写的联系人 zhangsan@example.com 与代码不一致", "zhangsan@example.com",
         "文中写的联系人张三与代码不一致"),
        ("手机号", "文中写的电话 13812345678 与代码不一致", "13812345678",
         "文中写的单号 12345678901 与代码不一致"),
        ("身份证号", "文中写的证件号 11010119900307753X 与代码不一致", "11010119900307753X",
         "文中写的编号 123456789012345 与代码不一致"),
        ("银行卡号", "文中写的卡号 6222021234567890 与代码不一致", "6222021234567890",
         "文中统计的共 123456789012345 条记录与代码不一致"),
        ("密钥赋值", "文中写的 password=abc123 与代码不一致", "abc123",
         "文中写的 token 刷新机制与代码不一致"),
        ("密钥赋值", "文中写的 token: xyz789 与代码不一致", "xyz789",
         "文中写的口令保管方式与代码不一致"),
        ("JDBC 连接串", "文中写的 jdbc:mysql://dbhost:3306/app 与代码不一致", "dbhost",
         "文中写的数据库连接方式与代码不一致"),
        ("私钥头", "文中贴了 -----BEGIN RSA PRIVATE KEY----- 与代码不一致", "PRIVATE KEY",
         "文中贴了 -----BEGIN PUBLIC KEY----- 与代码不一致"),
    ]

    def test_each_rule_hits_and_masks(self):
        for rule, hit_body, raw_secret, _safe in self.CASES:
            with self.subTest(rule=rule, body=hit_body):
                proc = self.run_cli(["preview", "1001", "--verdict", "wrong", "--text", hit_body])
                self.assertNotEqual(proc.returncode, 0, self.combined(proc))
                text = self.combined(proc)
                self.assertIn(rule, text)
                self.assertIn("*", text)               # 命中内容已打码
                self.assertNotIn(raw_secret, text)     # 原始敏感串不得原样出现
        self.assertEqual(self.server.post_count(), 0)

    def test_each_rule_no_false_positive(self):
        for rule, _hit, _raw, safe_body in self.CASES:
            with self.subTest(rule=rule, body=safe_body):
                proc = self.run_cli(["preview", "1001", "--verdict", "wrong", "--text", safe_body])
                self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(self.server.post_count(), 0)


class DenyTests(CliTestCase):
    def test_deny_space(self):
        cfg = self.make_config(deny="spaces = SECRET")
        proc = self.run_cli(["preview", "1002", "--verdict", "wrong", "--text", GOOD_BODY],
                            config_path=cfg)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("拒绝清单", self.combined(proc))
        self.assertEqual(self.server.post_count(), 0)

    def test_deny_page_id(self):
        cfg = self.make_config(deny="page_ids = 1001")
        proc = self.run_cli(["preview", "1001", "--verdict", "wrong", "--text", GOOD_BODY],
                            config_path=cfg)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("拒绝清单", self.combined(proc))
        self.assertEqual(self.server.post_count(), 0)

    def test_deny_title_keyword(self):
        cfg = self.make_config(deny="title_keywords = 机密")
        proc = self.run_cli(["preview", "1002", "--verdict", "wrong", "--text", GOOD_BODY],
                            config_path=cfg)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("机密", self.combined(proc))
        self.assertEqual(self.server.post_count(), 0)


class FollowupTests(CliTestCase):
    def test_idempotent_same_content_rejected(self):
        self.send_ok()
        proc = self.run_cli(["send", "1001", "--verdict", "wrong", "--text", GOOD_BODY, "--yes"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("已经发过", self.combined(proc))
        self.assertEqual(self.server.post_count(), 1)

    def test_unresolved_blocks_then_followup_replies(self):
        self.send_ok()
        # 同页已有未结评论：不同的新评论被拒
        proc = self.run_cli(["send", "1001", "--verdict", "partial",
                             "--text", GOOD_BODY_B, "--yes"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("未结", self.combined(proc))
        self.assertEqual(self.server.post_count(), 1)
        # 带 --followup：作为对上一条评论的回复发出，ancestors 指向父评论
        proc = self.run_cli(["send", "1001", "--verdict", "partial", "--text", GOOD_BODY_B,
                             "--followup", "--yes"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(self.server.post_count(), 2)
        payload = self.server.state["posts"][1]
        self.assertEqual(payload.get("ancestors"), [{"id": "5001"}])
        self.assertEqual(payload["container"], {"id": "1001", "type": "page"})


class StatusTests(CliTestCase):
    def test_status_four_states(self):
        for page_id in ("2001", "2002", "2003", "2004"):
            self.server.add_page(page_id, "状态测试页 " + page_id, version=1)
            self.send_ok(page=page_id)
        records = {r["page_id"]: r for r in self.read_log()}
        # 2002 有回复；2003 页面已更新；2004 评论已删除；2001 保持未回复
        self.server.add_reply(records["2002"]["comment_id"],
                              display_name="李四", text="已确认，按代码为准修改文档。")
        self.server.bump_page_version("2003")
        self.server.delete_comment(records["2004"]["comment_id"])
        proc = self.run_cli(["status"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        lines = self.out(proc).splitlines()

        def line_for(page_id):
            for line in lines:
                if "页面 {0}".format(page_id) in line:
                    return line
            return ""

        self.assertIn("未回复", line_for("2001"))
        self.assertIn("有回复", line_for("2002"))
        self.assertIn("页面已更新", line_for("2003"))
        self.assertIn("评论已删除", line_for("2004"))
        text = self.out(proc)
        self.assertIn("李四", text)                       # 回复人
        self.assertIn("已确认，按代码为准修改文档。", text)  # 回复内容前 100 字

    def test_log_command(self):
        self.send_ok()
        proc = self.run_cli(["log"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertIn("5001", self.out(proc))
        self.assertIn("订单流程说明", self.out(proc))


class LimitTests(CliTestCase):
    def test_run_limit(self):
        cfg = self.make_config(limits="max_sends_per_run = 2")
        env = {"CONFLUENCE_W_RUN_ID": "limit-run-fixed"}
        for page_id in ("3001", "3002", "3003"):
            self.server.add_page(page_id, "限额测试页 " + page_id)
        for page_id in ("3001", "3002"):
            proc = self.run_cli(["send", page_id, "--verdict", "wrong",
                                 "--text", GOOD_BODY, "--yes"],
                                config_path=cfg, env_extra=env)
            self.assertEqual(proc.returncode, 0, self.combined(proc))
        proc = self.run_cli(["send", "3003", "--verdict", "wrong",
                             "--text", GOOD_BODY, "--yes"],
                            config_path=cfg, env_extra=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("上限", self.combined(proc))
        self.assertEqual(self.server.post_count(), 2)


class ContextPathTests(unittest.TestCase):
    def test_context_path_base_url(self):
        server = FakeConfluenceServer(context_path="/confluence").start()
        self.addCleanup(server.stop)
        self.assertTrue(server.base_url.endswith("/confluence"))
        tmp = tempfile.mkdtemp(prefix="commenter-ctx-")
        config_path = os.path.join(tmp, "config.ini")
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join([
                "[confluence]",
                "base_url = " + server.base_url,
                "token = test-token",
                "[paths]",
                "log_dir = " + os.path.join(tmp, "logs"),
            ]) + "\n")
        env = dict(os.environ)
        env["CONFLUENCE_W_RUN_ID"] = "ctx-run"
        proc = subprocess.run([sys.executable, SCRIPT, "--config", config_path, "check"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn("测试用户", proc.stdout.decode("utf-8", "replace"))
        proc = subprocess.run([sys.executable, SCRIPT, "--config", config_path,
                               "send", "1001", "--verdict", "wrong",
                               "--text", GOOD_BODY, "--yes"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(server.post_count(), 1)


class EncodingTests(CliTestCase):
    def test_gbk_console_encoding(self):
        cfg = self.make_config(encoding="gbk")
        proc = self.run_cli(["preview", "1001", "--verdict", "wrong", "--text", GOOD_BODY],
                            config_path=cfg)
        self.assertEqual(proc.returncode, 0)
        text = proc.stdout.decode("gbk")
        self.assertIn("【AI 核对】", text)
        self.assertIn("订单流程说明", text)


class SourceHygieneTests(unittest.TestCase):
    def test_source_has_no_put_or_delete(self):
        with open(SCRIPT, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIsNone(re.search(r"\bPUT\b", source))
        self.assertIsNone(re.search(r"\bDELETE\b", source))
        # 实际用到的请求方法只有 GET 与 POST
        methods = set(re.findall(r'_request\("([A-Z]+)"', source))
        self.assertEqual(methods, {"GET", "POST"})

    def test_python38_syntax(self):
        with open(SCRIPT, "r", encoding="utf-8") as fh:
            source = fh.read()
        ast.parse(source, filename="confluence_comment.py", feature_version=(3, 8))


if __name__ == "__main__":
    unittest.main()
