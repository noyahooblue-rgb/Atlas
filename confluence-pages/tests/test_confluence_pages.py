#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""confluence_pages.py 的 unittest 套件（配合 fake_confluence 模拟服务器）。

运行：在 confluence-pages/ 目录下执行  python -m unittest discover -s tests -v
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(TESTS_DIR)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "confluence_pages.py")
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

from fake_confluence import FakeConfluenceServer  # noqa: E402
import confluence_pages as cp  # noqa: E402

GOOD_BODY = "# 支付回调说明\n\n这是第一版正文，说明回调的处理口径。\n"
NEW_BODY = "# 支付回调说明\n\n这是第二版正文，口径已经更新。\n"


class CliTestCase(unittest.TestCase):
    """每个用例：一个全新模拟服务器 + 一个临时目录（配置与本地状态互相隔离）。"""

    def setUp(self):
        self.server = FakeConfluenceServer().start()
        self.addCleanup(self.server.stop)
        self.tmp = tempfile.mkdtemp(prefix="pages-test-")
        self.log_dir = os.path.join(self.tmp, "logs")

    # ---------- 工具方法 ----------
    def make_config(self, base_url=None, token="test-token", username="", password="",
                    allow_spaces="AI", allow_parent_ids="", allow_url_hosts="",
                    limits="", deny="", encoding="utf-8"):
        lines = [
            "[confluence]",
            "base_url = {0}".format(base_url if base_url is not None else self.server.base_url),
            "token = {0}".format(token),
            "username = {0}".format(username),
            "password = {0}".format(password),
            "console_encoding = {0}".format(encoding),
            "[pages]",
            "allow_spaces = {0}".format(allow_spaces),
            "allow_parent_ids = {0}".format(allow_parent_ids),
            "allow_url_hosts = {0}".format(allow_url_hosts),
            "[limits]",
            limits,
            "[deny]",
            deny,
            "[paths]",
            "log_dir = {0}".format(self.log_dir),
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
        return subprocess.run(
            [sys.executable, SCRIPT, "--config", config_path] + cli_args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    @staticmethod
    def out(proc, encoding="utf-8"):
        return proc.stdout.decode(encoding, errors="replace")

    @staticmethod
    def combined(proc, encoding="utf-8"):
        return (proc.stdout + proc.stderr).decode(encoding, errors="replace")

    def write_body(self, text, name="body.md"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def read_registry(self):
        path = os.path.join(self.log_dir, "pages.jsonl")
        folded = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        record = json.loads(line)
                        folded[str(record["page_id"])] = record
        return folded

    def write_registry(self, records):
        os.makedirs(self.log_dir, exist_ok=True)
        path = os.path.join(self.log_dir, "pages.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_ops(self):
        path = os.path.join(self.log_dir, "ops.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def create_ok(self, parent="2001", title="回调说明页", body=GOOD_BODY, config_path=None):
        next_id = str(self.server.state["next_page_id"])
        path = self.write_body(body)
        proc = self.run_cli(
            ["create", "--parent", parent, "--title", title, "--file", path, "--yes"],
            config_path=config_path)
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        return next_id


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


class AllowSpacesTests(CliTestCase):
    """补充修订：allow_spaces 空间白名单是硬前置条件。"""

    def test_allow_spaces_empty_create_rejected(self):
        cfg = self.make_config(allow_spaces="")
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "新页面", "--file", path, "--yes"],
            config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("未配置 AI 专用空间", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_allow_spaces_empty_update_rejected(self):
        page_id = self.create_ok()
        cfg = self.make_config(allow_spaces="")
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", page_id, "--file", path, "--yes"], config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("未配置 AI 专用空间", self.combined(proc))
        self.assertEqual(self.server.put_count(), 0)

    def test_parent_in_other_space_rejected(self):
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "3001", "--title", "新页面", "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("不在 AI 专用空间", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_update_page_in_other_space_rejected(self):
        # 登记表里有记录、创建人也匹配，但页面在 DEV 空间：空间检查先于 5a–5e
        self.server.add_page(
            "8001", "研发空间旧页", space_key="DEV", version=1,
            labels=["ai-authored", "ai-pending"])
        self.write_registry([{
            "page_id": "8001", "title": "研发空间旧页", "space": "DEV",
            "parent_id": "3001", "created_at": "2026-11-09T09:00:00+08:00",
            "last_version": 1, "content_hash": "a" * 64,
        }])
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", "8001", "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("不在 AI 专用空间", self.combined(proc))
        self.assertEqual(self.server.put_count(), 0)

    def test_allow_parent_ids_unset_any_parent_in_space(self):
        # 没配 allow_parent_ids：AI 空间首页（2001）下可以直接新建
        page_id = self.create_ok(parent="2001")
        self.assertIn(page_id, self.server.state["pages"])

    def test_allow_parent_ids_set_requires_membership(self):
        cfg = self.make_config(allow_parent_ids="2002")
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "页面甲", "--file", path, "--yes"],
            config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("allow_parent_ids", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)
        # 在名单内的父页面下新建则通过（两个条件都要满足）
        page_id = self.create_ok(parent="2002", title="页面乙", config_path=cfg)
        self.assertIn(page_id, self.server.state["pages"])

    def test_allow_parent_ids_set_but_wrong_space_still_rejected(self):
        cfg = self.make_config(allow_parent_ids="3001")
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "3001", "--title", "页面丙", "--file", path, "--yes"],
            config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("不在 AI 专用空间", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)


class CreateTests(CliTestCase):
    def test_create_preview_no_writes(self):
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "回调说明页", "--file", path])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        text = self.out(proc)
        self.assertIn("AI 空间首页", text)      # 父页面标题
        self.assertIn("回调说明页", text)
        self.assertIn("ai-authored", text)
        self.assertEqual(self.server.write_count(), 0)   # 预览期间 0 次写请求
        self.assertEqual(self.read_registry(), {})

    def test_create_yes_one_create_post_banner_labels_registry(self):
        page_id = self.create_ok()
        self.assertEqual(self.server.create_post_count(), 1)  # 恰好 1 次新建请求
        page = self.server.page(page_id)
        storage = page["body"]["storage"]["value"]
        self.assertTrue(storage.startswith('<ac:structured-macro ac:name="info">'), storage)
        self.assertIn(cp.BANNER_TEXT, storage)
        self.assertIn("第一版正文", storage)
        self.assertEqual(sorted(self.server.labels_of(page_id)), ["ai-authored", "ai-pending"])
        record = self.read_registry()[page_id]
        self.assertEqual(record["title"], "回调说明页")
        self.assertEqual(record["space"], "AI")
        self.assertEqual(record["parent_id"], "2001")
        self.assertEqual(record["last_version"], 1)
        self.assertEqual(len(record["content_hash"]), 64)
        self.assertTrue(record["created_at"])
        ops = [r for r in self.read_ops() if r.get("op") == "create"]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["page_id"], page_id)

    def test_create_same_name_rejected(self):
        self.server.add_page("8101", "回调说明页", space_key="AI")
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "回调说明页", "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("同名页面", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_create_parent_not_exist_rejected(self):
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "9999", "--title", "新页面", "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("父页面不存在", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_create_parent_in_deny_page_ids_rejected(self):
        cfg = self.make_config(deny="page_ids = 2001")
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "新页面", "--file", path, "--yes"],
            config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("拒绝清单", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_create_parent_title_hits_deny_keyword_rejected(self):
        cfg = self.make_config(deny="title_keywords = 归档")
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "2002", "--title", "新页面", "--file", path, "--yes"],
            config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("拒绝关键词", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_create_new_title_hits_deny_keyword_rejected(self):
        cfg = self.make_config(deny="title_keywords = 结算")
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "内部结算说明", "--file", path, "--yes"],
            config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("拒绝关键词", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_create_body_too_large_rejected(self):
        cfg = self.make_config(limits="max_body_bytes = 50")
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "新页面", "--file", path, "--yes"],
            config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("正文太大", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_create_post_network_error_uncertain_exit6(self):
        self.server.state["drop_post_response"] = True
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "回调说明页", "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 6, self.combined(proc))
        self.assertEqual(self.server.create_post_count(), 1)  # 只请求 1 次，没重试
        uncertain = [r for r in self.read_ops() if r.get("status") == "uncertain"]
        self.assertEqual(len(uncertain), 1)
        self.assertEqual(uncertain[0]["op"], "create")

    def test_create_parent_accepts_url(self):
        path = self.write_body(GOOD_BODY)
        url = "http://conf.example.com/confluence/pages/2001/AI+空间首页"
        proc = self.run_cli(
            ["create", "--parent", url, "--title", "URL父页面页", "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(self.server.create_post_count(), 1)

    def test_create_full_chinese_chain(self):
        body = "# 中文标题页\n\n正文里有中文内容，说明中文处理口径。\n"
        # 预览里能看到中文父页面标题
        path = self.write_body(body)
        proc = self.run_cli(
            ["create", "--parent", "4001", "--title", "中文标题页", "--file", path])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertIn("中文父页面标题", self.out(proc))
        page_id = self.create_ok(parent="4001", title="中文标题页", body=body)
        record = self.read_registry()[page_id]
        self.assertEqual(record["title"], "中文标题页")
        self.assertIn("中文处理口径", self.server.page(page_id)["body"]["storage"]["value"])


class UpdateTests(CliTestCase):
    def _registry_record(self, page_id, space="AI", last_version=1, title="外部页面"):
        return {
            "page_id": page_id, "title": title, "space": space,
            "parent_id": "2001", "created_at": "2026-11-09T09:00:00+08:00",
            "last_version": last_version, "content_hash": "b" * 64,
        }

    def test_update_happy_path(self):
        page_id = self.create_ok()
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", page_id, "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        page = self.server.page(page_id)
        self.assertEqual(page["version"]["number"], 2)          # 版本号 +1
        self.assertEqual(page["title"], "回调说明页")            # 标题没动
        self.assertEqual(sorted(self.server.labels_of(page_id)),
                         ["ai-authored", "ai-pending"])          # 标签没动
        storage = page["body"]["storage"]["value"]
        self.assertIn(cp.BANNER_TEXT, storage)                   # 横幅保留
        self.assertIn("第二版正文", storage)
        self.assertNotIn("第一版正文", storage)
        self.assertEqual(self.read_registry()[page_id]["last_version"], 2)

    def test_update_preview_shows_diff_no_writes(self):
        page_id = self.create_ok()
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", page_id, "--file", path])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        text = self.out(proc)
        self.assertIn("-这是第一版正文，说明回调的处理口径。", text)
        self.assertIn("+这是第二版正文，口径已经更新。", text)
        self.assertEqual(self.server.put_count(), 0)
        self.assertEqual(self.server.page(page_id)["version"]["number"], 1)

    def test_update_rejects_not_in_registry_5a(self):
        self.server.add_page("8201", "外部页面", space_key="AI",
                             labels=["ai-authored", "ai-pending"])
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", "8201", "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("登记表", self.combined(proc))
        self.assertEqual(self.server.put_count(), 0)

    def test_update_rejects_creator_mismatch_5b(self):
        self.server.add_page(
            "8202", "外部页面", space_key="AI", labels=["ai-authored", "ai-pending"],
            created_by={"username": "zhangsan", "displayName": "张三"})
        self.write_registry([self._registry_record("8202")])
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", "8202", "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("创建人", self.combined(proc))
        self.assertEqual(self.server.put_count(), 0)

    def test_update_rejects_missing_authored_label_5c(self):
        self.server.add_page("8203", "外部页面", space_key="AI", labels=["ai-pending"])
        self.write_registry([self._registry_record("8203")])
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", "8203", "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("ai-authored", self.combined(proc))
        self.assertEqual(self.server.put_count(), 0)

    def test_update_rejects_human_modified_5d(self):
        page_id = self.create_ok()
        self.server.human_edit(page_id, "<p>人改过的内容</p>")
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", page_id, "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("页面已被人修改，人的修改优先，请先 pull 查看差异", self.combined(proc))
        self.assertEqual(self.server.put_count(), 0)

    def test_update_rejects_approved_5e(self):
        page_id = self.create_ok()
        self.server.set_labels(page_id, ["ai-authored", "ai-approved"])
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", page_id, "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("ai-approved", self.combined(proc))
        self.assertIn("冻结", self.combined(proc))
        self.assertEqual(self.server.put_count(), 0)

    def test_update_409_rejected(self):
        page_id = self.create_ok()
        self.server.state["force_put_conflict"] = True
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", page_id, "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("409", self.combined(proc))
        self.assertEqual(self.read_registry()[page_id]["last_version"], 1)

    def test_update_put_network_error_uncertain_exit6(self):
        page_id = self.create_ok()
        self.server.state["drop_put_response"] = True
        path = self.write_body(NEW_BODY, "new.md")
        proc = self.run_cli(["update", page_id, "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 6, self.combined(proc))
        self.assertEqual(self.server.put_count(), 1)  # 只请求 1 次，没重试
        uncertain = [r for r in self.read_ops() if r.get("status") == "uncertain"]
        self.assertEqual(len(uncertain), 1)
        self.assertEqual(uncertain[0]["op"], "update")
        self.assertEqual(uncertain[0]["page_id"], page_id)

    def test_update_twice_version_chain(self):
        page_id = self.create_ok()
        path = self.write_body(NEW_BODY, "v2.md")
        proc = self.run_cli(["update", page_id, "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        third = "# 支付回调说明\n\n这是第三版正文。\n"
        path = self.write_body(third, "v3.md")
        proc = self.run_cli(["update", page_id, "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertEqual(self.server.page(page_id)["version"]["number"], 3)
        self.assertEqual(self.read_registry()[page_id]["last_version"], 3)
        self.assertIn("第三版正文", self.server.page(page_id)["body"]["storage"]["value"])

    def test_update_chinese_body(self):
        page_id = self.create_ok()
        path = self.write_body("# 中文标题页\n\n更新后的中文正文内容。\n", "new.md")
        proc = self.run_cli(["update", page_id, "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertIn("更新后的中文正文内容",
                      self.server.page(page_id)["body"]["storage"]["value"])


class StatusPullTests(CliTestCase):
    def test_status_five_states(self):
        pending_id = self.create_ok(title="待审核页")
        approved_id = self.create_ok(title="已批准页")
        self.server.set_labels(approved_id, ["ai-authored", "ai-approved"])
        modified_id = self.create_ok(title="被改过的页")
        self.server.human_edit(modified_id, "<p>人动过的内容</p>")
        deleted_id = self.create_ok(title="被删除的页")
        self.server.delete_page(deleted_id)
        commented_id = self.create_ok(title="有评论的页")
        long_text = "请核对这一段的口径" * 20  # 远超 100 字
        self.server.add_comment(commented_id, author="王五", text=long_text)
        proc = self.run_cli(["status"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        text = self.out(proc)
        self.assertIn("待审核", text)
        self.assertIn("已批准", text)
        self.assertIn("被人修改过", text)
        self.assertIn("已删除", text)
        self.assertIn("有评论", text)
        self.assertIn("王五", text)
        self.assertIn(long_text[:100], text)       # 只显示前 100 字
        self.assertNotIn(long_text, text)
        for pid in (pending_id, approved_id, modified_id, deleted_id, commented_id):
            self.assertIn(pid, text)

    def test_status_single_page_and_unregistered(self):
        page_id = self.create_ok()
        proc = self.run_cli(["status", page_id])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertIn("待审核", self.out(proc))
        proc = self.run_cli(["status", "3001"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertIn("登记表", self.out(proc))

    def test_status_skips_pages_outside_allow_spaces(self):
        self.write_registry([{
            "page_id": "8301", "title": "研发空间登记页", "space": "DEV",
            "parent_id": "3001", "created_at": "2026-11-09T09:00:00+08:00",
            "last_version": 1, "content_hash": "c" * 64,
        }])
        proc = self.run_cli(["status"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertNotIn("研发空间登记页", self.out(proc))

    def test_pull_after_create(self):
        page_id = self.create_ok()
        proc = self.run_cli(["pull", page_id])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        saved = os.path.join(self.log_dir, "pull", "{0}.md".format(page_id))
        self.assertTrue(os.path.exists(saved))
        with open(saved, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("# 支付回调说明", content)
        self.assertIn("第一版正文", content)
        self.assertIn(cp.BANNER_TEXT, content)  # 横幅以引用块形式出现

    def test_pull_reflects_human_edit(self):
        page_id = self.create_ok()
        self.server.human_edit(page_id, "<h1>人改的新标题</h1><p>人改的正文内容</p>")
        out_path = os.path.join(self.tmp, "pulled.md")
        proc = self.run_cli(["pull", page_id, "--out", out_path])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        with open(out_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("# 人改的新标题", content)
        self.assertIn("人改的正文内容", content)

    def test_pull_not_registered_rejected(self):
        proc = self.run_cli(["pull", "3001"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("登记表", self.combined(proc))

    def test_pull_outside_allow_spaces_rejected(self):
        self.write_registry([{
            "page_id": "8302", "title": "研发空间登记页", "space": "DEV",
            "parent_id": "3001", "created_at": "2026-11-09T09:00:00+08:00",
            "last_version": 1, "content_hash": "d" * 64,
        }])
        proc = self.run_cli(["pull", "8302"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("不在 AI 专用空间", self.combined(proc))

    def test_log_lists_operations(self):
        self.create_ok()
        proc = self.run_cli(["log"])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertIn("create", self.out(proc))
        self.assertIn("回调说明页", self.out(proc))


class SensitiveScanTests(CliTestCase):
    # 验收口径：中文语境下必须命中，且报错里只能出现打码后的内容
    HIT_CASES = [
        ("请联系13812345678确认", "13812345678", "手机号"),
        ("证件号11010119900307123X有误", "11010119900307123X", "身份证号"),
        ("卡号6222021234567890123已失效", "6222021234567890123", "银行卡号"),
        ("配置中password=abc123有误", "abc123", "密钥赋值"),
        ("令牌token:xyz", "token:xyz", "密钥赋值"),
        ("password：abc123", "abc123", "密钥赋值"),
        ("api_key=abcd1234", "abcd1234", "密钥赋值"),
    ]

    def test_chinese_context_hits_in_body(self):
        for idx, (body, raw, rule) in enumerate(self.HIT_CASES):
            with self.subTest(rule=rule, body=body):
                path = self.write_body("# 标题\n\n{0}\n".format(body), "hit{0}.md".format(idx))
                proc = self.run_cli(
                    ["create", "--parent", "2001", "--title", "扫描页{0}".format(idx),
                     "--file", path, "--yes"])
                self.assertEqual(proc.returncode, 2, self.combined(proc))
                self.assertIn(rule, self.combined(proc))
                self.assertNotIn(raw, self.combined(proc))  # 原始敏感串不得外泄
                self.assertEqual(self.server.write_count(), 0)

    def test_sensitive_hit_in_title(self):
        path = self.write_body(GOOD_BODY)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "联系电话13812345678",
             "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("手机号", self.combined(proc))
        self.assertNotIn("13812345678", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_ordinary_chinese_no_false_positive(self):
        body = "# 版本说明\n\n共 3 处不一致已经修正，版本 12 的口径与页面一致。\n"
        page_id = self.create_ok(title="版本说明页", body=body)
        self.assertIn(page_id, self.server.state["pages"])


class LimitTests(CliTestCase):
    def test_run_write_limit(self):
        cfg = self.make_config(limits="max_writes_per_run = 1")
        self.create_ok(title="第一页", config_path=cfg)
        path = self.write_body(GOOD_BODY, "second.md")
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "第二页", "--file", path, "--yes"],
            config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("上限", self.combined(proc))
        self.assertEqual(self.server.create_post_count(), 1)


class ContextPathTests(unittest.TestCase):
    def test_context_path_base_url(self):
        server = FakeConfluenceServer(context_path="/confluence").start()
        self.addCleanup(server.stop)
        tmp = tempfile.mkdtemp(prefix="pages-ctx-")
        log_dir = os.path.join(tmp, "logs")
        cfg_path = os.path.join(tmp, "config.ini")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join([
                "[confluence]",
                "base_url = {0}".format(server.base_url),
                "token = test-token",
                "[pages]",
                "allow_spaces = AI",
                "[paths]",
                "log_dir = {0}".format(log_dir),
            ]) + "\n")
        body_path = os.path.join(tmp, "body.md")
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write(GOOD_BODY)
        env = dict(os.environ)
        env["CONFLUENCE_W_RUN_ID"] = "ctx-run"
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--config", cfg_path, "check"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--config", cfg_path, "create",
             "--parent", "2001", "--title", "上下文路径页", "--file", body_path, "--yes"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(server.create_post_count(), 1)


class EncodingTests(CliTestCase):
    def test_gbk_console_encoding(self):
        cfg = self.make_config(encoding="gbk")
        proc = self.run_cli(["check"], config_path=cfg)
        self.assertEqual(proc.returncode, 0)
        text = proc.stdout.decode("gbk")
        self.assertIn("连接成功", text)



def _norm(md):
    lines = [ln.rstrip() for ln in md.strip().split("\n")]
    out = []
    for ln in lines:
        if ln == "" and out and out[-1] == "":
            continue
        out.append(ln)
    return "\n".join(out).strip()


class MarkdownConvertTests(unittest.TestCase):
    """Markdown <-> storage 转换的单元测试（直接调转换函数）。"""

    def test_heading_levels_1_to_4(self):
        storage, warnings = cp.markdown_to_storage("# 一级\n\n## 二级\n\n### 三级\n\n#### 四级")
        self.assertEqual(warnings, [])
        for level in (1, 2, 3, 4):
            self.assertIn("<h{0}>".format(level), storage)

    def test_paragraph_inline_formats(self):
        storage, _ = cp.markdown_to_storage("有 **粗体**、*斜体*、`行内代码` 和[链接](/pages/123)。")
        self.assertIn("<strong>粗体</strong>", storage)
        self.assertIn("<em>斜体</em>", storage)
        self.assertIn("<code>行内代码</code>", storage)
        self.assertIn('<a href="/pages/123">链接</a>', storage)

    def test_unordered_and_ordered_lists(self):
        storage, _ = cp.markdown_to_storage("- 甲\n- 乙\n\n1. 第一步\n2. 第二步")
        self.assertIn("<ul><li>甲</li><li>乙</li></ul>", storage)
        self.assertIn("<ol><li>第一步</li><li>第二步</li></ol>", storage)

    def test_nested_list_two_levels(self):
        storage, _ = cp.markdown_to_storage("- 外层\n  - 内层甲\n  - 内层乙\n- 外层二")
        self.assertIn("<li>外层<ul><li>内层甲</li><li>内层乙</li></ul></li>", storage)

    def test_table(self):
        md = "| 字段 | 说明 |\n| --- | --- |\n| txn_id | 交易编号 |"
        storage, _ = cp.markdown_to_storage(md)
        self.assertIn("<th>字段</th>", storage)
        self.assertIn("<td>txn_id</td>", storage)
        self.assertIn("<td>交易编号</td>", storage)

    def test_code_block_becomes_code_macro_with_language(self):
        storage, _ = cp.markdown_to_storage("```python\nprint('你好')\n```")
        self.assertIn('<ac:structured-macro ac:name="code">', storage)
        self.assertIn('<ac:parameter ac:name="language">python</ac:parameter>', storage)
        self.assertIn("<![CDATA[print('你好')]]>", storage)

    def test_blockquote_and_hr(self):
        storage, _ = cp.markdown_to_storage("> 引用内容\n\n---")
        self.assertIn("<blockquote><p>引用内容</p></blockquote>", storage)
        self.assertIn("<hr/>", storage)

    def test_html_escaping(self):
        storage, _ = cp.markdown_to_storage("正文里的 <b>不是标签</b>，只是字面文本")
        self.assertNotIn("<b>", storage)
        self.assertIn("&lt;b&gt;", storage)

    def test_unsupported_image_warns_and_kept_literal(self):
        storage, warnings = cp.markdown_to_storage("先放一张图 ![示意图](img.png) 在这里")
        self.assertTrue(any("图片" in w for w in warnings))
        self.assertIn("![示意图](img.png)", storage)  # 原样输出，没有被转成链接
        self.assertNotIn("<a href", storage)

    def test_heading_level5_warns(self):
        storage, warnings = cp.markdown_to_storage("##### 五级标题")
        self.assertTrue(any("1–4 级标题" in w for w in warnings))
        self.assertNotIn("<h5>", storage)
        self.assertIn("##### 五级标题", storage)

    def test_strikethrough_warns(self):
        _storage, warnings = cp.markdown_to_storage("这段有 ~~删除线~~ 语法")
        self.assertTrue(any("删除线" in w for w in warnings))

    def test_roundtrip_combined_document(self):
        md = (
            "# 支付回调说明\n"
            "\n"
            "这是**第一段**，含 *斜体*、`代码` 和[链接](/pages/123)。\n"
            "\n"
            "## 处理步骤\n"
            "\n"
            "1. 接收通知\n"
            "2. 校验签名\n"
            "  - 子项甲\n"
            "  - 子项乙\n"
            "3. 返回成功\n"
            "\n"
            "- 无序一\n"
            "- 无序二\n"
            "  - 嵌套项\n"
            "\n"
            "| 字段 | 说明 |\n"
            "| --- | --- |\n"
            "| txn_id | 交易编号 |\n"
            "| amount | 金额 |\n"
            "\n"
            "```python\n"
            "def handle():\n"
            "    return \"ok\"\n"
            "```\n"
            "\n"
            "> 注意：回调可能重复投递。\n"
            "\n"
            "---\n"
        )
        storage, warnings = cp.markdown_to_storage(md)
        self.assertEqual(warnings, [])
        back = cp.storage_to_markdown(storage)
        self.assertEqual(_norm(back), _norm(md))

    def test_roundtrip_code_with_special_chars(self):
        md = "```xml\n<a>1 & 2</a>\n```"
        storage, _ = cp.markdown_to_storage(md)
        back = cp.storage_to_markdown(storage)
        self.assertEqual(_norm(back), _norm(md))

    def test_banner_storage_roundtrip_stable(self):
        md = cp.storage_to_markdown(cp.BANNER_STORAGE)
        self.assertEqual(md, "> " + cp.BANNER_TEXT)
        # 再转回去内容等价（横幅文本保留）
        storage, _ = cp.markdown_to_storage(md)
        self.assertIn(cp.BANNER_TEXT, cp.storage_to_markdown(storage))


class SourceDisciplineTests(unittest.TestCase):
    """源码纪律：写请求方法受限、Python 3.8 语法。"""

    def _source(self):
        with open(SCRIPT, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_no_delete_anywhere(self):
        self.assertIsNone(re.search(r"delete", self._source(), re.I))

    def test_request_methods_are_get_post_put_only(self):
        methods = set(re.findall(r'_request\("([A-Z]+)"', self._source()))
        self.assertEqual(methods, {"GET", "POST", "PUT"})

    def test_write_call_sites_by_function(self):
        import ast
        tree = ast.parse(self._source())
        post_in, put_in = set(), set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if not (isinstance(func, ast.Attribute) and func.attr == "_request"):
                    continue
                if sub.args and isinstance(sub.args[0], ast.Constant):
                    if sub.args[0].value == "POST":
                        post_in.add(node.name)
                    elif sub.args[0].value == "PUT":
                        put_in.add(node.name)
        # PUT 只出现在「更新自建页面正文」这一处
        self.assertEqual(put_in, {"update_page"})
        # POST 只用于新建页面和加标签
        self.assertEqual(post_in, {"create_page", "add_labels"})

    def test_python38_syntax(self):
        import ast
        ast.parse(self._source(), filename="confluence_pages.py", feature_version=(3, 8))


class LinkProtocolTests(unittest.TestCase):
    """返工 M1：Markdown 链接与图片 src 的协议白名单。

    只允许 http/https、以 / 开头的站内相对路径、以 # 开头的锚点；
    其他协议一律不生成链接，只输出转义后的纯文本并给出警告。
    """

    FORBIDDEN = [
        "[链接](javascript:alert(1))",
        "[链接](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
        "[链接](vbscript:msgbox)",
        "[链接](file:///etc/passwd)",
        "[链接](JaVaScRiPt:alert(1))",        # 不区分大小写
        "[链接](  javascript:alert(1))",      # 协议前空白
        "[链接](ftp://example.com/x)",        # ftp 也不在允许三类里
    ]

    def test_forbidden_protocols_rejected(self):
        for md in self.FORBIDDEN:
            with self.subTest(md=md):
                storage, warnings = cp.markdown_to_storage(md)
                self.assertNotIn("<a href", storage)
                self.assertNotIn("href=", storage)
                self.assertIn(md.replace("  javascript", "  javascript"), storage.replace("&amp;", "&"))
                self.assertTrue(any("协议不允许" in w for w in warnings), warnings)

    def test_encoded_bypass_rejected(self):
        for md in ("[链接](java%73cript:alert(1))",       # 百分号编码
                   "[链接](&#106;avascript:alert(1))",    # HTML 实体编码（十进制）
                   "[链接](&#x6a;avascript:alert(1))",    # HTML 实体编码（十六进制）
                   "[链接](java&#115;cript:alert(1))"):
            with self.subTest(md=md):
                storage, warnings = cp.markdown_to_storage(md)
                self.assertNotIn("<a href", storage)
                self.assertTrue(any("协议不允许" in w for w in warnings), warnings)

    def test_allowed_protocols_render_links(self):
        cases = [
            ("[链接](http://example.com/a)", '<a href="http://example.com/a">链接</a>'),
            ("[链接](https://example.com/a)", '<a href="https://example.com/a">链接</a>'),
            ("[链接](/pages/123)", '<a href="/pages/123">链接</a>'),
            ("[链接](#小节)", '<a href="#小节">链接</a>'),
        ]
        for md, expected in cases:
            with self.subTest(md=md):
                storage, warnings = cp.markdown_to_storage(md)
                self.assertIn(expected, storage)
                self.assertEqual(warnings, [])

    def test_image_src_forbidden_protocol(self):
        storage, warnings = cp.markdown_to_storage("配图 ![图](javascript:alert(1)) 在此")
        self.assertNotIn("<a href", storage)
        self.assertNotIn("<img", storage)
        self.assertIn("![图](javascript:alert(1))", storage)  # 只输出原文纯文本
        self.assertTrue(any("图片地址协议不允许" in w for w in warnings), warnings)

    def test_image_src_allowed_protocol_kept_literal(self):
        # 允许的协议：维持原行为（图片不渲染、按原文输出、给不支持警告）
        storage, warnings = cp.markdown_to_storage("配图 ![示意图](https://example.com/a.png) 在此")
        self.assertIn("![示意图](https://example.com/a.png)", storage)
        self.assertNotIn("<img", storage)
        self.assertTrue(any("图片语法不支持" in w for w in warnings), warnings)
        # 站内相对路径与锚点同样走「不支持」而非「协议不允许」
        _s, warnings = cp.markdown_to_storage("![图](/files/a.png)")
        self.assertTrue(any("图片语法不支持" in w for w in warnings), warnings)


class LinkParenUrlTests(unittest.TestCase):
    """返工 S1：URL 内的括号按配对解析；支持 <URL> 包裹写法。"""

    def test_balanced_parens_in_url(self):
        storage, warnings = cp.markdown_to_storage("[x](/a_(b))")
        self.assertIn('<a href="/a_(b)">x</a>', storage)
        self.assertNotIn("/a_(b)", storage.replace('<a href="/a_(b)">x</a>', ""))
        self.assertEqual(warnings, [])

    def test_nested_parens_in_url(self):
        storage, _ = cp.markdown_to_storage("[x](/a_(b_(c)))")
        self.assertIn('<a href="/a_(b_(c))">x</a>', storage)

    def test_angle_wrapped_url(self):
        storage, _ = cp.markdown_to_storage("[x](<https://example.com/a_(b)>)")
        self.assertIn('<a href="https://example.com/a_(b)">x</a>', storage)

    def test_angle_wrapped_url_with_space(self):
        storage, _ = cp.markdown_to_storage("[x](</a b_(c)>)")
        self.assertIn('<a href="/a b_(c)">x</a>', storage)

    def test_paren_url_pull_roundtrip(self):
        storage, _ = cp.markdown_to_storage("[x](/a_(b))")
        back = cp.storage_to_markdown(storage)
        self.assertIn("[x](/a_(b))", back)
        storage2, _ = cp.markdown_to_storage(back)
        self.assertEqual(storage2, storage)


class UrlWhitelistScanTests(unittest.TestCase):
    """追加单第 1 条（pages 侧）：敏感扫描的 URL 主机白名单。"""

    def test_whitelisted_ip_port_url_passes(self):
        hits = cp.scan_sensitive(
            "参见 http://10.1.1.1:8090/pages/viewpage.action?pageId=1 说明",
            ["10.1.1.1:8090"])
        self.assertEqual(hits, [])  # URL 整体放行，其中的 IP 也不触发 IP 规则

    def test_whitelisted_domain_and_default_port(self):
        # 条目不带端口：只比主机，任意端口都放行
        self.assertEqual(cp.scan_sensitive(
            "见 https://conf.example.com/x 说明", ["conf.example.com"]), [])
        self.assertEqual(cp.scan_sensitive(
            "见 https://conf.example.com:8443/x 说明", ["conf.example.com"]), [])
        # 条目带端口：缺省端口按协议归一化（https 缺省 443）
        self.assertEqual(cp.scan_sensitive(
            "见 https://conf.example.com/x 说明", ["conf.example.com:443"]), [])
        self.assertEqual(cp.scan_sensitive(
            "见 http://conf.example.com/x 说明", ["conf.example.com:80"]), [])

    def test_port_mismatch_blocked(self):
        hits = cp.scan_sensitive(
            "见 http://10.1.1.1:8090/x 说明", ["10.1.1.1:9999"])
        rules = [name for name, _masked in hits]
        self.assertIn("带主机名的 URL", rules)
        self.assertIn("IPv4 地址", rules)

    def test_no_whitelist_blocks_url_and_ip(self):
        hits = cp.scan_sensitive("见 http://10.1.1.1:8090/x 说明", [])
        rules = [name for name, _masked in hits]
        self.assertIn("带主机名的 URL", rules)
        self.assertIn("IPv4 地址", rules)

    def test_spoof_userinfo_blocked(self):
        # http://白名单@evil.com/ 的真实主机是 evil.com
        hits = cp.scan_sensitive(
            "见 http://10.1.1.1:8090@evil.com/x 说明", ["10.1.1.1:8090"])
        self.assertIn("带主机名的 URL", [name for name, _m in hits])

    def test_spoof_query_value_blocked(self):
        hits = cp.scan_sensitive(
            "见 http://evil.com/?next=10.1.1.1 说明", ["10.1.1.1"])
        self.assertIn("带主机名的 URL", [name for name, _m in hits])

    def test_spoof_subdomain_blocked(self):
        hits = cp.scan_sensitive(
            "见 http://10.1.1.1.evil.com/ 说明", ["10.1.1.1"])
        self.assertIn("带主机名的 URL", [name for name, _m in hits])

    def test_secret_query_param_blocked_even_whitelisted(self):
        for query in ("access_token=abc123", "Session_Id=xyz", "user_password=p"):
            with self.subTest(query=query):
                hits = cp.scan_sensitive(
                    "见 http://10.1.1.1:8090/x?{0} 说明".format(query),
                    ["10.1.1.1:8090"])
                self.assertNotEqual(hits, [])

    def test_ftp_never_exempt(self):
        hits = cp.scan_sensitive(
            "见 ftp://10.1.1.1:8090/x 说明", ["10.1.1.1:8090"])
        self.assertIn("带主机名的 URL", [name for name, _m in hits])

    def test_ipv6_whitelisted_passes(self):
        hits = cp.scan_sensitive(
            "见 http://[::1]:8090/x 说明", ["[::1]:8090"])
        self.assertEqual(hits, [])

    def test_bare_ip_outside_url_still_blocked(self):
        # 白名单只豁免 URL 内部的 IP；正文里裸写的 IP 照旧拦截
        hits = cp.scan_sensitive("服务器地址是 10.1.1.1，请核对", ["10.1.1.1"])
        self.assertIn("IPv4 地址", [name for name, _m in hits])


class UrlWhitelistCliTests(CliTestCase):
    """白名单在真实命令路径上的行为（config allow_url_hosts + base_url 自动并入）。"""

    def test_config_allow_url_hosts_passes(self):
        cfg = self.make_config(allow_url_hosts="docs.example.com")
        body = "外部文档见[文档](http://docs.example.com/guide)一节。\n"
        page_id = self.create_ok(title="白名单放行页", body=body, config_path=cfg)
        self.assertIn(page_id, self.server.state["pages"])

    def test_config_allow_url_hosts_wrong_port_blocked(self):
        cfg = self.make_config(allow_url_hosts="docs.example.com:9999")
        path = self.write_body("文档见 http://docs.example.com:8080/guide 一节。\n")
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "端口不符页", "--file", path, "--yes"],
            config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("带主机名的 URL", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_external_url_blocked_by_scan(self):
        path = self.write_body("参见 http://evil.example.com/x 的说明。\n")
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "外部地址页", "--file", path, "--yes"])
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertIn("带主机名的 URL", self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_base_url_host_auto_whitelisted(self):
        # 没配 allow_url_hosts：正文里出现 base_url 本身的地址也放行
        body = "本站地址是 {0}/pages/viewpage.action?pageId=2001 的说明。\n".format(
            self.server.base_url)
        page_id = self.create_ok(title="本站地址页", body=body)
        self.assertIn(page_id, self.server.state["pages"])

    def test_whitelisted_url_with_token_query_blocked(self):
        cfg = self.make_config(allow_url_hosts="docs.example.com")
        path = self.write_body("文档见 http://docs.example.com/g?access_token=abc123 一节。\n")
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "带令牌参数页", "--file", path, "--yes"],
            config_path=cfg)
        self.assertEqual(proc.returncode, 2, self.combined(proc))
        self.assertEqual(self.server.write_count(), 0)


def _client_for(server):
    cfg = {"base_url": server.base_url, "token": "test-token", "username": "",
           "password": "", "verify_ssl": True, "timeout": 10.0,
           "allow_system_proxy": False}
    return cp.ConfluenceClient(cfg)


class InternalLinkTests(unittest.TestCase):
    """追加单第 2 条：站内页面链接转 ac:link（模拟服务器 + 真实客户端）。"""

    def setUp(self):
        self.server = FakeConfluenceServer().start()
        self.addCleanup(self.server.stop)
        self.client = _client_for(self.server)
        self.base = self.server.base_url

    def convert(self, md, base=None):
        resolver = cp.InternalPageLinkResolver(self.client, base or self.base)
        storage, warnings = cp.markdown_to_storage(md, resolver)
        return storage, warnings, resolver

    def get_requests(self):
        return [path for method, path in self.server.state["requests"] if method == "GET"]

    def test_form1_viewpage_relative(self):
        storage, _w, resolver = self.convert("参见[首页](/pages/viewpage.action?pageId=2001)了解。")
        self.assertIn('<ac:link><ri:page ri:space-key="AI" ri:content-title="AI 空间首页"/>', storage)
        self.assertIn("<ac:plain-text-body><![CDATA[首页]]></ac:plain-text-body>", storage)
        self.assertTrue(any("/rest/api/content/2001" in p for p in self.get_requests()))
        self.assertEqual(resolver.resolved,
                         [("/pages/viewpage.action?pageId=2001", "AI 空间首页", "2001")])

    def test_form1_viewpage_absolute(self):
        url = "{0}/pages/viewpage.action?pageId=2001".format(self.base)
        storage, _w, _r = self.convert("参见[首页]({0})了解。".format(url))
        self.assertIn('ri:content-title="AI 空间首页"', storage)
        self.assertIn("<ac:plain-text-body><![CDATA[首页]]></ac:plain-text-body>", storage)

    def test_form2_display(self):
        import urllib.parse
        url = "/display/AI/" + urllib.parse.quote("AI 空间首页")
        storage, _w, resolver = self.convert("参见[点我]({0})。".format(url))
        self.assertIn('<ri:page ri:space-key="AI" ri:content-title="AI 空间首页"/>', storage)
        self.assertEqual(resolver.resolved[0][1:], ("AI 空间首页", "2001"))
        self.assertTrue(any("/rest/api/content?" in p for p in self.get_requests()))

    def test_form2_display_plus_separated_title(self):
        import urllib.parse
        url = "/display/AI/" + urllib.parse.quote_plus("AI 空间首页")
        storage, _w, _r = self.convert("参见[点我]({0})。".format(url))
        self.assertIn('ri:content-title="AI 空间首页"', storage)

    def test_form3_spaces(self):
        storage, _w, _r = self.convert("参见[归档](/spaces/AI/pages/2002/AI+归档区)。")
        self.assertIn('<ri:page ri:space-key="AI" ri:content-title="AI 归档区"/>', storage)
        self.assertTrue(any("/rest/api/content/2002" in p for p in self.get_requests()))

    def test_context_path_forms(self):
        ctx_server = FakeConfluenceServer(context_path="/confluence").start()
        self.addCleanup(ctx_server.stop)
        client = _client_for(ctx_server)
        base = ctx_server.base_url
        for md in ("[首页]({0}/pages/viewpage.action?pageId=2001)".format(base),
                   "[首页](/confluence/pages/viewpage.action?pageId=2001)"):
            with self.subTest(md=md):
                resolver = cp.InternalPageLinkResolver(client, base)
                storage, _w = cp.markdown_to_storage(md, resolver)
                self.assertIn('ri:content-title="AI 空间首页"', storage)

    def test_404_fallback_with_warning(self):
        storage, warnings, _r = self.convert("[不存在的页](/pages/viewpage.action?pageId=9999)")
        self.assertIn('<a href="/pages/viewpage.action?pageId=9999">不存在的页</a>', storage)
        self.assertNotIn("<ac:link>", storage)
        self.assertTrue(any("退回普通链接" in w for w in warnings), warnings)

    def test_bare_url_line_converted(self):
        url = "{0}/pages/viewpage.action?pageId=4001".format(self.base)
        storage, _w, _r = self.convert("上一节的内容见：\n\n{0}\n".format(url))
        self.assertIn("<p><ac:link>", storage)
        self.assertIn('ri:content-title="中文父页面标题"', storage)
        self.assertNotIn("ac:plain-text-body", storage)  # 没写文字，显示目标页标题

    def test_bare_url_relative_line_converted(self):
        storage, _w, _r = self.convert("/pages/viewpage.action?pageId=2001")
        self.assertIn('ri:content-title="AI 空间首页"', storage)

    def test_chinese_title_and_label(self):
        storage, _w, _r = self.convert("[中文父页面](/pages/viewpage.action?pageId=4001)")
        self.assertIn('ri:content-title="中文父页面标题"', storage)
        self.assertIn("<ac:plain-text-body><![CDATA[中文父页面]]></ac:plain-text-body>", storage)

    def test_label_equal_title_omits_body(self):
        storage, _w, _r = self.convert("[AI 空间首页](/pages/viewpage.action?pageId=2001)")
        self.assertIn("<ac:link>", storage)
        self.assertNotIn("ac:plain-text-body", storage)

    def test_target_in_other_space_still_links(self):
        # 目标页在 DEV 空间（reader 的排除口径与本工具无关）：照常生成链接
        storage, _w, _r = self.convert("[研发首页](/pages/viewpage.action?pageId=3001)")
        self.assertIn('<ri:page ri:space-key="DEV" ri:content-title="研发空间首页"/>', storage)

    def test_external_link_not_converted(self):
        storage, _w, resolver = self.convert("[外部](http://example.com/pages/viewpage.action?pageId=2001)")
        self.assertIn('<a href="http://example.com/pages/viewpage.action?pageId=2001">外部</a>', storage)
        self.assertEqual(resolver.resolved, [])

    def test_pull_roundtrip(self):
        storage, _w, _r = self.convert("参见[首页](/pages/viewpage.action?pageId=2001)了解。")
        back = cp.storage_to_markdown(storage)
        self.assertIn("[首页](/display/AI/", back)
        storage2, _w2, _r2 = self.convert(back)
        self.assertEqual(storage2, storage)  # 往返后 storage 完全一致

    def test_pull_roundtrip_bare_title_link(self):
        storage, _w, _r = self.convert("[AI 空间首页](/pages/viewpage.action?pageId=2001)")
        back = cp.storage_to_markdown(storage)
        self.assertIn("[AI 空间首页](/display/AI/", back)
        storage2, _w2, _r2 = self.convert(back)
        self.assertEqual(storage2, storage)


class InternalLinkCliTests(CliTestCase):
    """站内链接在 create 预览/执行路径上的行为。"""

    def test_create_preview_lists_internal_links(self):
        body = "参见[首页](/pages/viewpage.action?pageId=2001)了解背景。\n"
        path = self.write_body(body)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "链接预览页", "--file", path])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        text = self.out(proc)
        self.assertIn("站内页面链接", text)
        self.assertIn("/pages/viewpage.action?pageId=2001 → 《AI 空间首页》（ID 2001）", text)
        self.assertIn("<ac:link>", text)
        self.assertEqual(self.server.write_count(), 0)  # 预览仍然 0 次写请求
        gets = [p for m, p in self.server.state["requests"] if m == "GET"]
        self.assertTrue(any("/rest/api/content/2001" in p for p in gets))

    def test_create_yes_storage_has_ac_link(self):
        body = "参见[首页](/pages/viewpage.action?pageId=2001)了解背景。\n"
        page_id = self.create_ok(title="链接落库页", body=body)
        storage = self.server.page(page_id)["body"]["storage"]["value"]
        self.assertIn('<ac:link><ri:page ri:space-key="AI" ri:content-title="AI 空间首页"/>', storage)
        self.assertIn("<ac:plain-text-body><![CDATA[首页]]></ac:plain-text-body>", storage)

    def test_create_absolute_base_url_link(self):
        body = "参见[首页]({0}/pages/viewpage.action?pageId=2001)。\n".format(self.server.base_url)
        page_id = self.create_ok(title="绝对地址链接页", body=body)
        storage = self.server.page(page_id)["body"]["storage"]["value"]
        self.assertIn('ri:content-title="AI 空间首页"', storage)

    def test_create_preview_404_fallback(self):
        body = "参见[不存在的页](/pages/viewpage.action?pageId=9999)。\n"
        path = self.write_body(body)
        proc = self.run_cli(
            ["create", "--parent", "2001", "--title", "链接失效页", "--file", path])
        self.assertEqual(proc.returncode, 0, self.combined(proc))  # 不中断
        self.assertIn("退回普通链接", self.combined(proc))
        self.assertIn('<a href="/pages/viewpage.action?pageId=9999">不存在的页</a>',
                      self.out(proc))
        self.assertEqual(self.server.write_count(), 0)

    def test_update_preview_lists_internal_links(self):
        page_id = self.create_ok()
        body = "新版参见[归档区](/spaces/AI/pages/2002/AI+归档区)。\n"
        path = self.write_body(body, "new.md")
        proc = self.run_cli(["update", page_id, "--file", path])
        self.assertEqual(proc.returncode, 0, self.combined(proc))
        self.assertIn("站内页面链接", self.out(proc))
        self.assertIn("《AI 归档区》（ID 2002）", self.out(proc))
        self.assertEqual(self.server.put_count(), 0)


if __name__ == "__main__":
    unittest.main()
