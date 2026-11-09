#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""confluence_comment.py —— Confluence「只发评论」工具。

confluence-commenter skill 的实现：核对文档与代码后，只能在页面底部
发一条简短评论，或回复自己之前发的那条评论。不能改页面、不能删评论、
不发页面内评论。只用 Python 标准库，兼容 Python 3.8，Windows/Linux 通用。

命令：
  check    测试连接与认证（取当前用户）
  preview  生成最终评论并跑全部检查，只打印不发送
  send     真正发送，必须带 --yes
  status   查看已发评论的状态（未回复 / 有回复 / 页面已更新 / 评论已删除）
  log      列出本地发送日志

退出码：0 成功；2 参数或规则拒绝；3 认证失败；4 网络错误；
5 服务器错误；6 发送结果不确定（POST 遇网络错误，未重试）。
"""

import argparse
import base64
import configparser
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

PREFIX = "【AI 核对】"
SUFFIX_TEMPLATE = "（以上由 AI 依据当前代码核对生成{ref}；如有不同意见请直接回复本评论）"
LOG_FILENAME = "comments.jsonl"
RUN_STATE_FILENAME = "run_state.json"
RUN_STATE_WINDOW_SECONDS = 3600  # 没有显式运行编号时，1 小时内的发送算同一次运行

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
    """规则拒绝（内容不合格、防误发拦截等），不发送。"""

    exit_code = EXIT_REJECT


class AuthError(ToolError):
    """认证失败：立即停下，绝不重试。"""

    exit_code = EXIT_AUTH


class NetworkError(ToolError):
    """网络错误：GET 最多重试 1 次后仍失败。"""

    exit_code = EXIT_NETWORK


class UncertainError(ToolError):
    """POST 遇网络错误：评论可能已发到服务器，结果不确定，绝不自动重发。"""

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
        "max_body_chars": 300,
        "max_body_lines": 5,
        "max_sends_per_run": 10,
        "allow_url_hosts": [],
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
        try:
            cfg["max_body_chars"] = int(get("limits", "max_body_chars", "300") or 300)
            cfg["max_body_lines"] = int(get("limits", "max_body_lines", "5") or 5)
            cfg["max_sends_per_run"] = int(get("limits", "max_sends_per_run", "10") or 10)
        except ValueError:
            raise RejectError("config.ini 中 [limits] 的数值填写不正确")
        cfg["allow_url_hosts"] = _split_list(get("confluence", "allow_url_hosts", ""))
        cfg["deny_spaces"] = _split_list(get("deny", "spaces", ""))
        cfg["deny_page_ids"] = _split_list(get("deny", "page_ids", ""))
        cfg["deny_title_keywords"] = _split_list(get("deny", "title_keywords", ""))
        log_dir = (get("paths", "log_dir", "") or "").strip()
        if log_dir:
            if not os.path.isabs(log_dir):
                log_dir = os.path.join(skill_dir(), log_dir)
            cfg["log_dir"] = log_dir
    # 环境变量覆盖（凭据类）
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


# ---------------------------------------------------------------- HTTP 客户端


class ConfluenceClient:
    """极简 Confluence REST 客户端：只用 GET 和 POST 两种请求。"""

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

        GET 网络错误最多重试 1 次；POST 一律不重试——第一次可能已经成功、
        只是响应丢了，重试会重复发评论。POST 遇网络错误抛 UncertainError。
        """
        url = self.base_url + path
        data = None
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        attempts = 1 if method == "POST" else 2  # 首次 +（仅 GET）最多重试 1 次
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
        if method == "POST":
            raise UncertainError(
                "发送结果不确定：提交评论时遇到网络错误（{0}）。"
                "评论可能已经发到服务器上，请先用 status 或到页面上核实，不要直接重发。".format(last_error))
        raise NetworkError("网络错误（已重试 1 次仍失败）：{0}".format(last_error))

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

    def current_user(self):
        data = self._request("GET", "/rest/api/user/current")
        if isinstance(data, dict) and data.get("type") == "anonymous":
            raise AuthError("服务器返回的是匿名用户，认证未通过，已停止，不会重试。")
        return data

    def get_page(self, page_id):
        return self._request("GET", "/rest/api/content/{0}?expand=version,space".format(page_id))

    def get_content(self, content_id):
        """取任意内容（页面或评论）；不存在返回 None。"""
        try:
            return self._request("GET", "/rest/api/content/{0}?expand=version".format(content_id))
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def get_comment_replies(self, comment_id):
        data = self._request(
            "GET",
            "/rest/api/content/{0}/child/comment?expand=version,body.storage,history&limit=200".format(comment_id),
        )
        if isinstance(data, dict):
            return data.get("results", []) or []
        return []

    def post_comment(self, payload):
        return self._request("POST", "/rest/api/content", payload)


# ---------------------------------------------------------------- 评论内容规则


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


# 每条规则：(规则名, 查找函数)。命中任一条就拒绝发送。
# 注意：数字类规则不用 \b 作边界——Python 3 里汉字属于 \w，数字紧贴汉字时
# 没有词边界会漏拦；统一改用「前后不是数字」的前后瞻。
# 密钥赋值规则：键名前不要求词边界（汉字紧贴键名也要拦），只要求键名前面
# 不是 ASCII 字母/数字/下划线（避免 monkey: 这类英文单词内部误命中）；
# 覆盖复合键名与中文键名，分隔符含全角 = 与 ：。
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


# ---------------------------------------------------------------- URL 主机白名单
# 站内页面链接要能在评论里原样贴出：主机（含端口）命中白名单的 http/https
# URL 整体放行——不触发「带主机名的 URL」规则，其中出现的 IP 也不触发
# IPv4/IPv6 规则。白名单 = config 的 allow_url_hosts + base_url 的主机（含端口，
# 永远自动并入）。判定用 urllib.parse 正确解析主机，防 userinfo 等伪装；
# query 里带 token/password/session 类参数名的 URL 即使主机在白名单也不放行。

_URL_SPAN_RE = re.compile(r"(?:https?|ftp)://[^\s]+")
_URL_HOST_RULES = ("IPv4 地址", "IPv6 地址", "带主机名的 URL")
_SENSITIVE_QUERY_WORDS = ("token", "password", "session")


def _parse_host_entry(entry):
    """把白名单条目（host 或 host:port，可带协议头）解析成 (主机, 端口或 None)。"""
    entry = (entry or "").strip()
    if not entry:
        return None
    candidate = entry if "://" in entry else "//" + entry
    try:
        parts = urllib.parse.urlsplit(candidate)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    return (host.lower(), port)


def _base_url_entry(base_url):
    """base_url 的主机（含端口）永远自动算在白名单里，端口缺省按协议默认补齐。"""
    if not base_url:
        return None
    try:
        parts = urllib.parse.urlsplit(base_url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return (host.lower(), port)


def url_whitelist(cfg):
    """生效的 URL 主机白名单：[(主机, 端口或 None), ...]，含 base_url 自动条目。"""
    entries = []
    for item in (cfg or {}).get("allow_url_hosts") or []:
        parsed = _parse_host_entry(item)
        if parsed:
            entries.append(parsed)
    base = _base_url_entry((cfg or {}).get("base_url", ""))
    if base:
        entries.append(base)
    return entries


def _query_has_sensitive_param(query):
    """query 参数名（不分大小写）包含 token/password/session 任一词就算敏感。

    参数名包含这些词也算，例如 access_token、sessionid。解析不了时保守
    按「有问题」处理，不放行。
    """
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return True
    for name, _value in pairs:
        lowered = name.lower()
        for word in _SENSITIVE_QUERY_WORDS:
            if word in lowered:
                return True
    return False


def _url_whitelisted(url_text, whitelist):
    """判断一个 URL 是否命中白名单。协议只认 http/https。

    主机一律取 urlsplit 解析出的 hostname（userinfo 伪装如
    http://白名单主机@evil.com/ 的真实主机是 evil.com，不会命中）；
    端口缺省按协议默认（http 80 / https 443）归一化后比较：白名单条目
    带端口时必须端口相等，不带端口时只比主机。
    """
    try:
        parts = urllib.parse.urlsplit(url_text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if not host:
        return False
    if _query_has_sensitive_param(parts.query):
        return False
    host = host.lower()
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    for entry_host, entry_port in whitelist:
        if entry_host == host and (entry_port is None or entry_port == port):
            return True
    return False


def _scrub_whitelisted_urls(text, whitelist):
    """把命中白名单的 URL 整段替换成等长空格，供 URL/IP 三条规则扫描。

    替换后原文里只剩未放行的 URL 与裸 IP，那三条规则照旧能拦；
    其余规则仍在原文上扫，不受影响。
    """
    if not whitelist:
        return text
    chars = list(text)
    for match in _URL_SPAN_RE.finditer(text):
        if _url_whitelisted(match.group(0), whitelist):
            for i in range(match.start(), match.end()):
                chars[i] = " "
    return "".join(chars)


def scan_sensitive(body, whitelist=()):
    """返回 [(规则名, 打码后的命中内容), ...]，无命中返回空列表。

    whitelist 是 URL 主机白名单 [(主机, 端口或 None), ...]（见 url_whitelist）：
    命中白名单的 URL 整体不触发「带主机名的 URL」规则，其中出现的 IP 也不
    触发 IPv4/IPv6 规则；不在白名单的 URL 和 IP 照旧拦截。
    """
    scrubbed = _scrub_whitelisted_urls(body, whitelist)
    hits = []
    for name, finder in SENSITIVE_RULES:
        found = finder(scrubbed if name in _URL_HOST_RULES else body)
        if found:
            hits.append((name, mask_secret(found)))
    return hits


def validate_body(body, cfg):
    """校验评论正文，返回清理后的正文；不合格抛 RejectError。"""
    body = (body or "").strip("\n").strip()
    if not body:
        raise RejectError("评论正文为空")
    if len(body) > cfg["max_body_chars"]:
        raise RejectError(
            "正文太长：{0} 个字符，上限 {1} 个字符。评论只写结论，不要写细节。".format(
                len(body), cfg["max_body_chars"])
        )
    lines = body.count("\n") + 1
    if lines > cfg["max_body_lines"]:
        raise RejectError(
            "正文行数太多：{0} 行，上限 {1} 行。".format(lines, cfg["max_body_lines"])
        )
    if "```" in body:
        raise RejectError("正文里不允许出现代码块（```），只写结论性的几句话")
    hits = scan_sensitive(body, url_whitelist(cfg))
    if hits:
        detail = "；".join("{0}：{1}".format(name, masked) for name, masked in hits)
        raise RejectError("正文疑似包含敏感信息，已拒绝发送。命中规则 —— {0}".format(detail))
    return body


def validate_verdict(verdict):
    if verdict == "correct":
        raise RejectError("verdict=correct 表示与代码一致：一致的页面不需要评论，已拒绝")
    if verdict not in ("wrong", "partial"):
        raise RejectError("verdict 只能是 wrong 或 partial，收到的是：{0}".format(verdict))


CODE_REF_MAX_LEN = 64
# @ 兼容「分支@提交号」这种已有写法（仓库既有测试与用法都在用），其余只允许
# 字母、数字、点、横线、下划线、斜杠、冒号。
CODE_REF_CHARS = re.compile(r"[A-Za-z0-9.\-/_:@]+")


def validate_code_ref(code_ref):
    """校验 --code-ref：它会原样写进最终评论落款，必须限长且只含安全字符。"""
    if not code_ref:
        return
    if len(code_ref) > CODE_REF_MAX_LEN:
        raise RejectError(
            "--code-ref 太长：{0} 个字符，上限 {1} 个字符".format(len(code_ref), CODE_REF_MAX_LEN))
    if not CODE_REF_CHARS.fullmatch(code_ref):
        raise RejectError(
            "--code-ref 含有非法字符（只允许字母、数字、点、横线、下划线、斜杠、冒号、@）：{0}".format(code_ref))


def build_comment_text(body, code_ref):
    """最终评论 = 固定前缀 + 正文 + 固定落款。"""
    ref = "，代码版本：{0}".format(code_ref) if code_ref else ""
    suffix = SUFFIX_TEMPLATE.format(ref=ref)
    return PREFIX + body + "\n" + suffix


def to_storage(text):
    """纯文本转 Confluence storage 格式：逐行 HTML 转义后包成段落。"""
    paragraphs = [html.escape(line, quote=True) for line in text.split("\n")]
    return "<p>" + "</p><p>".join(paragraphs) + "</p>"


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 本地日志与运行计数


def log_path(cfg):
    return os.path.join(cfg["log_dir"], LOG_FILENAME)


def read_log(cfg):
    path = log_path(cfg)
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


def append_log(cfg, record):
    os.makedirs(cfg["log_dir"], exist_ok=True)
    with open(log_path(cfg), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


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


def run_send_count(cfg):
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


# ---------------------------------------------------------------- 页面与状态


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


def check_deny(page, page_id, cfg):
    space_key = ((page.get("space") or {}).get("key") or "")
    title = page.get("title") or ""
    if space_key and space_key in cfg["deny_spaces"]:
        raise RejectError("该页面所在空间 {0} 在拒绝清单（deny spaces）里，不能发评论".format(space_key))
    if str(page_id) in cfg["deny_page_ids"]:
        raise RejectError("该页面 ID 在拒绝清单（deny page_ids）里，不能发评论")
    for keyword in cfg["deny_title_keywords"]:
        if keyword and keyword in title:
            raise RejectError("该页面标题命中拒绝关键词「{0}」，不能发评论".format(keyword))


def _plain_text(storage_value):
    text = re.sub(r"<[^>]+>", "", storage_value or "")
    return html.unescape(text).strip()


def _reply_info(reply):
    version = reply.get("version") or {}
    by = version.get("by") or {}
    history = reply.get("history") or {}
    created_by = history.get("createdBy") or {}
    author = by.get("displayName") or created_by.get("displayName") or "未知用户"
    when = version.get("when") or history.get("createdDate") or ""
    body = ((reply.get("body") or {}).get("storage") or {}).get("value") or ""
    return {"author": author, "when": when, "text": _plain_text(body)[:100]}


def comment_status(client, record):
    """判断一条已发评论现在的状态，返回 dict。

    status 取值：未回复 / 有回复 / 页面已更新 / 评论已删除（按此优先级）。
    """
    comment = client.get_content(record["comment_id"])
    if comment is None:
        return {"status": "评论已删除", "replies": [], "page_version_now": None}
    replies = [_reply_info(r) for r in client.get_comment_replies(record["comment_id"])]
    page_version_now = None
    try:
        page = client.get_page(record["page_id"])
        page_version_now = ((page.get("version") or {}).get("number"))
    except HttpError as exc:
        if exc.status != 404:
            raise
    if replies:
        status = "有回复"
    elif page_version_now is not None and page_version_now > int(record.get("page_version", 0)):
        status = "页面已更新"
    else:
        status = "未回复"
    return {"status": status, "replies": replies, "page_version_now": page_version_now}


def is_unresolved(client, record):
    """未结 = 评论还在、且没人回复（页面是否更新不影响是否未结）。"""
    info = comment_status(client, record)
    return info["status"] in ("未回复", "页面已更新")


# ---------------------------------------------------------------- 发送前准备（preview / send 共用）


def read_body_arg(args):
    if getattr(args, "text", None) is not None:
        return args.text
    if getattr(args, "file", None):
        with open(args.file, "r", encoding="utf-8-sig") as fh:
            return fh.read()
    raise RejectError("必须用 --text 或 --file 提供评论正文")


def prepare(args, cfg, client):
    """取页面、跑全部检查、生成最终评论。返回准备结果字典；不合格抛 RejectError。"""
    validate_verdict(args.verdict)
    body = validate_body(read_body_arg(args), cfg)
    validate_code_ref(getattr(args, "code_ref", None))
    page_id = parse_page_ref(args.page)
    page = client.get_page(page_id)
    check_deny(page, page_id, cfg)
    final_text = build_comment_text(body, getattr(args, "code_ref", None))
    # 对最终评论全文（前缀 + 正文 + 落款）再统一扫一遍敏感信息：
    # code-ref 在落款里，单扫正文会漏掉它。
    hits = scan_sensitive(final_text, url_whitelist(cfg))
    if hits:
        detail = "；".join("{0}：{1}".format(name, masked) for name, masked in hits)
        raise RejectError(
            "最终评论（含落款中的代码版本）疑似包含敏感信息，已拒绝发送。命中规则 —— {0}".format(detail))
    # 幂等口径：对「页面 ID + 正文」做哈希。换一个 code-ref 不能绕过幂等；
    # 发送结果不确定（status=uncertain）的记录也写了同一个哈希，同样算已发。
    digest = content_hash("{0}\n{1}".format(page_id, body))
    records = read_log(cfg)
    for record in records:
        if record.get("page_id") == str(page_id) and record.get("content_hash") == digest:
            if record.get("status") == "uncertain":
                raise RejectError(
                    "同样的评论已有一条发送结果不确定的记录（幂等拦截）：页面 {0}。"
                    "请先用 status 或到页面上核实，不要直接重发".format(page_id))
            raise RejectError("同样的评论已经发过（幂等拦截）：页面 {0}，评论 ID {1}".format(
                page_id, record.get("comment_id")))
    if run_send_count(cfg) >= cfg["max_sends_per_run"]:
        raise RejectError("本次运行发送数已达上限 {0} 条，拒绝继续发送".format(cfg["max_sends_per_run"]))
    parent_id = None
    page_records = [r for r in records if r.get("page_id") == str(page_id)]
    unresolved = None
    for record in page_records:
        if not record.get("comment_id"):
            continue  # 发送结果不确定的记录没有评论 ID，无法查状态；同内容已由幂等拦截
        if is_unresolved(client, record):
            unresolved = record  # 取最近一条未结评论
    if unresolved is not None:
        if not getattr(args, "followup", False):
            raise RejectError(
                "该页面已有一条未结的本工具评论（评论 ID {0}），拒绝再发新评论；"
                "确需跟进请用 --followup 作为对它的回复发出".format(unresolved.get("comment_id"))
            )
        parent_id = str(unresolved["comment_id"])
    return {
        "page_id": str(page_id),
        "page": page,
        "body": body,
        "final_text": final_text,
        "storage": to_storage(final_text),
        "content_hash": digest,
        "parent_id": parent_id,
    }


# ---------------------------------------------------------------- 命令实现


def cmd_check(args, cfg):
    client = ConfluenceClient(cfg)
    user = client.current_user()
    name = user.get("displayName") or user.get("username") or "未知"
    print("连接成功：当前用户 {0}（{1}），服务器 {2}".format(
        name, user.get("username", ""), cfg["base_url"]))
    return EXIT_OK


def cmd_preview(args, cfg):
    client = ConfluenceClient(cfg)
    prepared = prepare(args, cfg, client)
    page = prepared["page"]
    space_key = ((page.get("space") or {}).get("key") or "")
    version = ((page.get("version") or {}).get("number"))
    print("页面：{0}（ID {1}，空间 {2}，当前版本 v{3}）".format(
        page.get("title", ""), prepared["page_id"], space_key, version))
    print("结论：{0}".format(args.verdict))
    if prepared["parent_id"]:
        print("形式：作为评论 {0} 的回复发出（--followup）".format(prepared["parent_id"]))
    print("将要发送的评论：")
    print("----")
    print(prepared["final_text"])
    print("----")
    print("（preview 只做检查和预览，没有发送任何内容）")
    return EXIT_OK


def cmd_send(args, cfg):
    if not args.yes:
        print("拒绝发送：send 必须带 --yes。", file=sys.stderr)
        print("请先用 preview 生成评论给用户确认，确认后再 send --yes。", file=sys.stderr)
        return EXIT_REJECT
    client = ConfluenceClient(cfg)
    prepared = prepare(args, cfg, client)
    page = prepared["page"]
    payload = {
        "type": "comment",
        "container": {"id": prepared["page_id"], "type": "page"},
        "body": {"storage": {"value": prepared["storage"], "representation": "storage"}},
    }
    if prepared["parent_id"]:
        payload["ancestors"] = [{"id": prepared["parent_id"]}]
    record = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "page_id": prepared["page_id"],
        "title": page.get("title", ""),
        "space": ((page.get("space") or {}).get("key") or ""),
        "page_version": ((page.get("version") or {}).get("number")),
        "comment_id": "",
        "content_hash": prepared["content_hash"],
        "verdict": args.verdict,
        "code_ref": getattr(args, "code_ref", None) or "",
    }
    try:
        result = client.post_comment(payload)
    except UncertainError as exc:
        # 结果不确定也记一行日志：幂等要把它算进去，防止核实前重复发送。
        record["status"] = "uncertain"
        append_log(cfg, record)
        bump_run_count(cfg)
        print("错误：{0}".format(exc), file=sys.stderr)
        return EXIT_UNCERTAIN
    comment_id = str(result.get("id", ""))
    if not comment_id:
        raise ToolError("服务器未返回评论 ID，无法确认发送结果")
    record["comment_id"] = comment_id
    append_log(cfg, record)
    bump_run_count(cfg)
    print("已发送：页面《{0}》（ID {1}）评论 ID {2}".format(
        record["title"], record["page_id"], comment_id))
    return EXIT_OK


def cmd_status(args, cfg):
    client = ConfluenceClient(cfg)
    records = read_log(cfg)
    if getattr(args, "page", None):
        wanted = parse_page_ref(args.page)
        records = [r for r in records if r.get("page_id") == wanted]
    if not records:
        print("（没有符合条件的发送记录）")
        return EXIT_OK
    for record in records:
        if record.get("status") == "uncertain":
            print("页面 {0}《{1}》：发送结果不确定（{2} 发送时网络错误，未确认评论 ID），"
                  "请到页面上核实".format(
                      record.get("page_id"), record.get("title", ""), record.get("time", "")))
            continue
        info = comment_status(client, record)
        line = "页面 {0}《{1}》 评论 {2}（verdict={3}，发送于 {4}，发送时页面版本 v{5}）：{6}".format(
            record.get("page_id"), record.get("title", ""), record.get("comment_id"),
            record.get("verdict", ""), record.get("time", ""), record.get("page_version", ""),
            info["status"])
        if info["status"] == "页面已更新":
            line += "（当前页面版本 v{0}）".format(info["page_version_now"])
        print(line)
        for reply in info["replies"]:
            print("  - {0} {1}：{2}".format(reply["author"], reply["when"], reply["text"]))
    return EXIT_OK


def cmd_log(args, cfg):
    records = read_log(cfg)
    if not records:
        print("（暂无发送记录，日志文件：{0}）".format(log_path(cfg)))
        return EXIT_OK
    for record in records:
        line = "{0} 页面 {1}《{2}》 评论 {3} verdict={4} code-ref={5}".format(
            record.get("time", ""), record.get("page_id"), record.get("title", ""),
            record.get("comment_id"), record.get("verdict", ""), record.get("code_ref", ""))
        if record.get("status") == "uncertain":
            line += "（发送结果不确定，未确认评论 ID）"
        print(line)
    return EXIT_OK


# ---------------------------------------------------------------- 入口


def build_parser():
    parser = argparse.ArgumentParser(
        prog="confluence_comment.py",
        description="Confluence 只发评论工具（confluence-commenter）",
    )
    parser.add_argument("--config", default=None, help="config.ini 路径（默认 skill 目录下的 config.ini）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="测试连接与认证")

    def add_content_args(p):
        p.add_argument("page", help="页面 ID 或页面 URL")
        p.add_argument("--verdict", required=True, help="wrong 或 partial（correct 不发评论）")
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--text", default=None, help="评论正文（直接传文本）")
        group.add_argument("--file", default=None, help="评论正文文件（Markdown）")
        p.add_argument("--code-ref", default=None, help="代码版本说明，如分支名或提交号，可选")
        p.add_argument("--followup", action="store_true",
                       help="页面已有未结的本工具评论时，作为对它的回复发出")

    add_content_args(sub.add_parser("preview", help="生成最终评论并检查，只打印不发送"))
    send_parser = sub.add_parser("send", help="发送评论（必须带 --yes）")
    add_content_args(send_parser)
    send_parser.add_argument("--yes", action="store_true", help="确认发送（不带则拒绝）")

    status_parser = sub.add_parser("status", help="查看已发评论的状态")
    status_parser.add_argument("--page", default=None, help="只看某个页面（ID 或 URL）")

    sub.add_parser("log", help="列出本地发送日志")
    return parser


COMMANDS = {
    "check": cmd_check,
    "preview": cmd_preview,
    "send": cmd_send,
    "status": cmd_status,
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
