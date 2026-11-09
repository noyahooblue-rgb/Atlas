#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""confluence_pages.py —— Confluence「AI 起草页面」工具。

confluence-pages skill 的实现：把助手整理好的文档发布到 Confluence 的
AI 专用空间里供人审核。它只做两件事：

  1. 在白名单空间（config 的 allow_spaces）内新建页面；
  2. 修改「本工具自己建的、创建人是当前账号、带 ai-authored 标签、
     且没被人改过（版本号与本地登记表一致）、也还没被批准
     （没有 ai-approved 标签）」的页面。

不改标题、不移动页面、不动标签、不删任何东西。只用 Python 标准库，
兼容 Python 3.8，Windows/Linux 通用。

命令：
  check    测试连接与认证（取当前用户）
  create   在指定父页面下新建页面（不带 --yes 只预览）
  update   修改本工具自建且未被审核冻结的页面（不带 --yes 只预览，含 diff）
  status   查看本工具登记页面的状态
  pull     把服务器当前正文转成 Markdown 存到本地，便于对比差异
  log      列出本地写操作日志

退出码：0 成功；2 参数或规则拒绝；3 认证失败；4 网络错误；
5 服务器错误；6 写操作结果不确定（POST/PUT 遇网络错误，未重试）。
"""

import argparse
import base64
import configparser
import difflib
import hashlib
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from html.parser import HTMLParser

BANNER_TEXT = (
    "本页由 AI 起草，状态：待审核。请审核后把标签 ai-pending 改为 "
    "ai-approved，或在评论中提出修改意见。"
)
BANNER_STORAGE = (
    '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>'
    + BANNER_TEXT
    + "</p></ac:rich-text-body></ac:structured-macro>"
)
LABEL_AUTHORED = "ai-authored"
LABEL_PENDING = "ai-pending"
LABEL_APPROVED = "ai-approved"

REGISTRY_FILENAME = "pages.jsonl"
OPS_LOG_FILENAME = "ops.jsonl"
RUN_STATE_FILENAME = "run_state.json"
RUN_STATE_WINDOW_SECONDS = 3600  # 没有显式运行编号时，1 小时内的写操作算同一次运行

EXIT_OK = 0
EXIT_REJECT = 2
EXIT_AUTH = 3
EXIT_NETWORK = 4
EXIT_HTTP = 5
EXIT_UNCERTAIN = 6


class ToolError(Exception):
    """带退出码的工具错误。"""

    exit_code = 1


class RejectError(ToolError):
    """规则拒绝（白名单/登记/版本/内容不合格等），不发送。"""

    exit_code = EXIT_REJECT


class AuthError(ToolError):
    """认证失败：立即停下，绝不重试。"""

    exit_code = EXIT_AUTH


class NetworkError(ToolError):
    """网络错误：GET 最多重试 1 次后仍失败。"""

    exit_code = EXIT_NETWORK


class UncertainError(ToolError):
    """POST/PUT 遇网络错误：写操作可能已生效，结果不确定，绝不自动重发。"""

    exit_code = EXIT_UNCERTAIN


class HttpError(ToolError):
    """服务器返回了非预期的状态码。"""

    exit_code = EXIT_HTTP

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 配置


def skill_dir():
    """skill 根目录（scripts/ 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_config_path():
    return os.environ.get("CONFLUENCE_W_CONFIG") or os.path.join(skill_dir(), "config.ini")


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _split_list(value):
    if not value:
        return []
    return [item.strip() for item in str(value).replace(";", ",").split(",") if item.strip()]


def load_config(path):
    """读 config.ini 并套用环境变量覆盖，返回配置字典。"""
    cfg = {
        "base_url": "",
        "token": "",
        "username": "",
        "password": "",
        "verify_ssl": True,
        "timeout": 10.0,
        "allow_system_proxy": False,
        "console_encoding": "utf-8",
        "allow_spaces": [],
        "allow_parent_ids": [],
        "max_body_bytes": 200 * 1024,
        "max_writes_per_run": 10,
        "deny_spaces": [],
        "deny_page_ids": [],
        "deny_title_keywords": [],
        "log_dir": os.path.join(skill_dir(), "logs"),
    }
    parser = None
    if path and os.path.exists(path):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding="utf-8-sig")
    elif not os.environ.get("CONFLUENCE_W_BASE_URL"):
        raise RejectError(
            "找不到配置文件：{0}\n请把 config.ini.example 复制为 config.ini 并填写，"
            "或用 --config 指定路径。".format(path)
        )
    if parser is not None:
        def get(section, key, fallback=None):
            if parser.has_option(section, key):
                return parser.get(section, key)
            return fallback

        cfg["base_url"] = (get("confluence", "base_url", "") or "").strip()
        cfg["token"] = (get("confluence", "token", "") or "").strip()
        cfg["username"] = (get("confluence", "username", "") or "").strip()
        cfg["password"] = get("confluence", "password", "") or ""
        cfg["verify_ssl"] = _as_bool(get("confluence", "verify_ssl"), True)
        cfg["allow_system_proxy"] = _as_bool(get("confluence", "allow_system_proxy"), False)
        cfg["console_encoding"] = (get("confluence", "console_encoding", "utf-8") or "utf-8").strip()
        try:
            cfg["timeout"] = float(get("confluence", "timeout", "10") or 10)
        except ValueError:
            cfg["timeout"] = 10.0
        cfg["allow_spaces"] = _split_list(get("pages", "allow_spaces", ""))
        cfg["allow_parent_ids"] = _split_list(get("pages", "allow_parent_ids", ""))
        try:
            cfg["max_body_bytes"] = int(get("limits", "max_body_bytes", str(200 * 1024)) or 200 * 1024)
            cfg["max_writes_per_run"] = int(get("limits", "max_writes_per_run", "10") or 10)
        except ValueError:
            raise RejectError("config.ini 中 [limits] 的数值填写不正确")
        cfg["deny_spaces"] = _split_list(get("deny", "spaces", ""))
        cfg["deny_page_ids"] = _split_list(get("deny", "page_ids", ""))
        cfg["deny_title_keywords"] = _split_list(get("deny", "title_keywords", ""))
        log_dir = (get("paths", "log_dir", "") or "").strip()
        if log_dir:
            if not os.path.isabs(log_dir):
                log_dir = os.path.join(skill_dir(), log_dir)
            cfg["log_dir"] = log_dir
    # 环境变量覆盖（凭据类，与 confluence-commenter 共用同一套）
    cfg["base_url"] = os.environ.get("CONFLUENCE_W_BASE_URL", cfg["base_url"]).strip()
    cfg["token"] = os.environ.get("CONFLUENCE_W_TOKEN", cfg["token"]).strip()
    cfg["username"] = os.environ.get("CONFLUENCE_W_USERNAME", cfg["username"]).strip()
    cfg["password"] = os.environ.get("CONFLUENCE_W_PASSWORD", cfg["password"])
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    return cfg


def configure_console(encoding):
    """按配置设置控制台输出编码，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding=encoding, errors="replace")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------- 敏感信息扫描
# 规则与 confluence-commenter 返工后的版本完全一致（含中文语境处理）：
# 数字类规则不用 \b 作边界——Python 3 里汉字属于 \w，数字紧贴汉字时
# 没有词边界会漏拦；统一改用「前后不是数字」的前后瞻。
# 密钥赋值规则：键名前不要求词边界（汉字紧贴键名也要拦），只要求键名前面
# 不是 ASCII 字母/数字/下划线（避免 monkey: 这类英文单词内部误命中）；
# 覆盖复合键名与中文键名，分隔符含全角 = 与 ：。


def _valid_ipv4(text):
    parts = text.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            return False
    return True


def _find_ipv4(text):
    for match in re.finditer(r"(?:\d{1,3}\.){3}\d{1,3}", text):
        if _valid_ipv4(match.group(0)):
            return match.group(0)
    return None


def _regex_finder(pattern, flags=0):
    compiled = re.compile(pattern, flags)

    def find(text):
        match = compiled.search(text)
        return match.group(0) if match else None

    return find


SENSITIVE_RULES = [
    ("IPv4 地址", _find_ipv4),
    ("IPv6 地址", _regex_finder(
        r"(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}"
        r"|[0-9A-Fa-f]{1,4}::[0-9A-Fa-f:]*"
        r"|::[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*")),
    ("带主机名的 URL", _regex_finder(r"(?:https?|ftp)://[^\s]+")),
    ("邮箱", _regex_finder(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("手机号", _regex_finder(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证号", _regex_finder(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("银行卡号", _regex_finder(r"(?<!\d)\d{16,19}(?!\d)")),
    ("密钥赋值", _regex_finder(
        r"(?<![A-Za-z0-9_])(?:api_key|apikey|access_token|accesstoken|secret_key"
        r"|client_secret|private_key|password|passwd|pwd|token|secret|key"
        r"|密码|口令|密钥)\s*[=＝:：]\s*\S+",
        re.IGNORECASE)),
    ("JDBC 连接串", _regex_finder(r"jdbc:[a-z0-9]+:", re.IGNORECASE)),
    ("私钥头", _regex_finder(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
]


def mask_secret(text):
    """把命中内容打码后展示：只留首尾各 2 个字符。"""
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


def scan_sensitive(body):
    """返回 [(规则名, 打码后的命中内容), ...]，无命中返回空列表。"""
    hits = []
    for name, finder in SENSITIVE_RULES:
        found = finder(body)
        if found:
            hits.append((name, mask_secret(found)))
    return hits


def scan_or_reject(text, what):
    """对将要写入的内容统一扫描，命中即拒绝。"""
    hits = scan_sensitive(text)
    if hits:
        detail = "；".join("{0}：{1}".format(name, masked) for name, masked in hits)
        raise RejectError(
            "{0}疑似包含敏感信息，已拒绝。命中规则 —— {1}。"
            "请修改内容本身，不要改写规避扫描。".format(what, detail))


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- HTTP 客户端


class ConfluenceClient:
    """极简 Confluence REST 客户端：只用 GET / POST / PUT 三种请求。

    写请求（POST、PUT）只出现在新建页面、加标签、更新自建页面正文
    这三处调用点上，源码测试会核对这一点。
    """

    def __init__(self, cfg):
        if not cfg["base_url"]:
            raise RejectError("未配置 base_url（config.ini 的 [confluence] base_url 或环境变量 CONFLUENCE_W_BASE_URL）")
        self.base_url = cfg["base_url"]
        self.timeout = cfg["timeout"]
        if cfg["token"]:
            self._auth = "Bearer " + cfg["token"]
        elif cfg["username"]:
            raw = "{0}:{1}".format(cfg["username"], cfg["password"]).encode("utf-8")
            self._auth = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            raise RejectError("未配置认证信息：请填写个人访问令牌（token），或用户名 + 密码")
        handlers = []
        if cfg["allow_system_proxy"]:
            handlers.append(urllib.request.ProxyHandler())
        else:
            handlers.append(urllib.request.ProxyHandler({}))  # 默认不走系统代理
        if cfg["verify_ssl"]:
            context = ssl.create_default_context()
        else:
            context = ssl._create_unverified_context()
        handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    def _request(self, method, path, payload=None):
        """发请求。认证失败立即抛 AuthError（不重试）。

        GET 网络错误最多重试 1 次；POST、PUT 一律不重试——第一次可能
        已经成功、只是响应丢了，重试会重复写。写请求遇网络错误抛
        UncertainError（退出码 6）。
        """
        url = self.base_url + path
        data = None
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        attempts = 2 if method == "GET" else 1  # GET：首次 + 最多重试 1 次；写请求只发 1 次
        last_error = None
        for _attempt in range(attempts):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    content_type = response.headers.get("Content-Type", "")
                    return self._parse(raw, content_type)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise AuthError("认证失败（HTTP {0}），已停止，不会重试。请检查令牌/账号。".format(exc.code))
                raise HttpError(exc.code, "服务器返回 HTTP {0}：{1}".format(exc.code, exc.reason))
            except (urllib.error.URLError, OSError) as exc:
                last_error = exc  # 网络错误
        if method == "GET":
            raise NetworkError("网络错误（已重试 1 次仍失败）：{0}".format(last_error))
        raise UncertainError(
            "写操作结果不确定：{0} 请求遇到网络错误（{1}）。"
            "操作可能已经在服务器上生效，请先用 status / pull 或到页面上核实，"
            "不要直接重试。".format(method, last_error))

    @staticmethod
    def _parse(raw, content_type):
        text = raw.decode("utf-8", errors="replace")
        if "text/html" in content_type and (
            "os_username" in text or "/login.action" in text or 'id="login"' in text
        ):
            raise AuthError("服务器返回了登录页，认证未通过，已停止，不会重试。")
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise ToolError("服务器返回了无法解析的响应（不是 JSON）")

    # ---- 读 ----
    def current_user(self):
        data = self._request("GET", "/rest/api/user/current")
        if isinstance(data, dict) and data.get("type") == "anonymous":
            raise AuthError("服务器返回的是匿名用户，认证未通过，已停止，不会重试。")
        return data

    def get_page(self, page_id):
        """取页面（含正文、版本、空间、创建人、标签）；不存在返回 None。"""
        try:
            return self._request(
                "GET",
                "/rest/api/content/{0}?expand=version,space,body.storage,history,metadata.labels".format(page_id),
            )
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def find_by_title(self, space_key, title):
        """同空间内按标题精确查找页面，返回结果列表（用于同名检查）。"""
        query = urllib.parse.urlencode({"spaceKey": space_key, "title": title, "type": "page"})
        data = self._request("GET", "/rest/api/content?{0}".format(query))
        if isinstance(data, dict):
            return data.get("results", []) or []
        return []

    def get_comments(self, page_id):
        data = self._request(
            "GET",
            "/rest/api/content/{0}/child/comment?expand=version,body.storage,history&limit=200".format(page_id),
        )
        if isinstance(data, dict):
            return data.get("results", []) or []
        return []

    # ---- 写（只有下面三处，源码测试会核对） ----
    def create_page(self, payload):
        return self._request("POST", "/rest/api/content", payload)

    def add_labels(self, page_id, names):
        payload = [{"prefix": "global", "name": name} for name in names]
        return self._request("POST", "/rest/api/content/{0}/label".format(page_id), payload)

    def update_page(self, page_id, payload):
        return self._request("PUT", "/rest/api/content/{0}".format(page_id), payload)


# ---------------------------------------------------------------- Markdown -> storage 转换
#
# 支持：标题 1–4 级、段落、粗体、斜体、行内代码、有序/无序列表（含嵌套）、
# 表格、代码块（转 code 宏，保留语言）、链接、引用块、分隔线。
# 所有文本做 HTML 转义；不支持的语法原样当文本输出并记入 warnings。

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _convert_inline(text, warnings):
    """行内格式转换：先把图片、行内代码摘出来保护，转义后再拼回去。"""
    images = []

    def stash_image(match):
        warnings.append("图片语法不支持，已按原文输出：{0}".format(match.group(0)))
        images.append(match.group(0))
        return "\x00I{0}\x00".format(len(images) - 1)

    codes = []

    def stash_code(match):
        codes.append(match.group(1))
        return "\x00C{0}\x00".format(len(codes) - 1)

    text = _IMAGE_RE.sub(stash_image, text)
    text = _CODE_SPAN_RE.sub(stash_code, text)
    if "~~" in text:
        warnings.append("删除线语法不支持，已按原文输出")
    text = html.escape(text, quote=True)

    def render_link(match):
        label, url = match.group(1), match.group(2)
        return '<a href="{0}">{1}</a>'.format(url, label)

    text = _LINK_RE.sub(render_link, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", text)
    text = re.sub(r"(?<![A-Za-z0-9])_([^_]+)_(?![A-Za-z0-9])", r"<em>\1</em>", text)

    def restore_code(match):
        return "<code>{0}</code>".format(html.escape(codes[int(match.group(1))], quote=True))

    def restore_image(match):
        return html.escape(images[int(match.group(1))], quote=True)

    text = re.sub(r"\x00C(\d+)\x00", restore_code, text)
    text = re.sub(r"\x00I(\d+)\x00", restore_image, text)
    return text


class _ListBlock(object):
    """列表树节点：ordered 表示有序/无序，items 是 [(文本, [子块, ...]), ...]。"""

    def __init__(self, ordered):
        self.ordered = ordered
        self.items = []


def _list_tree(items, start, indent):
    """把 (缩进, 是否有序, 文本) 序列按缩进递归组装成列表树。"""
    block = None
    i = start
    while i < len(items):
        ind, ordered, text = items[i]
        if ind < indent:
            break
        if ind > indent:
            child, i = _list_tree(items, i, ind)
            block.items[-1][1].append(child)
            continue
        if block is None:
            block = _ListBlock(ordered)
        elif block.ordered != ordered:
            break
        block.items.append((text, []))
        i += 1
    return block, i


def _render_list_block(block, warnings):
    tag = "ol" if block.ordered else "ul"
    parts = ["<{0}>".format(tag)]
    for text, children in block.items:
        parts.append("<li>")
        parts.append(_convert_inline(text, warnings))
        for child in children:
            parts.append(_render_list_block(child, warnings))
        parts.append("</li>")
    parts.append("</{0}>".format(tag))
    return "".join(parts)


def _indent_of(prefix):
    return len(prefix.replace("\t", "    "))


def _is_list_item(line):
    return _LIST_ITEM_RE.match(line) is not None


def _split_table_row(line):
    """按未转义的 | 切分表格行，返回单元格文本列表。"""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    cells = re.split(r"(?<!\\)\|", stripped)
    return [cell.replace("\\|", "|").strip() for cell in cells]


def _is_table_start(lines, i):
    if i + 1 >= len(lines):
        return False
    if "|" not in lines[i]:
        return False
    sep = lines[i + 1]
    return bool(_TABLE_SEP_RE.match(sep)) and "-" in sep


def _code_macro(lang, code):
    lang_param = ""
    if lang:
        lang_param = '<ac:parameter ac:name="language">{0}</ac:parameter>'.format(html.escape(lang, quote=True))
    safe = code.replace("]]>", "]]]]><![CDATA[>")
    return (
        '<ac:structured-macro ac:name="code">' + lang_param
        + "<ac:plain-text-body><![CDATA[" + safe + "]]></ac:plain-text-body></ac:structured-macro>"
    )


def markdown_to_storage(md_text):
    """Markdown 转 Confluence storage 格式。返回 (storage 字符串, warnings 列表)。"""
    warnings = []
    lines = md_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = []
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = _FENCE_RE.match(line)
        if fence:
            lang = fence.group(1)
            code_lines = []
            i += 1
            while i < total and lines[i].strip() != "```":
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过结尾的 ```
            blocks.append(_code_macro(lang, "\n".join(code_lines)))
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2)
            if level <= 4:
                blocks.append("<h{0}>{1}</h{0}>".format(level, _convert_inline(text, warnings)))
            else:
                warnings.append("只支持 1–4 级标题，{0} 级标题已按原文输出".format(level))
                blocks.append("<p>{0}</p>".format(_convert_inline(line, warnings)))
            i += 1
            continue
        if _HR_RE.match(line):
            blocks.append("<hr/>")
            i += 1
            continue
        if _is_table_start(lines, i):
            header = _split_table_row(lines[i])
            i += 2  # 跳过表头行与分隔行
            rows = []
            while i < total and lines[i].strip() and "|" in lines[i]:
                cells = _split_table_row(lines[i])
                if len(cells) < len(header):
                    cells += [""] * (len(header) - len(cells))
                rows.append(cells[:len(header)])
                i += 1
            parts = ["<table><tbody><tr>"]
            for cell in header:
                parts.append("<th>{0}</th>".format(_convert_inline(cell, warnings)))
            parts.append("</tr>")
            for row in rows:
                parts.append("<tr>")
                for cell in row:
                    parts.append("<td>{0}</td>".format(_convert_inline(cell, warnings)))
                parts.append("</tr>")
            parts.append("</tbody></table>")
            blocks.append("".join(parts))
            continue
        if line.lstrip().startswith(">"):
            quote_lines = []
            while i < total and lines[i].lstrip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner = "\n".join(quote_lines)
            blocks.append("<blockquote><p>{0}</p></blockquote>".format(_convert_inline(inner, warnings)))
            continue
        if _is_list_item(line):
            items = []
            while i < total and _is_list_item(lines[i]):
                match = _LIST_ITEM_RE.match(lines[i])
                indent = _indent_of(match.group(1))
                ordered = match.group(2)[0].isdigit()
                items.append((indent, ordered, match.group(3)))
                i += 1
            pos = 0
            while pos < len(items):
                block, pos = _list_tree(items, pos, items[pos][0])
                blocks.append(_render_list_block(block, warnings))
            continue
        # 普通段落：直到空行或下一个块级语法
        para_lines = [line]
        i += 1
        while i < total and lines[i].strip():
            nxt = lines[i]
            if (
                _FENCE_RE.match(nxt) or _HEADING_RE.match(nxt) or _HR_RE.match(nxt)
                or nxt.lstrip().startswith(">") or _is_list_item(nxt) or _is_table_start(lines, i)
            ):
                break
            para_lines.append(nxt)
            i += 1
        blocks.append("<p>{0}</p>".format(_convert_inline("\n".join(para_lines), warnings)))
    return "".join(blocks), warnings


# ---------------------------------------------------------------- storage -> Markdown 转换
#
# pull 用。先用正则把 code 宏摘出来（CDATA 内容不能交给 HTML 解析器），
# 其余用 html.parser 建一棵小 DOM 再渲染。info 宏渲染成引用块，
# 保证本工具生成的页面（横幅 + 正文）转换结果稳定。

_CODE_MACRO_RE = re.compile(
    r'<ac:structured-macro[^>]*ac:name="code"[^>]*>(.*?)</ac:structured-macro>', re.S)
_CODE_LANG_RE = re.compile(r'<ac:parameter ac:name="language">(.*?)</ac:parameter>', re.S)
_CODE_BODY_RE = re.compile(r"<ac:plain-text-body><!\[CDATA\[(.*)\]\]></ac:plain-text-body>", re.S)

_VOID_TAGS = {"br", "hr"}


class _Node(object):
    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs
        self.children = []


class _TreeBuilder(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = _Node("root", {})
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs))
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(_Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == tag:
                del self._stack[idx:]
                break

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def _node_text(node):
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        else:
            parts.append(_node_text(child))
    return "".join(parts)


def _render_inline(children):
    parts = []
    for child in children:
        if isinstance(child, str):
            parts.append(child)
            continue
        inner = _render_inline(child.children)
        if child.tag in ("strong", "b"):
            parts.append("**{0}**".format(inner))
        elif child.tag in ("em", "i"):
            parts.append("*{0}*".format(inner))
        elif child.tag == "code":
            parts.append("`{0}`".format(inner))
        elif child.tag == "a":
            parts.append("[{0}]({1})".format(inner, child.attrs.get("href", "")))
        elif child.tag == "br":
            parts.append("\n")
        else:
            parts.append(inner)
    return "".join(parts)


def _render_list(node, depth):
    lines = []
    ordered = node.tag == "ol"
    index = 1
    for child in node.children:
        if isinstance(child, str) or child.tag != "li":
            continue
        inline_children = [
            c for c in child.children
            if isinstance(c, str) or c.tag not in ("ul", "ol")
        ]
        text = _render_inline(inline_children).strip()
        marker = "{0}.".format(index) if ordered else "-"
        lines.append("{0}{1} {2}".format("  " * depth, marker, text))
        index += 1
        for sub in child.children:
            if not isinstance(sub, str) and sub.tag in ("ul", "ol"):
                lines.extend(_render_list(sub, depth + 1))
    return lines


def _render_table(node):
    rows = []

    def collect_rows(current):
        for child in current.children:
            if isinstance(child, str):
                continue
            if child.tag == "tr":
                rows.append(child)
            else:
                collect_rows(child)

    collect_rows(node)
    if not rows:
        return ""
    rendered = []
    for row_idx, row in enumerate(rows):
        cells = []
        for cell in row.children:
            if isinstance(cell, str) or cell.tag not in ("th", "td"):
                continue
            cells.append(_render_inline(cell.children).strip().replace("|", "\\|"))
        rendered.append("| " + " | ".join(cells) + " |")
        if row_idx == 0:
            rendered.append("| " + " | ".join(["---"] * len(cells)) + " |")
    return "\n".join(rendered)


def _render_blocks(children, code_store):
    blocks = []
    for child in children:
        if isinstance(child, str):
            if child.strip():
                blocks.append(child.strip())
            continue
        tag = child.tag
        if tag == "p":
            blocks.append(_render_inline(child.children))
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            blocks.append("{0} {1}".format("#" * level, _render_inline(child.children)))
        elif tag in ("ul", "ol"):
            blocks.append("\n".join(_render_list(child, 0)))
        elif tag == "table":
            table_md = _render_table(child)
            if table_md:
                blocks.append(table_md)
        elif tag == "blockquote":
            inner = "\n\n".join(_render_blocks(child.children, code_store))
            blocks.append("\n".join("> " + ln for ln in inner.split("\n")))
        elif tag == "hr":
            blocks.append("---")
        elif tag == "pre" and "data-coderef" in child.attrs:
            lang, code = code_store[int(child.attrs["data-coderef"])]
            blocks.append("```{0}\n{1}\n```".format(lang, code))
        elif tag == "ac:structured-macro":
            name = child.attrs.get("ac:name", "")
            if name == "info":
                inner = "\n\n".join(_render_blocks(child.children, code_store))
                blocks.append("\n".join("> " + ln for ln in inner.split("\n")))
            else:
                inner = "\n\n".join(_render_blocks(child.children, code_store))
                if inner.strip():
                    blocks.append(inner)
        elif tag in ("ac:rich-text-body", "ac:plain-text-body", "div", "section", "span"):
            blocks.extend(_render_blocks(child.children, code_store))
        else:
            inner = _render_inline(child.children)
            if inner.strip():
                blocks.append(inner)
    return [b for b in blocks if b != ""]


def storage_to_markdown(storage):
    """Confluence storage 格式转 Markdown（pull 用）。"""
    code_store = []

    def stash_macro(match):
        inner = match.group(1)
        lang_match = _CODE_LANG_RE.search(inner)
        lang = html.unescape(lang_match.group(1)) if lang_match else ""
        body_match = _CODE_BODY_RE.search(inner)
        code = body_match.group(1).replace("]]]]><![CDATA[>", "]]>") if body_match else ""
        code_store.append((lang, code))
        return '<pre data-coderef="{0}"></pre>'.format(len(code_store) - 1)

    prepared = _CODE_MACRO_RE.sub(stash_macro, storage or "")
    builder = _TreeBuilder()
    builder.feed(prepared)
    builder.close()
    blocks = _render_blocks(builder.root.children, code_store)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- 本地登记表、日志与运行计数


def registry_path(cfg):
    return os.path.join(cfg["log_dir"], REGISTRY_FILENAME)


def ops_log_path(cfg):
    return os.path.join(cfg["log_dir"], OPS_LOG_FILENAME)


def _read_jsonl(path):
    records = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        pass
    return records


def _append_jsonl(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_registry(cfg):
    """登记表是只追加的 JSONL：同一页面以最后一条记录为准。返回 {page_id: record}。"""
    folded = {}
    for record in _read_jsonl(registry_path(cfg)):
        page_id = str(record.get("page_id", ""))
        if page_id:
            folded[page_id] = record
    return folded


def append_registry(cfg, record):
    _append_jsonl(registry_path(cfg), record)


def read_ops_log(cfg):
    return _read_jsonl(ops_log_path(cfg))


def append_ops_log(cfg, record):
    _append_jsonl(ops_log_path(cfg), record)


def _run_state_path(cfg):
    return os.path.join(cfg["log_dir"], RUN_STATE_FILENAME)


def _read_run_state(cfg):
    path = _run_state_path(cfg)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return None
    return None


def current_run_id(cfg):
    """本次运行的编号：优先环境变量 CONFLUENCE_W_RUN_ID；否则 1 小时窗口内复用。"""
    env_id = os.environ.get("CONFLUENCE_W_RUN_ID", "").strip()
    if env_id:
        return env_id
    state = _read_run_state(cfg)
    now = time.time()
    if state and state.get("run_id") and now - float(state.get("updated", 0)) < RUN_STATE_WINDOW_SECONDS:
        return state["run_id"]
    return uuid.uuid4().hex


def run_write_count(cfg):
    state = _read_run_state(cfg)
    if state and state.get("run_id") == current_run_id(cfg):
        return int(state.get("count", 0))
    return 0


def bump_run_count(cfg):
    run_id = current_run_id(cfg)
    state = _read_run_state(cfg)
    count = 1
    if state and state.get("run_id") == run_id:
        count = int(state.get("count", 0)) + 1
    os.makedirs(cfg["log_dir"], exist_ok=True)
    with open(_run_state_path(cfg), "w", encoding="utf-8") as fh:
        json.dump({"run_id": run_id, "count": count, "updated": time.time()}, fh)


def check_run_limit(cfg):
    if run_write_count(cfg) >= cfg["max_writes_per_run"]:
        raise RejectError(
            "本次运行写操作已达上限 {0} 次，拒绝继续写入".format(cfg["max_writes_per_run"]))


# ---------------------------------------------------------------- 页面信息提取与检查


def parse_page_ref(ref):
    """页面参数可以是纯数字 ID，也可以是页面 URL。"""
    ref = (ref or "").strip()
    if ref.isdigit():
        return ref
    match = re.search(r"/pages/(\d+)", ref)
    if match:
        return match.group(1)
    match = re.search(r"[?&]pageId=(\d+)", ref)
    if match:
        return match.group(1)
    raise RejectError("无法从参数里解析页面 ID：{0}（请传页面 ID 或页面 URL）".format(ref))


def page_space_key(page):
    return ((page.get("space") or {}).get("key") or "")


def page_version(page):
    return (page.get("version") or {}).get("number")


def page_labels(page):
    results = (((page.get("metadata") or {}).get("labels") or {}).get("results")) or []
    return [item.get("name", "") for item in results]


def page_creator(page):
    return ((page.get("history") or {}).get("createdBy") or {})


def page_storage(page):
    return (((page.get("body") or {}).get("storage") or {}).get("value")) or ""


def check_deny(page, page_id, cfg, what):
    """拒绝清单对将要读写的页面同样生效。"""
    space_key = page_space_key(page)
    title = page.get("title") or ""
    if space_key and space_key in cfg["deny_spaces"]:
        raise RejectError("{0}所在空间 {1} 在拒绝清单（deny spaces）里".format(what, space_key))
    if str(page_id) in cfg["deny_page_ids"]:
        raise RejectError("{0}的 ID 在拒绝清单（deny page_ids）里".format(what))
    for keyword in cfg["deny_title_keywords"]:
        if keyword and keyword in title:
            raise RejectError("{0}的标题命中拒绝关键词「{1}」".format(what, keyword))


def check_title_keywords(title, cfg, what):
    for keyword in cfg["deny_title_keywords"]:
        if keyword and keyword in title:
            raise RejectError("{0}命中拒绝关键词「{1}」".format(what, keyword))


def require_allow_spaces(cfg):
    if not cfg["allow_spaces"]:
        raise RejectError(
            "未配置 AI 专用空间（config 的 allow_spaces 为空）："
            "create 和 update 一律拒绝。请先在 config.ini 的 [pages] 里配置 allow_spaces。")


def read_body_file(path, cfg):
    """读正文 Markdown 文件并检查大小上限，返回文本。"""
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    size = len(text.encode("utf-8"))
    if size > cfg["max_body_bytes"]:
        raise RejectError(
            "正文太大：{0} 字节，上限 {1} 字节（config 的 max_body_bytes）".format(size, cfg["max_body_bytes"]))
    if not text.strip():
        raise RejectError("正文文件是空的")
    return text


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _print_warnings(warnings):
    for warning in warnings:
        print("警告：{0}".format(warning), file=sys.stderr)


# ---------------------------------------------------------------- 命令：check


def cmd_check(args, cfg):
    client = ConfluenceClient(cfg)
    user = client.current_user()
    name = user.get("displayName") or user.get("username") or "未知"
    print("连接成功：当前用户 {0}（{1}），服务器 {2}".format(
        name, user.get("username", ""), cfg["base_url"]))
    if cfg["allow_spaces"]:
        print("AI 专用空间（allow_spaces）：{0}".format("、".join(cfg["allow_spaces"])))
    else:
        print("注意：allow_spaces 未配置，create / update 会被拒绝")
    return EXIT_OK


# ---------------------------------------------------------------- 命令：create


def prepare_create(args, cfg, client):
    """新建前的全部检查与转换；不合格抛 RejectError。不发任何写请求。"""
    require_allow_spaces(cfg)
    parent_id = parse_page_ref(args.parent)
    title = (args.title or "").strip()
    if not title:
        raise RejectError("标题不能为空")
    check_title_keywords(title, cfg, "新页面标题")
    parent = client.get_page(parent_id)
    if parent is None:
        raise RejectError("父页面不存在：{0}".format(parent_id))
    check_deny(parent, parent_id, cfg, "父页面")
    parent_space = page_space_key(parent)
    if parent_space not in cfg["allow_spaces"]:
        raise RejectError(
            "父页面所在空间 {0} 不在 AI 专用空间（allow_spaces：{1}）内，拒绝新建".format(
                parent_space, "、".join(cfg["allow_spaces"])))
    if cfg["allow_parent_ids"] and str(parent_id) not in cfg["allow_parent_ids"]:
        raise RejectError(
            "父页面 {0} 不在 allow_parent_ids 白名单里，拒绝新建".format(parent_id))
    md_text = read_body_file(args.file, cfg)
    scan_or_reject(title + "\n" + BANNER_TEXT + "\n" + md_text, "标题、正文与横幅")
    existing = client.find_by_title(parent_space, title)
    if existing:
        raise RejectError(
            "同一空间 {0} 内已有同名页面《{1}》（ID {2}），拒绝新建".format(
                parent_space, title, existing[0].get("id")))
    storage, warnings = markdown_to_storage(md_text)
    return {
        "parent_id": str(parent_id),
        "parent": parent,
        "parent_space": parent_space,
        "title": title,
        "md_text": md_text,
        "storage": BANNER_STORAGE + storage,
        "warnings": warnings,
    }


def cmd_create(args, cfg):
    client = ConfluenceClient(cfg)
    prepared = prepare_create(args, cfg, client)
    _print_warnings(prepared["warnings"])
    parent = prepared["parent"]
    if not args.yes:
        print("父页面：《{0}》（ID {1}，空间 {2}）".format(
            parent.get("title", ""), prepared["parent_id"], prepared["parent_space"]))
        print("新页面标题：《{0}》".format(prepared["title"]))
        print("将自动添加标签：{0}、{1}".format(LABEL_AUTHORED, LABEL_PENDING))
        print("将要写入的正文（storage 格式，顶部已自动加 AI 横幅）：")
        print("----")
        print(prepared["storage"])
        print("----")
        print("（预览只做检查，没有发送任何写请求；确认后加 --yes 执行 create）")
        return EXIT_OK
    check_run_limit(cfg)
    payload = {
        "type": "page",
        "title": prepared["title"],
        "space": {"key": prepared["parent_space"]},
        "ancestors": [{"id": prepared["parent_id"]}],
        "body": {"storage": {"value": prepared["storage"], "representation": "storage"}},
    }
    op_record = {
        "time": _now_iso(),
        "op": "create",
        "page_id": "",
        "title": prepared["title"],
        "space": prepared["parent_space"],
        "parent_id": prepared["parent_id"],
        "version": None,
        "content_hash": content_hash(prepared["md_text"]),
    }
    try:
        result = client.create_page(payload)
    except UncertainError as exc:
        op_record["status"] = "uncertain"
        append_ops_log(cfg, op_record)
        bump_run_count(cfg)
        print("错误：{0}".format(exc), file=sys.stderr)
        return EXIT_UNCERTAIN
    page_id = str(result.get("id", ""))
    if not page_id:
        raise ToolError("服务器未返回新页面 ID，无法确认新建结果")
    created = client.get_page(page_id) or result
    version = page_version(created)
    registry_record = {
        "page_id": page_id,
        "title": prepared["title"],
        "space": prepared["parent_space"],
        "parent_id": prepared["parent_id"],
        "created_at": _now_iso(),
        "last_version": version,
        "content_hash": op_record["content_hash"],
    }
    append_registry(cfg, registry_record)
    op_record["page_id"] = page_id
    op_record["version"] = version
    label_error = None
    try:
        client.add_labels(page_id, [LABEL_AUTHORED, LABEL_PENDING])
    except ToolError as exc:
        label_error = exc  # 页面已建好并已登记，标签失败单独报告，不假装整体失败
    append_ops_log(cfg, op_record)
    bump_run_count(cfg)
    if label_error is not None:
        print("页面已新建并登记（ID {0}），但添加标签失败：{1}".format(page_id, label_error),
              file=sys.stderr)
        return label_error.exit_code
    print("已新建页面：《{0}》（ID {1}，空间 {2}，版本 v{3}），标签 {4}、{5} 已添加".format(
        prepared["title"], page_id, prepared["parent_space"], version,
        LABEL_AUTHORED, LABEL_PENDING))
    return EXIT_OK


# ---------------------------------------------------------------- 命令：update


def prepare_update(args, cfg, client):
    """修改前的全部检查与转换；不合格抛 RejectError。不发任何写请求。

    检查顺序（补充修订要求）：allow_spaces 是否配置 → 页面所在空间 →
    拒绝清单 → 登记表（5a）→ 创建人（5b）→ ai-authored 标签（5c）→
    版本号未被人改过（5d）→ 没有 ai-approved 标签（5e）。
    """
    require_allow_spaces(cfg)
    page_id = parse_page_ref(args.page)
    page = client.get_page(page_id)
    registry = read_registry(cfg)
    record = registry.get(str(page_id))
    if page is None:
        if record is not None:
            raise RejectError("页面 {0} 已不存在（可能已被删除），无法更新".format(page_id))
        raise RejectError(
            "页面 {0} 不在本地登记表里，不是本工具建的页面，拒绝修改（条件 5a）".format(page_id))
    space_key = page_space_key(page)
    if space_key not in cfg["allow_spaces"]:
        raise RejectError(
            "页面所在空间 {0} 不在 AI 专用空间（allow_spaces：{1}）内，拒绝修改".format(
                space_key, "、".join(cfg["allow_spaces"])))
    check_deny(page, page_id, cfg, "该页面")
    if record is None:
        raise RejectError(
            "页面 {0} 不在本地登记表里，不是本工具建的页面，拒绝修改（条件 5a）".format(page_id))
    user = client.current_user()
    my_name = user.get("username") or user.get("key") or ""
    creator = page_creator(page)
    creator_name = creator.get("username") or creator.get("key") or ""
    if not my_name or creator_name != my_name:
        raise RejectError(
            "页面创建人是 {0}，不是当前账号 {1}，拒绝修改（条件 5b）".format(
                creator_name or "未知", my_name or "未知"))
    labels = page_labels(page)
    if LABEL_AUTHORED not in labels:
        raise RejectError(
            "页面没有 {0} 标签，不是本工具起草的页面，拒绝修改（条件 5c）".format(LABEL_AUTHORED))
    current_version = page_version(page)
    if current_version != record.get("last_version"):
        raise RejectError(
            "页面已被人修改，人的修改优先，请先 pull 查看差异"
            "（登记版本 v{0}，服务器当前版本 v{1}；条件 5d）".format(
                record.get("last_version"), current_version))
    if LABEL_APPROVED in labels:
        raise RejectError(
            "页面已有 {0} 标签，已被批准并冻结，AI 不得再改（条件 5e）".format(LABEL_APPROVED))
    md_text = read_body_file(args.file, cfg)
    title = page.get("title") or ""
    scan_or_reject(title + "\n" + BANNER_TEXT + "\n" + md_text, "标题、正文与横幅")
    storage, warnings = markdown_to_storage(md_text)
    old_md = storage_to_markdown(page_storage(page))
    new_storage = BANNER_STORAGE + storage
    new_md = storage_to_markdown(new_storage)
    diff = list(difflib.unified_diff(
        old_md.splitlines(), new_md.splitlines(),
        fromfile="服务器当前正文（v{0}）".format(current_version),
        tofile="待提交正文",
        lineterm=""))
    return {
        "page_id": str(page_id),
        "page": page,
        "record": record,
        "title": title,
        "space": space_key,
        "current_version": current_version,
        "md_text": md_text,
        "storage": new_storage,
        "warnings": warnings,
        "diff": diff,
    }


def cmd_update(args, cfg):
    client = ConfluenceClient(cfg)
    prepared = prepare_update(args, cfg, client)
    _print_warnings(prepared["warnings"])
    if not args.yes:
        print("页面：《{0}》（ID {1}，空间 {2}，当前版本 v{3}）".format(
            prepared["title"], prepared["page_id"], prepared["space"],
            prepared["current_version"]))
        print("正文差异（服务器当前正文 -> 待提交正文）：")
        print("----")
        if prepared["diff"]:
            for line in prepared["diff"]:
                print(line)
        else:
            print("（正文没有变化）")
        print("----")
        print("（预览只做检查，没有发送任何写请求；确认后加 --yes 执行 update）")
        return EXIT_OK
    check_run_limit(cfg)
    new_version = int(prepared["current_version"]) + 1
    payload = {
        "id": prepared["page_id"],
        "type": "page",
        "title": prepared["title"],  # Confluence 更新接口要求回传标题，值与原来完全一致，不改名
        "version": {"number": new_version},
        "body": {"storage": {"value": prepared["storage"], "representation": "storage"}},
    }
    op_record = {
        "time": _now_iso(),
        "op": "update",
        "page_id": prepared["page_id"],
        "title": prepared["title"],
        "space": prepared["space"],
        "version": new_version,
        "content_hash": content_hash(prepared["md_text"]),
    }
    try:
        client.update_page(prepared["page_id"], payload)
    except UncertainError as exc:
        op_record["status"] = "uncertain"
        append_ops_log(cfg, op_record)
        bump_run_count(cfg)
        print("错误：{0}".format(exc), file=sys.stderr)
        return EXIT_UNCERTAIN
    except HttpError as exc:
        if exc.status == 409:
            print(
                "错误：服务器返回 409 版本冲突，已拒绝更新。"
                "页面可能刚被人修改，请先 pull 查看差异后再决定。",
                file=sys.stderr)
            return EXIT_REJECT
        raise
    registry_record = dict(prepared["record"])
    registry_record["last_version"] = new_version
    registry_record["content_hash"] = op_record["content_hash"]
    append_registry(cfg, registry_record)
    append_ops_log(cfg, op_record)
    bump_run_count(cfg)
    print("已更新页面：《{0}》（ID {1}），版本 v{2} -> v{3}（只换了正文，标题与标签未动）".format(
        prepared["title"], prepared["page_id"], prepared["current_version"], new_version))
    return EXIT_OK


# ---------------------------------------------------------------- 命令：status


def _plain_text(storage_value):
    text = re.sub(r"<[^>]+>", "", storage_value or "")
    return html.unescape(text).strip()


def _comment_info(comment):
    version = comment.get("version") or {}
    by = version.get("by") or {}
    history = comment.get("history") or {}
    created_by = history.get("createdBy") or {}
    author = by.get("displayName") or created_by.get("displayName") or "未知用户"
    when = version.get("when") or history.get("createdDate") or ""
    body = ((comment.get("body") or {}).get("storage") or {}).get("value") or ""
    return {"author": author, "when": when, "text": _plain_text(body)[:100]}


def cmd_status(args, cfg):
    client = ConfluenceClient(cfg)
    registry = read_registry(cfg)
    records = list(registry.values())
    if getattr(args, "page", None):
        wanted = parse_page_ref(args.page)
        records = [r for r in records if str(r.get("page_id")) == wanted]
        if not records:
            print("（页面 {0} 不在本地登记表里，不是本工具建的页面）".format(wanted))
            return EXIT_OK
    visible = []
    for record in records:
        if str(record.get("space", "")) in cfg["allow_spaces"]:
            visible.append(record)
        # status 只覆盖 allow_spaces 内的登记页面，其余不列
    if not visible:
        print("（没有符合条件的登记页面；status 只列 allow_spaces 内本工具建的页面）")
        return EXIT_OK
    for record in visible:
        page_id = str(record.get("page_id"))
        page = client.get_page(page_id)
        head = "页面 {0}《{1}》（空间 {2}，登记版本 v{3}）：".format(
            page_id, record.get("title", ""), record.get("space", ""), record.get("last_version"))
        if page is None:
            print(head + "已删除")
            continue
        labels = page_labels(page)
        current_version = page_version(page)
        if LABEL_APPROVED in labels:
            status = "已批准"
        elif current_version is not None and record.get("last_version") is not None \
                and int(current_version) > int(record["last_version"]):
            status = "被人修改过（当前版本 v{0}）".format(current_version)
        elif LABEL_PENDING in labels:
            status = "待审核"
        else:
            status = "状态未知（标签缺失）"
        print(head + status)
        comments = [_comment_info(c) for c in client.get_comments(page_id)]
        if comments:
            print("  有评论（{0} 条）：".format(len(comments)))
            for info in comments:
                print("  - {0} {1}：{2}".format(info["author"], info["when"], info["text"]))
    return EXIT_OK


# ---------------------------------------------------------------- 命令：pull


def cmd_pull(args, cfg):
    client = ConfluenceClient(cfg)
    page_id = parse_page_ref(args.page)
    registry = read_registry(cfg)
    record = registry.get(str(page_id))
    if record is None:
        raise RejectError(
            "页面 {0} 不在本地登记表里，不是本工具建的页面，拒绝 pull".format(page_id))
    if str(record.get("space", "")) not in cfg["allow_spaces"]:
        raise RejectError(
            "页面所在空间 {0} 不在 AI 专用空间（allow_spaces）内，拒绝 pull".format(
                record.get("space", "")))
    page = client.get_page(page_id)
    if page is None:
        raise RejectError("页面 {0} 已不存在（可能已被删除），无法 pull".format(page_id))
    md_text = storage_to_markdown(page_storage(page))
    out_path = args.out
    if not out_path:
        out_path = os.path.join(cfg["log_dir"], "pull", "{0}.md".format(page_id))
    directory = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(md_text + "\n")
    print("已保存页面《{0}》（ID {1}，当前版本 v{2}）的 Markdown 到：{3}".format(
        page.get("title", ""), page_id, page_version(page), out_path))
    return EXIT_OK


# ---------------------------------------------------------------- 命令：log


def cmd_log(args, cfg):
    records = read_ops_log(cfg)
    if not records:
        print("（暂无写操作记录，日志文件：{0}）".format(ops_log_path(cfg)))
        return EXIT_OK
    for record in records:
        line = "{0} {1} 页面 {2}《{3}》（空间 {4}，版本 v{5}）".format(
            record.get("time", ""), record.get("op", ""), record.get("page_id", ""),
            record.get("title", ""), record.get("space", ""), record.get("version"))
        if record.get("status") == "uncertain":
            line += "（写操作结果不确定，未确认是否生效）"
        print(line)
    return EXIT_OK


# ---------------------------------------------------------------- 入口


def build_parser():
    parser = argparse.ArgumentParser(
        prog="confluence_pages.py",
        description="Confluence AI 起草页面工具（confluence-pages）",
    )
    parser.add_argument("--config", default=None, help="config.ini 路径（默认 skill 目录下的 config.ini）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="测试连接与认证")

    create_parser = sub.add_parser("create", help="在指定父页面下新建页面")
    create_parser.add_argument("--parent", required=True, help="父页面 ID 或 URL")
    create_parser.add_argument("--title", required=True, help="新页面标题")
    create_parser.add_argument("--file", required=True, help="正文 Markdown 文件")
    create_parser.add_argument("--yes", action="store_true", help="确认执行（不带只预览）")

    update_parser = sub.add_parser("update", help="修改本工具自建且未被冻结的页面")
    update_parser.add_argument("page", help="页面 ID 或 URL")
    update_parser.add_argument("--file", required=True, help="新正文 Markdown 文件")
    update_parser.add_argument("--yes", action="store_true", help="确认执行（不带只预览，含 diff）")

    status_parser = sub.add_parser("status", help="查看登记页面的状态")
    status_parser.add_argument("page", nargs="?", default=None, help="只看某个页面（ID 或 URL）")

    pull_parser = sub.add_parser("pull", help="把服务器当前正文转成 Markdown 存到本地")
    pull_parser.add_argument("page", help="页面 ID 或 URL")
    pull_parser.add_argument("--out", default=None, help="保存路径（默认 logs/pull/<页面ID>.md）")

    sub.add_parser("log", help="列出本地写操作日志")
    return parser


COMMANDS = {
    "check": cmd_check,
    "create": cmd_create,
    "update": cmd_update,
    "status": cmd_status,
    "pull": cmd_pull,
    "log": cmd_log,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    config_path = args.config or default_config_path()
    try:
        cfg = load_config(config_path)
    except ToolError as exc:
        print("错误：{0}".format(exc), file=sys.stderr)
        return exc.exit_code
    configure_console(cfg["console_encoding"])
    try:
        return COMMANDS[args.command](args, cfg)
    except ToolError as exc:
        print("错误：{0}".format(exc), file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print("错误：本地文件读写失败：{0}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
