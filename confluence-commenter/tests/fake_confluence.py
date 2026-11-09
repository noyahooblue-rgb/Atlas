#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟 Confluence 服务器（测试专用，只用标准库 http.server）。

支持：
- Bearer 令牌与 Basic 用户名密码两种认证；
- GET /rest/api/user/current；GET 页面（version、space、title）；
- POST /rest/api/content 新建评论；GET 评论的子评论；
- 可模拟：评论被删（GET 返回 404）、页面版本递增、
  context path（如 /confluence）、对 user/current 返回登录页。

所有收到的请求都记录在 state["requests"]（(method, path) 列表），
POST 的请求体记录在 state["posts"]，供测试断言次数与内容。
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOGIN_PAGE = (
    b'<html><body><form id="login" action="/login.action">'
    b'<input name="os_username"/><input name="os_password" type="password"/>'
    b"</form></body></html>"
)


def make_state(context_path=""):
    """一份全新的模拟数据状态。"""
    return {
        "context_path": context_path,
        "token": "test-token",
        "username": "tester",
        "password": "test-pass",
        "force_login_page": False,
        "drop_post_response": False,  # True 时 POST 已处理但断开连接不回响应
        "pages": {
            "1001": {
                "id": "1001", "type": "page", "title": "订单流程说明",
                "space": {"key": "DEV", "name": "研发空间"},
                "version": {"number": 3},
            },
            "1002": {
                "id": "1002", "type": "page", "title": "机密项目总览",
                "space": {"key": "SECRET", "name": "保密空间"},
                "version": {"number": 1},
            },
        },
        "comments": {},          # 评论 ID -> 评论对象
        "children": {},          # 评论 ID -> [子评论对象]
        "deleted_comments": set(),
        "next_comment_id": 5001,
        "requests": [],          # [(method, path), ...]
        "posts": [],             # [请求体 dict, ...]
    }


def _reply(display_name, when, text, reply_id="9001"):
    return {
        "id": reply_id,
        "type": "comment",
        "version": {"number": 1, "when": when, "by": {"displayName": display_name}},
        "history": {"createdDate": when, "createdBy": {"displayName": display_name}},
        "body": {"storage": {"value": "<p>{0}</p>".format(text), "representation": "storage"}},
    }


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 测试时保持安静
            pass

        # ---- 工具方法 ----
        def _send_json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self):
            header = self.headers.get("Authorization", "")
            if header == "Bearer " + state["token"]:
                return True
            if header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(header[6:]).decode("utf-8")
                except Exception:
                    return False
                return decoded == "{0}:{1}".format(state["username"], state["password"])
            return False

        def _strip_context(self, path):
            prefix = state["context_path"]
            if prefix and path.startswith(prefix):
                return path[len(prefix):]
            return path

        # ---- 路由 ----
        def do_GET(self):
            state["requests"].append(("GET", self.path))
            path = self._strip_context(self.path.split("?")[0])
            if not self._check_auth():
                self._send_json(401, {"message": "Unauthorized"})
                return
            if path == "/rest/api/user/current":
                if state["force_login_page"]:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(LOGIN_PAGE)))
                    self.end_headers()
                    self.wfile.write(LOGIN_PAGE)
                    return
                self._send_json(200, {
                    "type": "known", "username": "tester", "displayName": "测试用户",
                })
                return
            if path.startswith("/rest/api/content/"):
                rest = path[len("/rest/api/content/"):]
                parts = rest.split("/")
                content_id = parts[0]
                if len(parts) == 3 and parts[1] == "child" and parts[2] == "comment":
                    results = state["children"].get(content_id, [])
                    self._send_json(200, {"results": results, "size": len(results)})
                    return
                if content_id in state["deleted_comments"]:
                    self._send_json(404, {"message": "Comment not found"})
                    return
                if content_id in state["pages"]:
                    self._send_json(200, state["pages"][content_id])
                    return
                if content_id in state["comments"]:
                    self._send_json(200, state["comments"][content_id])
                    return
                self._send_json(404, {"message": "Not found"})
                return
            self._send_json(404, {"message": "Unknown path"})

        def do_POST(self):
            state["requests"].append(("POST", self.path))
            path = self._strip_context(self.path.split("?")[0])
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            if not self._check_auth():
                self._send_json(401, {"message": "Unauthorized"})
                return
            if path == "/rest/api/content":
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._send_json(400, {"message": "Bad JSON"})
                    return
                state["posts"].append(payload)
                comment_id = str(state["next_comment_id"])
                state["next_comment_id"] += 1
                comment = {
                    "id": comment_id,
                    "type": "comment",
                    "container": payload.get("container"),
                    "ancestors": payload.get("ancestors", []),
                    "body": payload.get("body"),
                    "version": {"number": 1},
                }
                state["comments"][comment_id] = comment
                if state["drop_post_response"]:
                    # 模拟「第一次 POST 其实已成功、只是响应丢了」：
                    # 评论已存下，但直接断开连接，不回任何响应。
                    self.close_connection = True
                    self.connection.close()
                    return
                self._send_json(200, {"id": comment_id, "type": "comment"})
                return
            self._send_json(404, {"message": "Unknown path"})

    return Handler


class FakeConfluenceServer:
    """在随机端口上启动模拟服务器，with 语句或 start/stop 管理。"""

    def __init__(self, context_path=""):
        self.state = make_state(context_path)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self._thread = None
        port = self._server.server_address[1]
        self.base_url = "http://127.0.0.1:{0}{1}".format(port, context_path)

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---- 测试辅助 ----
    def post_count(self):
        return len(self.state["posts"])

    def request_count(self):
        return len(self.state["requests"])

    def add_page(self, page_id, title, space_key="DEV", version=1):
        self.state["pages"][page_id] = {
            "id": page_id, "type": "page", "title": title,
            "space": {"key": space_key, "name": space_key},
            "version": {"number": version},
        }

    def add_reply(self, comment_id, display_name="李四", when="2026-11-09T10:00:00.000+08:00",
                  text="已确认，按代码为准修改文档。"):
        reply = _reply(display_name, when, text, reply_id="9{0}".format(comment_id))
        self.state["children"].setdefault(comment_id, []).append(reply)

    def bump_page_version(self, page_id):
        self.state["pages"][page_id]["version"]["number"] += 1

    def delete_comment(self, comment_id):
        self.state["deleted_comments"].add(comment_id)
