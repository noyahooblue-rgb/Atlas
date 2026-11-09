#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟 Confluence 服务器（confluence-pages 测试专用，只用标准库 http.server）。

支持：
- Bearer 令牌与 Basic 用户名密码两种认证；
- GET /rest/api/user/current；GET 页面（正文、版本、空间、创建人、标签）；
- GET /rest/api/content?spaceKey=&title=&type=page 按标题查同名页面；
- GET 页面评论列表；POST 新建页面；POST 加标签；
- PUT 更新页面（校验版本号：必须等于当前版本 +1，否则 409）；
- 可模拟：页面被删除、context path、返回登录页、
  POST/PUT 已处理但断开连接不回响应、PUT 强制 409。

所有请求记录在 state["requests"]，POST 体在 state["posts"]，
PUT 体在 state["puts"]，供测试断言次数与内容。
"""

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOGIN_PAGE = (
    b'<html><body><form id="login" action="/login.action">'
    b'<input name="os_username"/><input name="os_password" type="password"/>'
    b"</form></body></html>"
)

CURRENT_USER = {"username": "tester", "displayName": "测试用户"}


def _page(page_id, title, space_key, body="", version=1, created_by=None, labels=None,
          parent_id=None):
    return {
        "id": page_id,
        "type": "page",
        "title": title,
        "space": {"key": space_key, "name": space_key},
        "version": {"number": version, "when": "2026-11-09T09:00:00.000+08:00",
                    "by": {"displayName": "测试用户"}},
        "history": {
            "createdBy": created_by or {"username": "tester", "displayName": "测试用户"},
            "createdDate": "2026-11-09T09:00:00.000+08:00",
        },
        "body": {"storage": {"value": body, "representation": "storage"}},
        "metadata": {"labels": {"results": [{"name": n} for n in (labels or [])],
                                "size": len(labels or [])}},
        "ancestors": [{"id": parent_id}] if parent_id else [],
    }


def make_state(context_path=""):
    """一份全新的模拟数据状态。

    页面布局：
    - 2001「AI 空间首页」空间 AI（空间首页，父页面正例）
    - 2002「AI 归档区」空间 AI（可配进 allow_parent_ids）
    - 3001「研发空间首页」空间 DEV（AI 空间之外的父页面）
    - 4001「中文父页面标题」空间 AI（中文全链路用）
    """
    return {
        "context_path": context_path,
        "token": "test-token",
        "username": "tester",
        "password": "test-pass",
        "force_login_page": False,
        "drop_post_response": False,   # POST 已处理但断开连接不回响应
        "drop_put_response": False,    # PUT 已处理但断开连接不回响应
        "force_put_conflict": False,   # PUT 一律回 409
        "pages": {
            "2001": _page("2001", "AI 空间首页", "AI", body="<p>首页正文</p>"),
            "2002": _page("2002", "AI 归档区", "AI", body="<p>归档区正文</p>", parent_id="2001"),
            "3001": _page("3001", "研发空间首页", "DEV", body="<p>研发首页正文</p>"),
            "4001": _page("4001", "中文父页面标题", "AI", body="<p>中文父页面正文</p>"),
        },
        "comments": {},            # 页面 ID -> [评论对象]
        "deleted_pages": set(),
        "next_page_id": 7001,
        "requests": [],            # [(method, path), ...]
        "posts": [],               # [(path, 请求体), ...]
        "puts": [],                # [(path, 请求体), ...]
    }


def _comment(author, when, text, comment_id="9001"):
    return {
        "id": comment_id,
        "type": "comment",
        "version": {"number": 1, "when": when, "by": {"displayName": author}},
        "history": {"createdDate": when, "createdBy": {"displayName": author}},
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

        def _drop_connection(self):
            self.close_connection = True
            self.connection.close()

        # ---- 路由 ----
        def do_GET(self):
            state["requests"].append(("GET", self.path))
            raw_path, _, query = self.path.partition("?")
            path = self._strip_context(raw_path)
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
                    "type": "known", "username": CURRENT_USER["username"],
                    "displayName": CURRENT_USER["displayName"],
                })
                return
            if path == "/rest/api/content":
                params = urllib.parse.parse_qs(query)
                space_key = (params.get("spaceKey") or [""])[0]
                title = (params.get("title") or [""])[0]
                results = [
                    page for page in state["pages"].values()
                    if page["id"] not in state["deleted_pages"]
                    and page["space"]["key"] == space_key and page["title"] == title
                ]
                self._send_json(200, {"results": results, "size": len(results)})
                return
            if path.startswith("/rest/api/content/"):
                rest = path[len("/rest/api/content/"):]
                parts = rest.split("/")
                content_id = parts[0]
                if len(parts) == 3 and parts[1] == "child" and parts[2] == "comment":
                    results = state["comments"].get(content_id, [])
                    self._send_json(200, {"results": results, "size": len(results)})
                    return
                if content_id in state["deleted_pages"] or content_id not in state["pages"]:
                    self._send_json(404, {"message": "Not found"})
                    return
                self._send_json(200, state["pages"][content_id])
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
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                self._send_json(400, {"message": "Bad JSON"})
                return
            if path == "/rest/api/content":
                state["posts"].append((path, payload))
                page_id = str(state["next_page_id"])
                state["next_page_id"] += 1
                ancestors = payload.get("ancestors") or []
                parent_id = ancestors[0].get("id") if ancestors else None
                page = _page(
                    page_id,
                    payload.get("title", ""),
                    (payload.get("space") or {}).get("key", ""),
                    body=((payload.get("body") or {}).get("storage") or {}).get("value", ""),
                    version=1,
                    parent_id=parent_id,
                )
                state["pages"][page_id] = page
                if state["drop_post_response"]:
                    # 页面其实已建好，只是响应丢了
                    self._drop_connection()
                    return
                self._send_json(200, page)
                return
            if path.startswith("/rest/api/content/") and path.endswith("/label"):
                state["posts"].append((path, payload))
                page_id = path[len("/rest/api/content/"):-len("/label")]
                page = state["pages"].get(page_id)
                if page is None or page_id in state["deleted_pages"]:
                    self._send_json(404, {"message": "Not found"})
                    return
                labels = page["metadata"]["labels"]["results"]
                for item in payload or []:
                    if item.get("name") and all(x["name"] != item["name"] for x in labels):
                        labels.append({"name": item["name"]})
                page["metadata"]["labels"]["size"] = len(labels)
                if state["drop_post_response"]:
                    self._drop_connection()
                    return
                self._send_json(200, page["metadata"]["labels"])
                return
            self._send_json(404, {"message": "Unknown path"})

        def do_PUT(self):
            state["requests"].append(("PUT", self.path))
            path = self._strip_context(self.path.split("?")[0])
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            if not self._check_auth():
                self._send_json(401, {"message": "Unauthorized"})
                return
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                self._send_json(400, {"message": "Bad JSON"})
                return
            if path.startswith("/rest/api/content/"):
                page_id = path[len("/rest/api/content/"):]
                state["puts"].append((path, payload))
                page = state["pages"].get(page_id)
                if page is None or page_id in state["deleted_pages"]:
                    self._send_json(404, {"message": "Not found"})
                    return
                if state["force_put_conflict"]:
                    self._send_json(409, {"message": "Version conflict"})
                    return
                new_version = ((payload.get("version") or {}).get("number"))
                if new_version != page["version"]["number"] + 1:
                    self._send_json(409, {"message": "Version conflict"})
                    return
                page["title"] = payload.get("title", page["title"])
                page["body"] = payload.get("body", page["body"])
                page["version"]["number"] = new_version
                if state["drop_put_response"]:
                    # 更新其实已生效，只是响应丢了
                    self._drop_connection()
                    return
                self._send_json(200, page)
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
    def request_count(self):
        return len(self.state["requests"])

    def write_count(self):
        return len(self.state["posts"]) + len(self.state["puts"])

    def create_post_count(self):
        return len([1 for path, _ in self.state["posts"] if path == "/rest/api/content"
                    or path.endswith("/rest/api/content")])

    def put_count(self):
        return len(self.state["puts"])

    def page(self, page_id):
        return self.state["pages"].get(page_id)

    def labels_of(self, page_id):
        page = self.state["pages"][page_id]
        return [x["name"] for x in page["metadata"]["labels"]["results"]]

    def add_page(self, page_id, title, space_key="AI", body="", version=1,
                 created_by=None, labels=None, parent_id=None):
        self.state["pages"][page_id] = _page(
            page_id, title, space_key, body=body, version=version,
            created_by=created_by, labels=labels, parent_id=parent_id)
        return self.state["pages"][page_id]

    def add_comment(self, page_id, author="王五", when="2026-11-09T10:30:00.000+08:00",
                    text="这一段请再核对一下口径。", comment_id=None):
        comment_id = comment_id or "9{0}".format(page_id)
        comment = _comment(author, when, text, comment_id=comment_id)
        self.state["comments"].setdefault(page_id, []).append(comment)
        return comment

    def human_edit(self, page_id, new_body):
        """模拟人直接在页面上改了正文：内容变、版本号 +1。"""
        page = self.state["pages"][page_id]
        page["body"] = {"storage": {"value": new_body, "representation": "storage"}}
        page["version"]["number"] += 1

    def set_labels(self, page_id, labels):
        page = self.state["pages"][page_id]
        page["metadata"]["labels"] = {
            "results": [{"name": n} for n in labels], "size": len(labels)}

    def delete_page(self, page_id):
        self.state["deleted_pages"].add(page_id)
