# -*- coding: utf-8 -*-
"""模拟 Confluence 服务器（测试专用，纯标准库 http.server）。

支持：Basic / Bearer / Cookie / 表单登录、context path、CQL 搜索接口、
页面（含版本、空间、祖先、标签、附件）、评论（含 AI 评论与回复链）、
页面版本递增、删除页面（404）、认证无效时返回登录页。
"""
import base64
import json
import re
import struct
import threading
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CTX = "/ctx"

USERS = {
    "alice": {"password": "pw1", "display": "Alice 张三"},
    "bob": {"password": "pw2", "display": "Bob 李四"},
    "aibot": {"password": "pw3", "display": "AI 核对员"},
    "carol": {"password": "pw4", "display": "Carol 赵六"},  # 该账号禁用 Basic，须走表单登录
}
TOKENS = {"tok-123": "alice"}

LOGIN_PAGE = ('<html><head><title>Log in</title></head><body>'
              '<form action="/dologin.action" method="post">'
              '<input name="os_username"/><input name="os_password" type="password"/>'
              '</form></body></html>')


def user_obj(username):
    # REST 风格的用户对象
    u = USERS.get(username, {"display": username})
    return {"type": "known", "username": username, "displayName": u["display"],
            "userKey": "key-" + username}


def strip_tags(html_text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text or "")).strip()


def tiny_png():
    # 生成一张 1x1 的合法 PNG
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _page(pid, title, space, parent, versions, current, created="2026-10-01T09:00:00.000+08:00",
          created_by="alice", labels=None, attachments=None, comments=None, ptype="page", deleted=False):
    return {"id": pid, "title": title, "space": space, "parent": parent, "versions": versions,
            "current": current, "created": created, "created_by": created_by,
            "labels": labels or [], "attachments": attachments or [], "comments": comments or [],
            "type": ptype, "deleted": deleted}


def _ver(body, when, by="alice"):
    return {"body_view": body, "body_storage": body, "when": when, "by": by}


def build_store():
    # 构造测试数据：空间、页面（含版本/祖先/附件/评论）、已删除页面
    spaces = {
        "DEV": {"key": "DEV", "name": "开发空间", "type": "global"},
        "OPS": {"key": "OPS", "name": "运维空间", "type": "global"},
        "SECRET": {"key": "SECRET", "name": "保密空间", "type": "global"},
    }
    pages = {}

    def add(p):
        pages[p["id"]] = p

    add(_page("1001", "支付接口规范", "DEV", None,
              {1: _ver("<p>支付接口第一版。</p>", "2026-10-20T10:00:00.000+08:00"),
               2: _ver("<p>支付接口第二版。</p>", "2026-10-28T10:00:00.000+08:00"),
               3: _ver('<p>支付接口用于订单支付，交易码 TXN001。</p>'
                       '<p>订单表 order_info 记录支付结果。</p>'
                       '<p><img src="/ctx/download/attachments/1001/pic.png"/></p>',
                       "2026-11-05T10:30:00.000+08:00")},
              3, labels=["接口"],
              attachments=[
                  {"id": "att1", "title": "notes.txt", "data": "附件正文：费率表说明。".encode("utf-8"),
                   "mediaType": "text/plain", "version": 1},
                  {"id": "att2", "title": "pic.png", "data": tiny_png(),
                   "mediaType": "image/png", "version": 1},
              ]))
    add(_page("1002", "支付渠道接入", "DEV", "1001",
              {1: _ver("<p>支付渠道接入说明，含微信与支付宝渠道。</p>", "2026-11-06T09:00:00.000+08:00")}, 1))
    add(_page("1003", "订单表结构", "DEV", None,
              {2: _ver("<p>订单表 order_info 的字段定义。</p>", "2026-11-07T09:00:00.000+08:00")}, 2))
    add(_page("1004", "历史草稿文档", "DEV", None,
              {1: _ver("<p>支付的旧草稿。</p>", "2025-01-01T09:00:00.000+08:00")}, 1))
    add(_page("1005", "运维手册", "OPS", None,
              {1: _ver("<p>支付系统的运维手册。</p>", "2026-11-08T09:00:00.000+08:00")}, 1))
    add(_page("1006", "保密项目计划", "SECRET", None,
              {1: _ver("<p>支付保密项目计划。</p>", "2026-11-08T10:00:00.000+08:00")}, 1))
    add(_page("1007", "被排除的页面", "DEV", None,
              {1: _ver("<p>支付的内部页面。</p>", "2026-11-08T11:00:00.000+08:00")}, 1))
    add(_page("1008", "根页面甲", "DEV", None,
              {1: _ver("<p>祖先词根页面。</p>", "2026-11-01T09:00:00.000+08:00")}, 1))
    add(_page("1009", "子页面乙", "DEV", "1008",
              {1: _ver("<p>祖先词子页面。</p>", "2026-11-02T09:00:00.000+08:00")}, 1))
    add(_page("1010", "孙页面丙", "DEV", "1009",
              {1: _ver("<p>祖先词孙页面。</p>", "2026-11-03T09:00:00.000+08:00")}, 1))
    add(_page("1011", "评论测试页", "DEV", None,
              {2: _ver("<p>评论测试页正文。</p>", "2026-11-02T08:00:00.000+08:00")}, 2,
              comments=[
                  {"id": "c1", "author": "alice", "when": "2026-11-02T09:00:00.000+08:00",
                   "body": "<p>人工评论：请确认费率字段。</p>", "parent": None},
                  {"id": "c2", "author": "aibot", "when": "2026-11-02T09:05:00.000+08:00",
                   "body": "<p>【AI 核对】费率字段与代码不一致：文档写 0.6%，代码为 0.5%。</p>", "parent": None},
                  {"id": "c3", "author": "aibot", "when": "2026-11-02T09:06:00.000+08:00",
                   "body": "<p>补充：第二处也对过了。</p>", "parent": "c2"},
                  {"id": "c4", "author": "alice", "when": "2026-11-02T09:10:00.000+08:00",
                   "body": "<p>人工回复：费率以代码为准，我来改文档。</p>", "parent": "c2"},
                  {"id": "c5", "author": "aibot", "when": "2026-11-02T09:12:00.000+08:00",
                   "body": "<p>收到，我再复核一遍。</p>", "parent": "c4"},
              ]))
    add(_page("1012", "版本测试页", "DEV", None,
              {1: _ver("<p>第一版内容。</p>", "2026-10-01T10:00:00.000+08:00"),
               2: _ver("<p>第二版内容。</p>", "2026-11-01T10:00:00.000+08:00")}, 2))
    add(_page("1013", "已删除页面", "DEV", None,
              {1: _ver("<p>不存在了。</p>", "2026-10-01T10:00:00.000+08:00")}, 1, deleted=True))
    add(_page("1014", "支付周报", "DEV", None,
              {1: _ver("<p>支付周报正文。</p>", "2026-11-08T12:00:00.000+08:00")}, 1, ptype="blogpost"))
    # find 排序专用数据：检索词「苹果」「香蕉」
    add(_page("2001", "苹果香蕉派", "DEV", None,
              {1: _ver("<p>苹果 香蕉 都好吃。</p>", "2026-11-01T09:00:00.000+08:00")}, 1))
    add(_page("2002", "水果记录", "DEV", None,
              {1: _ver("<p>苹果 香蕉 都有记录。</p>", "2026-11-09T09:00:00.000+08:00")}, 1))
    add(_page("2003", "苹果日报", "DEV", None,
              {1: _ver("<p>苹果 的新闻。</p>", "2026-11-08T09:00:00.000+08:00")}, 1))
    add(_page("2004", "香蕉日报", "DEV", None,
              {1: _ver("<p>香蕉 的新闻。</p>", "2026-11-07T09:00:00.000+08:00")}, 1))
    add(_page("2005", "苹果旧闻", "DEV", None,
              {1: _ver("<p>苹果 的旧闻。</p>", "2024-06-01T09:00:00.000+08:00")}, 1))
    return {"spaces": spaces, "pages": pages}


def ancestors_of(store, page):
    # 由父链算出祖先列表（根在前）
    chain, seen, pid = [], set(), page["parent"]
    while pid and pid not in seen and pid in store["pages"]:
        seen.add(pid)
        p = store["pages"][pid]
        chain.append(p)
        pid = p["parent"]
    chain.reverse()
    return chain


def page_text(page):
    ver = page["versions"][page["current"]]
    return page["title"] + " " + strip_tags(ver["body_view"])


# ---------------- CQL 子集求值 ----------------

class CqlError(Exception):
    pass


_TOKEN_RE = re.compile(r'\s*(\(|\)|,|>=|<=|!=|=|~|>|<|"(?:\\.|[^"\\])*"|[^\s(),=~<>!"]+)')


def _unquote(tok):
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return tok


class _CqlParser:
    """极简 CQL 解析：支持 and/or、括号、字段 ~ / = / != / >= / <= / in (...)。"""

    def __init__(self, text):
        self.toks, i = [], 0
        while i < len(text):
            m = _TOKEN_RE.match(text, i)
            if not m:
                raise CqlError("无法解析 CQL: " + text)
            self.toks.append(m.group(1))
            i = m.end()
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def parse(self):
        node = self.parse_or()
        if self.peek() is not None:
            raise CqlError("CQL 尾部多余内容")
        return node

    def parse_or(self):
        left = self.parse_and()
        while (self.peek() or "").lower() == "or":
            self.next()
            left = ("or", left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_atom()
        while (self.peek() or "").lower() == "and":
            self.next()
            left = ("and", left, self.parse_atom())
        return left

    def parse_atom(self):
        if self.peek() == "(":
            self.next()
            node = self.parse_or()
            if self.next() != ")":
                raise CqlError("CQL 缺少右括号")
            return node
        field = self.next()
        if field is None:
            raise CqlError("CQL 不完整")
        if (self.peek() or "").lower() == "in":
            self.next()
            if self.next() != "(":
                raise CqlError("in 后缺少 (")
            vals = []
            while True:
                t = self.next()
                if t == ")":
                    break
                if t != ",":
                    vals.append(_unquote(t))
            return ("in", field.lower(), vals)
        op = self.next()
        val = self.next()
        if op is None or val is None:
            raise CqlError("CQL 比较不完整")
        return ("cmp", field.lower(), op, _unquote(val))


def eval_cql(node, page):
    # 对单个页面求值 CQL 语法树
    kind = node[0]
    if kind == "and":
        return eval_cql(node[1], page) and eval_cql(node[2], page)
    if kind == "or":
        return eval_cql(node[1], page) or eval_cql(node[2], page)
    if kind == "in":
        field, vals = node[1], node[2]
        if field == "type":
            return page["type"] in vals
        if field == "space":
            return page["space"] in vals
        raise CqlError("不支持的 in 字段: " + field)
    _, field, op, val = node
    if field == "text":
        hay = page_text(page).lower()
        return (val.lower() in hay) if op == "~" else (page_text(page) == val)
    if field == "title":
        return (val.lower() in page["title"].lower()) if op == "~" else (page["title"] == val)
    if field == "type":
        return (page["type"] == val) if op == "=" else (page["type"] != val)
    if field == "space":
        return page["space"] == val
    if field == "label":
        return val in page["labels"]
    if field in ("lastmodified", "lastmodifieddate"):
        when = page["versions"][page["current"]]["when"][:10]
        return {">=": when >= val, "<=": when <= val, ">": when > val, "<": when < val,
                "=": when == val}.get(op, False)
    raise CqlError("不支持的 CQL 字段: " + field)


def search_pages(store, cql):
    # 按 CQL 返回未删除页面（按 ID 排序，保证结果确定性）
    ast = _CqlParser(cql).parse()
    out = [p for p in store["pages"].values() if not p["deleted"] and eval_cql(ast, p)]
    out.sort(key=lambda p: p["id"])
    return out


# ---------------- JSON 构造 ----------------

def content_json(store, page, expand="", version_no=None):
    ver_no = version_no if version_no is not None else page["current"]
    ver = page["versions"][ver_no]
    exp = set(x.strip() for x in (expand or "").split(",") if x.strip())
    obj = {"id": page["id"], "type": page["type"], "status": "current", "title": page["title"],
           "_links": {"webui": "/pages/viewpage.action?pageId=" + page["id"]}}
    if "version" in exp or version_no is not None:
        obj["version"] = {"number": ver_no, "by": user_obj(ver["by"]), "when": ver["when"],
                          "message": "", "minorEdit": False}
    if "space" in exp:
        sp = store["spaces"][page["space"]]
        obj["space"] = {"key": sp["key"], "name": sp["name"], "type": sp["type"], "id": 1}
    if "ancestors" in exp:
        obj["ancestors"] = [{"id": a["id"], "type": a["type"], "title": a["title"]}
                            for a in ancestors_of(store, page)]
    if "history" in exp:
        obj["history"] = {"createdBy": user_obj(page["created_by"]), "createdDate": page["created"]}
    body = {}
    if "body.storage" in exp:
        body["storage"] = {"value": ver["body_storage"], "representation": "storage"}
    if "body.view" in exp:
        body["view"] = {"value": ver["body_view"], "representation": "view"}
    if body:
        obj["body"] = body
    if "metadata.labels" in exp:
        obj["metadata"] = {"labels": {"results": [{"name": n} for n in page["labels"]],
                                      "size": len(page["labels"])}}
    return obj


def comment_json(store, page, cm):
    anc = [{"id": page["id"], "type": "page", "title": page["title"]}]
    chain, seen, pid = [], set(), cm.get("parent")
    by_id = {c["id"]: c for c in page["comments"]}
    while pid and pid not in seen and pid in by_id:
        seen.add(pid)
        chain.append(by_id[pid])
        pid = by_id[pid].get("parent")
    for pc in reversed(chain):
        anc.append({"id": pc["id"], "type": "comment", "title": "Re: " + page["title"]})
    obj = {"id": cm["id"], "type": "comment", "title": "Re: " + page["title"],
           "body": {"view": {"value": cm["body"], "representation": "view"}},
           "version": {"number": 1, "by": user_obj(cm["author"]), "when": cm["when"]},
           "history": {"createdBy": user_obj(cm["author"]), "createdDate": cm["when"]},
           "ancestors": anc, "extensions": {"location": "footer"}}
    if cm.get("selection"):
        obj["extensions"]["inlineProperties"] = {"originalSelection": cm["selection"]}
    return obj


def attachment_json(page, att):
    return {"id": att["id"], "type": "attachment", "title": att["title"],
            "version": {"number": att["version"]},
            "extensions": {"fileSize": len(att["data"]), "mediaType": att["mediaType"]},
            "_links": {"download": "/download/attachments/%s/%s" % (page["id"], att["title"])}}


# ---------------- HTTP 处理 ----------------

class MockConfluence:
    """启动一个带 context path 的模拟 Confluence，记录全部请求便于断言。"""

    def __init__(self, ctx=CTX):
        self.store = build_store()
        self.ctx = ctx
        self.log = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.handle(self, None)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.handle(self, self.rfile.read(n) if n else b"")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        return "http://127.0.0.1:%d%s" % (self.port, self.ctx)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def reset_log(self):
        with self.lock:
            self.log = []

    def count(self, substr):
        with self.lock:
            return sum(1 for _, p in self.log if substr in p)

    # ---- 响应工具 ----
    def send_json(self, h, obj, code=200, headers=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    def send_text(self, h, text, code=200, ctype="text/html; charset=utf-8", headers=None):
        raw = text.encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    def auth_user(self, h):
        """返回 (username, special)：special 为 'loginpage' 表示返回登录页，'basic_disabled' 表示禁用 Basic。"""
        cookie = h.headers.get("Cookie") or ""
        if "bad-session" in cookie:
            return None, "loginpage"
        if "SESSION=valid-carol" in cookie:
            return "carol", None
        if "good-session" in cookie:
            return "alice", None
        auth = h.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            u = TOKENS.get(auth[7:].strip())
            return (u, None) if u else (None, None)
        if auth.startswith("Basic "):
            try:
                raw = base64.b64decode(auth[6:]).decode("utf-8")
                name, _, pw = raw.partition(":")
            except Exception:
                return None, None
            if name == "carol":
                return None, "basic_disabled"
            if name in USERS and USERS[name]["password"] == pw:
                return name, None
            return None, None
        return None, None

    def paginate(self, h, items, q, path):
        start = int(q.get("start", ["0"])[0])
        limit = int(q.get("limit", ["25"])[0])
        part = items[start:start + limit]
        out = {"results": part, "start": start, "limit": limit, "size": len(part),
               "_links": {"base": self.base_url}}
        if start + limit < len(items):
            out["_links"]["next"] = "%s?start=%d&limit=%d" % (path, start + limit, limit)
        return out

    def handle(self, h, post_data):
        parsed = urllib.parse.urlsplit(h.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        if self.ctx and (path == self.ctx or path.startswith(self.ctx + "/")):
            rest = path[len(self.ctx):] or "/"
        else:
            rest = path
        with self.lock:
            self.log.append((h.command, rest))
        try:
            self.route(h, rest, q, post_data)
        except BrokenPipeError:
            pass
        except Exception as e:  # 模拟服务自身出错时返回 500，便于测试定位
            try:
                self.send_json(h, {"message": "mock error: %r" % (e,)}, 500)
            except Exception:
                pass

    def route(self, h, rest, q, post_data):
        store = self.store
        # 表单登录（唯一的 POST）
        if rest == "/dologin.action" and h.command == "POST":
            form = urllib.parse.parse_qs((post_data or b"").decode("utf-8", "replace"))
            name = (form.get("os_username") or [""])[0]
            pw = (form.get("os_password") or [""])[0]
            if name in USERS and USERS[name]["password"] == pw:
                self.send_text(h, "<html>ok</html>", 200,
                               headers={"X-Seraph-LoginReason": "OK",
                                        "Set-Cookie": "SESSION=valid-%s; Path=/" % name})
            else:
                self.send_text(h, "<html>failed</html>", 200,
                               headers={"X-Seraph-LoginReason": "AUTHENTICATION_FAILED"})
            return
        if rest == "/rest/applinks/1.0/manifest":
            self.send_json(h, {"name": "Mock Confluence", "typeId": "confluence", "version": "7.19.0"})
            return
        # 其余 REST 接口都要认证
        user, special = self.auth_user(h)
        if special == "loginpage":
            self.send_text(h, LOGIN_PAGE, 200)
            return
        if special == "basic_disabled":
            self.send_text(h, "Basic Authentication is disabled on this server.", 403,
                           "text/plain; charset=utf-8",
                           headers={"X-Seraph-LoginReason": "AUTHENTICATION_DENIED"})
            return
        if not user:
            self.send_json(h, {"statusCode": 401, "message": "Unauthorized"}, 401,
                           headers={"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"})
            return

        m = re.match(r"^/rest/api/content/(\d+)$", rest)
        if m and h.command == "GET":
            page = store["pages"].get(m.group(1))
            if not page or page["deleted"]:
                self.send_json(h, {"statusCode": 404, "message": "No content found"}, 404)
                return
            expand = q.get("expand", [""])[0]
            if q.get("status", [""])[0] == "historical":
                vno = int(q.get("version", ["0"])[0])
                if vno not in page["versions"]:
                    self.send_json(h, {"statusCode": 404, "message": "No historical version"}, 404)
                    return
                self.send_json(h, content_json(store, page, expand, version_no=vno))
                return
            self.send_json(h, content_json(store, page, expand))
            return

        m = re.match(r"^/rest/api/content/(\d+)/child/(attachment|comment|page)$", rest)
        if m and h.command == "GET":
            page = store["pages"].get(m.group(1))
            if not page or page["deleted"]:
                self.send_json(h, {"statusCode": 404, "message": "No content found"}, 404)
                return
            kind = m.group(2)
            if kind == "attachment":
                items = [attachment_json(page, a) for a in page["attachments"]]
            elif kind == "comment":
                items = [comment_json(store, page, cm) for cm in page["comments"]]
            else:
                kids = [p for p in store["pages"].values()
                        if p["parent"] == page["id"] and not p["deleted"]]
                kids.sort(key=lambda p: p["id"])
                items = [content_json(store, k, q.get("expand", [""])[0]) for k in kids]
            self.send_json(h, self.paginate(h, items, q, rest))
            return

        if rest == "/rest/api/content/search" and h.command == "GET":
            try:
                found = search_pages(store, q.get("cql", [""])[0])
            except CqlError as e:
                self.send_json(h, {"statusCode": 400, "message": str(e)}, 400)
                return
            expand = q.get("expand", [""])[0]
            items = [content_json(store, p, expand) for p in found]
            out = self.paginate(h, items, q, rest)
            out["totalSize"] = len(items)
            self.send_json(h, out)
            return

        if rest == "/rest/api/search" and h.command == "GET":
            try:
                found = search_pages(store, q.get("cql", [""])[0])
            except CqlError as e:
                self.send_json(h, {"statusCode": 400, "message": str(e)}, 400)
                return
            limit = int(q.get("limit", ["25"])[0])
            results = []
            for p in found[:limit]:
                sp = store["spaces"][p["space"]]
                results.append({
                    "title": p["title"], "excerpt": strip_tags(page_text(p))[:100],
                    "url": "/pages/viewpage.action?pageId=" + p["id"],
                    "lastModified": p["versions"][p["current"]]["when"],
                    "content": {"id": p["id"], "type": p["type"], "title": p["title"]},
                    "resultGlobalContainer": {"title": sp["name"],
                                              "displayUrl": "/display/" + sp["key"]},
                })
            self.send_json(h, {"results": results, "totalSize": len(found), "_links": {}})
            return

        if rest == "/rest/api/content" and h.command == "GET":
            space = q.get("spaceKey", [None])[0]
            title = q.get("title", [None])[0]
            ptype = q.get("type", [None])[0]
            items = [p for p in store["pages"].values() if not p["deleted"]]
            if space:
                items = [p for p in items if p["space"] == space]
            if title:
                items = [p for p in items if p["title"] == title]
            if ptype:
                items = [p for p in items if p["type"] == ptype]
            items.sort(key=lambda p: p["id"])
            expand = q.get("expand", [""])[0]
            self.send_json(h, self.paginate(h, [content_json(store, p, expand) for p in items], q, rest))
            return

        if rest == "/rest/api/user/current" and h.command == "GET":
            u = user_obj(user)
            u["__user__"] = user
            self.send_json(h, u)
            return

        if rest == "/rest/api/space" and h.command == "GET":
            stype = q.get("type", [None])[0]
            items = [dict(s) for s in store["spaces"].values() if not stype or s["type"] == stype]
            items.sort(key=lambda s: s["key"])
            self.send_json(h, self.paginate(h, items, q, rest))
            return

        m = re.match(r"^/rest/api/space/([^/]+)/content/page$", rest)
        if m and h.command == "GET":
            key = urllib.parse.unquote(m.group(1))
            roots = [p for p in store["pages"].values()
                     if p["space"] == key and not p["parent"] and not p["deleted"] and p["type"] == "page"]
            roots.sort(key=lambda p: p["id"])
            self.send_json(h, self.paginate(
                h, [content_json(store, p, q.get("expand", [""])[0]) for p in roots], q, rest))
            return

        m = re.match(r"^/download/attachments/(\d+)/(.+)$", rest)
        if m and h.command == "GET":
            page = store["pages"].get(m.group(1))
            name = urllib.parse.unquote(m.group(2))
            if page and not page["deleted"]:
                for a in page["attachments"]:
                    if a["title"] == name:
                        h.send_response(200)
                        h.send_header("Content-Type", a["mediaType"])
                        h.send_header("Content-Length", str(len(a["data"])))
                        h.end_headers()
                        h.wfile.write(a["data"])
                        return
            self.send_json(h, {"statusCode": 404, "message": "No attachment"}, 404)
            return

        self.send_json(h, {"statusCode": 404, "message": "mock: no route for " + rest}, 404)
