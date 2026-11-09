#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
confluence.py —— 只读访问 Confluence Server / Data Center 的命令行工具
纯 Python 标准库实现（3.8+），不需要 pip 安装任何第三方包。

能力：
  * 页面正文 → Markdown（表格/代码块/列表/提示框/任务列表/合并单元格）
  * 页面图片、附件下载（并发 + 按版本缓存，未变化不重复下载）
  * 附件内容提取：Word(docx/doc)、Excel(xlsx/xls/csv)、PPT(pptx/ppt)、PDF、Visio(vsdx)、
    文本/代码/HTML/邮件(eml)/压缩包目录；文档内嵌图片导出成文件供多模态模型查看
  * 扫描版 PDF：导出页面图片（JPEG/PNG）供识图
  * 评论、标签、面包屑、历史版本、子页面树、空间、CQL 搜索、递归导出整棵页面树
  * 认证：用户名密码(Basic) / 个人访问令牌(PAT) / 浏览器 Cookie / 表单登录（自动回退）

用法示例：
  python confluence.py check
  python confluence.py page "http://IP:8090/pages/viewpage.action?pageId=123456"
  python confluence.py page 123456 --recursive --depth 2
  python confluence.py search "支付 接口 规范" --space ABC
  python confluence.py find --term 支付 --term TXN001 --term 订单表 --space ABC
  python confluence.py stale --file refs.txt
  python confluence.py children 123456 --depth 2
  python confluence.py extract D:\\docs\\需求.docx
运行 python confluence.py -h 查看全部命令。
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as futures
import configparser
import csv
import datetime as dt
import email
import email.policy
import html as htmlmod
import http.cookiejar
import io
import json
import os
import posixpath
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from html.parser import HTMLParser

VERSION = "1.0.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
VERBOSE = False

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tif", ".tiff", ".ico", ".emf", ".wmf"}
VIEWABLE_IMG = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".json", ".xml", ".yaml", ".yml", ".sql", ".log", ".ini", ".conf", ".cfg",
    ".properties", ".java", ".py", ".js", ".ts", ".jsx", ".tsx", ".vue", ".sh", ".bat", ".cmd", ".ps1",
    ".go", ".c", ".cpp", ".h", ".hpp", ".cs", ".kt", ".scala", ".rb", ".php", ".pl", ".lua", ".r",
    ".css", ".less", ".scss", ".toml", ".gradle", ".wsdl", ".xsd", ".proto", ".graphql", ".rst", ".adoc",
    ".tex", ".dtd", ".plantuml", ".puml", ".mmd", ".drawio", ".bpmn", ".csv", ".tsv",
}


# =============================================================================
# 0. 通用工具
# =============================================================================

def setup_console(encoding: str) -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding=encoding, errors="replace")
        except Exception:
            pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def vlog(msg: str) -> None:
    if VERBOSE:
        log("[debug] " + msg)


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_name(name: str, maxlen: int = 80) -> str:
    """生成 Windows 安全的文件名（去非法字符、避开保留名、限制长度防止路径超 260）。"""
    name = _BAD_CHARS.sub("_", name or "").strip().rstrip(". ")
    stem, ext = os.path.splitext(name)
    if len(ext) > 12:
        stem, ext = name, ""
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    if len(stem) + len(ext) > maxlen:
        stem = stem[: max(1, maxlen - len(ext))]
    return (stem + ext) or "_"


def human_size(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def decode_bytes(b: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1", "replace")


def read_text_file(path: str) -> str:
    with open(path, "rb") as f:
        return decode_bytes(f.read())


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def md_cell(s: str) -> str:
    s = (s or "").replace("\r", "").strip()
    s = s.replace("|", "\\|").replace("\n", "<br>")
    return s


def md_table(rows, header=True) -> str:
    rows = [list(r) for r in rows if r is not None]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    if ncol == 0:
        return ""
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    lines = []
    head = rows[0] if header else [f"列{i + 1}" for i in range(ncol)]
    body = rows[1:] if header else rows
    lines.append("| " + " | ".join(md_cell(c) for c in head) + " |")
    lines.append("|" + "---|" * ncol)
    for r in body:
        lines.append("| " + " | ".join(md_cell(c) for c in r) + " |")
    return "\n".join(lines)


def image_size(path: str):
    """纯标准库读取 PNG/JPEG/GIF/BMP/WEBP 宽高。"""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head[:2] == b"BM":
                w, h = struct.unpack("<ii", head[18:26])
                return w, abs(h)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                f.seek(12)
                chunk = f.read(30)
                if chunk[:4] == b"VP8X":
                    w = 1 + int.from_bytes(chunk[12:15], "little")
                    h = 1 + int.from_bytes(chunk[15:18], "little")
                    return w, h
                if chunk[:4] == b"VP8 ":
                    w, h = struct.unpack("<HH", chunk[14:18])
                    return w & 0x3FFF, h & 0x3FFF
                if chunk[:4] == b"VP8L":
                    b = chunk[9:13]
                    w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                    h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                    return w, h
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    m = f.read(2)
                    if len(m) < 2 or m[0] != 0xFF:
                        return None
                    marker = m[1]
                    while marker == 0xFF:
                        marker = f.read(1)[0]
                    if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                        continue
                    seglen = struct.unpack(">H", f.read(2))[0]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        data = f.read(5)
                        h, w = struct.unpack(">HH", data[1:5])
                        return w, h
                    f.seek(seglen - 2, 1)
    except Exception:
        return None
    return None


def write_png(path: str, width: int, height: int, color_type: int, bit_depth: int,
              raw: bytes = None, palette: bytes = None, filtered_zlib: bytes = None) -> bool:
    """把原始像素写成 PNG。filtered_zlib: 已经是 PNG 行过滤 + zlib 压缩的数据（PDF Predictor>=10 时可直接复用）。"""
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    stride = (width * channels * bit_depth + 7) // 8
    if filtered_zlib is None:
        if raw is None or len(raw) < stride * height:
            if raw is None or len(raw) < stride:
                return False
            height = len(raw) // stride
        rows = bytearray()
        for i in range(height):
            rows += b"\x00" + raw[i * stride:(i + 1) * stride]
        idat = zlib.compress(bytes(rows), 6)
    else:
        idat = filtered_zlib

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0))
    if palette:
        png += chunk(b"PLTE", palette)
    png += chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    return True


# =============================================================================
# 1. 配置
# =============================================================================

DEFAULTS = {
    "base_url": "",
    "username": "",
    "password": "",
    "token": "",
    "cookie": "",
    "auth_mode": "auto",
    "verify_ssl": "true",
    "ca_file": "",
    "proxy": "",
    "timeout": "30",
    "download_timeout": "180",
    "workers": "4",
    "output_dir": "",
    "max_attachment_mb": "50",
    "skip_extensions": ".mp4,.avi,.mov,.mkv,.wmv,.flv,.mp3,.wav,.iso,.exe,.msi,.dmg,.apk,.ipa",
    "max_print_chars": "20000",
    "excel_preview_rows": "100",
    "excel_max_rows": "20000",
    "pdf_max_pages": "500",
    "max_doc_images": "60",
    "min_image_px": "48",
    "legacy_office": "auto",
    "max_recursive_pages": "50",
    "console_encoding": "utf-8",
    "deny_spaces": "",
    "deny_page_ids": "",
    "deny_title_keywords": "",
    "deny_ancestor_ids": "",
}

ENV_MAP = {
    "base_url": "CONFLUENCE_BASE_URL",
    "username": "CONFLUENCE_USERNAME",
    "password": "CONFLUENCE_PASSWORD",
    "token": "CONFLUENCE_TOKEN",
    "cookie": "CONFLUENCE_COOKIE",
    "output_dir": "CONFLUENCE_OUTPUT_DIR",
    "console_encoding": "CONFLUENCE_CONSOLE_ENCODING",
}


class Config:
    def __init__(self, values: dict, path: str):
        self.v = values
        self.path = path

    def __getattr__(self, k):
        if k in ("v", "path"):
            raise AttributeError(k)
        return self.v.get(k, DEFAULTS.get(k, ""))

    def int(self, k):
        try:
            return int(float(self.v.get(k, DEFAULTS.get(k, "0"))))
        except Exception:
            return int(DEFAULTS.get(k, "0") or 0)

    def bool(self, k):
        return str(self.v.get(k, DEFAULTS.get(k, ""))).strip().lower() in ("1", "true", "yes", "on", "是")

    def list(self, k):
        return [x.strip().lower() for x in str(self.v.get(k, "")).split(",") if x.strip()]


def _is_placeholder(val: str) -> bool:
    return (not val) or ("填" in val) or (val.startswith("<") and val.endswith(">"))


def load_config(explicit: str = None) -> Config:
    cands = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("CONFLUENCE_CONFIG"):
        cands.append(os.environ["CONFLUENCE_CONFIG"])
    cands += [
        os.path.join(SKILL_DIR, "config.ini"),
        os.path.join(SCRIPT_DIR, "config.ini"),
        os.path.join(os.getcwd(), "config.ini"),
        os.path.join(os.path.expanduser("~"), ".confluence-reader", "config.ini"),
    ]
    values = dict(DEFAULTS)
    used = ""
    for p in cands:
        if p and os.path.isfile(p):
            cp = configparser.ConfigParser(interpolation=None)
            raw = open(p, "rb").read()
            cp.read_string(decode_bytes(raw))  # 兼容记事本保存的 UTF-8 BOM / GBK
            for sec in cp.sections():
                for k, val in cp.items(sec):
                    values[k.strip().lower()] = val.strip().strip('"').strip("'")
            used = p
            break
    for k, env in ENV_MAP.items():
        if os.environ.get(env):
            values[k] = os.environ[env]
    for k in ("base_url", "username", "password", "token", "cookie"):
        if _is_placeholder(values.get(k, "")):
            values[k] = ""
    return Config(values, used)


# =============================================================================
# 2. HTTP 客户端（认证 / 重试 / 分页 / 下载）
# =============================================================================

class AuthError(Exception):
    pass


class HttpError(Exception):
    def __init__(self, code, url, msg=""):
        super().__init__(f"HTTP {code}: {msg}  [{url}]")
        self.code = code
        self.url = url
        self.msg = msg


class NetError(Exception):
    pass


class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.base_url:
            raise SystemExit(
                "【配置未填写】请编辑 config.ini，填写 base_url（如 http://10.1.2.3:8090）以及用户名/密码或令牌。\n"
                f"当前读取的配置文件: {cfg.path or '未找到 config.ini'}"
            )
        self.base = cfg.base_url.strip().rstrip("/")
        if not re.match(r"^https?://", self.base):
            self.base = "http://" + self.base
        sp = urllib.parse.urlsplit(self.base)
        self.root = f"{sp.scheme}://{sp.netloc}"
        self.ctx = sp.path.rstrip("/")
        self.timeout = cfg.int("timeout") or 30
        self.dl_timeout = cfg.int("download_timeout") or 180
        handlers = []
        proxy = cfg.proxy.strip()
        # 默认不走系统代理：Windows 会从注册表读 IE 代理，访问内网 IP 时常因此失败
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
        if self.base.startswith("https"):
            if not cfg.bool("verify_ssl"):
                ctx = ssl._create_unverified_context()
            elif cfg.ca_file:
                ctx = ssl.create_default_context(cafile=cfg.ca_file)
            else:
                ctx = ssl.create_default_context()
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.jar = http.cookiejar.CookieJar()
        handlers.append(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener = urllib.request.build_opener(*handlers)
        self.headers = {
            "User-Agent": f"confluence-reader/{VERSION} (python-urllib)",
            "Accept": "application/json",
            "X-Atlassian-Token": "no-check",
        }
        self.mode = (cfg.auth_mode or "auto").lower()
        self._auth_lock = threading.Lock()
        self._auth_failed = None
        self._session_tried = False
        self._session_ready = False
        mode = self.mode
        if mode == "auto":
            if cfg.token:
                mode = "token"
            elif cfg.cookie:
                mode = "cookie"
            elif cfg.username and cfg.password:
                mode = "basic"
            else:
                mode = "anonymous"
        self.effective_mode = mode
        if mode == "token":
            if not cfg.token:
                raise SystemExit("auth_mode=token 但 token 为空")
            self.headers["Authorization"] = "Bearer " + cfg.token
        elif mode == "basic":
            if not (cfg.username and cfg.password):
                raise SystemExit("auth_mode=basic 需要 username 和 password")
            raw = f"{cfg.username}:{cfg.password}".encode("utf-8")
            self.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        elif mode == "cookie":
            if not cfg.cookie:
                raise SystemExit("auth_mode=cookie 但 cookie 为空")
            self.headers["Cookie"] = cfg.cookie
        elif mode == "session":
            if not (cfg.username and cfg.password):
                raise SystemExit("auth_mode=session 需要 username 和 password")

    # ---- URL 工具 ----
    def url(self, path_or_url: str, params: dict = None) -> str:
        if re.match(r"^https?://", path_or_url):
            u = path_or_url
        elif self.ctx and path_or_url.startswith(self.ctx + "/"):
            u = self.root + path_or_url
        else:
            u = self.base + (path_or_url if path_or_url.startswith("/") else "/" + path_or_url)
        if params:
            q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            u += ("&" if "?" in u else "?") + q
        return u

    def html_url(self, src: str) -> str:
        """HTML 中的链接/图片地址（以 / 开头的已包含 context path）。"""
        if re.match(r"^https?://", src):
            return src
        if src.startswith("/"):
            return self.root + src
        return urllib.parse.urljoin(self.base + "/", src)

    def norm_key(self, url_or_path: str) -> str:
        """附件地址归一化：去掉主机、context path、查询串，缩略图映射到原图。"""
        p = urllib.parse.unquote(urllib.parse.urlsplit(url_or_path).path)
        if self.ctx and p.startswith(self.ctx + "/"):
            p = p[len(self.ctx):]
        return p.replace("/download/thumbnails/", "/download/attachments/")

    def is_local(self, url: str) -> bool:
        if not re.match(r"^https?://", url):
            return True
        return urllib.parse.urlsplit(url).netloc.lower() == urllib.parse.urlsplit(self.root).netloc.lower()

    # ---- 底层请求 ----
    def _explain_auth(self, code, headers, body: str) -> str:
        reason = (headers.get("X-Seraph-LoginReason") or "") if headers else ""
        denied = (headers.get("X-Authentication-Denied-Reason") or "") if headers else ""
        if "CAPTCHA" in denied.upper() or "AUTHENTICATION_DENIED" in reason:
            return ("账号触发了登录验证码（多次登录失败导致）。请先用浏览器登录一次 Confluence 并输入验证码，"
                    "之后再运行。为避免账号被锁定，本工具已停止重试。")
        if "AUTHENTICATED_FAILED" in reason or code == 401:
            return ("认证失败（HTTP 401）：用户名/密码或令牌不正确，或该账号无 API 访问权限。"
                    "为避免连续失败导致账号锁定，本工具已停止重试，请检查 config.ini 后再试。")
        return f"无权限访问（HTTP {code}）：{body[:200]}"

    def _session_login(self):
        """表单登录 /dologin.action（当服务器禁用了 Basic 认证时使用）。"""
        with self._auth_lock:
            if self._session_ready:
                return
            if self._session_tried:
                raise AuthError("表单登录失败")
            self._session_tried = True
            self.headers.pop("Authorization", None)
            data = urllib.parse.urlencode({
                "os_username": self.cfg.username, "os_password": self.cfg.password,
                "login": "Log in", "os_destination": "",
            }).encode()
            h = dict(self.headers)
            h["Content-Type"] = "application/x-www-form-urlencoded"
            h["Accept"] = "text/html,*/*"
            req = urllib.request.Request(self.base + "/dologin.action", data=data, headers=h, method="POST")
            try:
                with self.opener.open(req, timeout=self.timeout) as r:
                    reason = r.headers.get("X-Seraph-LoginReason", "")
                    r.read(1024)
            except urllib.error.HTTPError as e:
                reason = e.headers.get("X-Seraph-LoginReason", "") if e.headers else ""
                if e.code in (401, 403):
                    self._auth_failed = self._explain_auth(e.code, e.headers, "")
                    raise AuthError(self._auth_failed)
            if "FAILED" in reason or "DENIED" in reason:
                self._auth_failed = self._explain_auth(401, {"X-Seraph-LoginReason": reason}, "")
                raise AuthError(self._auth_failed)
            self._session_ready = True
            vlog("表单登录完成")

    def open(self, url: str, data=None, headers=None, timeout=None, retries=3, method=None):
        if self._auth_failed:
            raise AuthError(self._auth_failed)
        if self.effective_mode == "session" and not self._session_ready:
            self._session_login()
        last = None
        for attempt in range(retries + 1):
            h = dict(self.headers)
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            vlog(f"GET {url}")
            try:
                return self.opener.open(req, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as e:
                try:
                    body = e.read(4000).decode("utf-8", "replace")
                except Exception:
                    body = ""
                reason = (e.headers.get("X-Seraph-LoginReason") or "") if e.headers else ""
                if e.code == 401 or (e.code == 403 and ("DENIED" in reason or "FAILED" in reason)):
                    # 服务器关闭了 Basic 认证 → 自动改用表单登录（只尝试一次）
                    if (self.mode == "auto" and self.effective_mode == "basic" and not self._session_tried
                            and re.search(r"basic auth\w*.{0,40}(disabled|not allowed)", body, re.I | re.S)):
                        log("服务器禁用了 Basic 认证，改用表单登录…")
                        self.effective_mode = "session"
                        self._session_login()
                        continue
                    self._auth_failed = self._explain_auth(e.code, e.headers, body)
                    raise AuthError(self._auth_failed)
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    wait = 1.5 * (attempt + 1)
                    try:
                        wait = max(wait, float(e.headers.get("Retry-After", 0)))
                    except Exception:
                        pass
                    time.sleep(min(wait, 20))
                    last = e
                    continue
                msg = body
                try:
                    msg = json.loads(body).get("message", body)
                except Exception:
                    msg = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))[:300]
                raise HttpError(e.code, url, msg)
            except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise NetError(
                    f"网络错误：{getattr(e, 'reason', e)}  [{url}]\n"
                    "排查：1) base_url 的 IP/端口是否正确、浏览器能否打开；2) 若必须走代理，在 config.ini 填 proxy；"
                    "3) https 自签证书可设 verify_ssl = false。"
                )
        raise NetError(str(last))

    def get_json(self, path: str, params: dict = None, retries=3):
        url = self.url(path, params)
        with self.open(url, retries=retries) as r:
            ctype = r.headers.get("Content-Type", "")
            raw = r.read()
            user = r.headers.get("X-AUSERNAME", "")
        if "json" not in ctype.lower():
            txt = raw[:3000].decode("utf-8", "replace").lower()
            if "login" in txt or "os_username" in txt:
                raise AuthError("服务器返回了登录页面：认证未生效（可能需要 SSO/Cookie 方式，见 config.ini 的 cookie 说明）")
            raise HttpError(200, url, f"返回的不是 JSON（Content-Type: {ctype}）")
        d = json.loads(raw.decode("utf-8"))
        if isinstance(d, dict):
            d["__user__"] = user
        return d

    def paged(self, path: str, params: dict = None, limit: int = 100, max_items: int = None):
        start = 0
        n = 0
        while True:
            p = dict(params or {})
            p.update(start=start, limit=limit)
            d = self.get_json(path, p)
            results = d.get("results", []) if isinstance(d, dict) else []
            for r in results:
                yield r
                n += 1
                if max_items and n >= max_items:
                    return
            if not results:
                return
            nxt = (d.get("_links") or {}).get("next")
            if not nxt and len(results) < limit:
                return
            start += len(results)

    def download(self, url: str, dest: str, max_bytes: int = 0) -> int:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        size = 0
        with self.open(url, headers={"Accept": "*/*"}, timeout=self.dl_timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    size += len(chunk)
                    if max_bytes and size > max_bytes:
                        f.close()
                        os.remove(tmp)
                        raise HttpError(413, url, f"文件超过大小上限 {human_size(max_bytes)}")
                    f.write(chunk)
        if "text/html" in ctype.lower() and size < 200000 and not dest.lower().endswith((".html", ".htm")):
            head = open(tmp, "rb").read(5000).lower()
            if b"os_username" in head or b"login" in head:
                os.remove(tmp)
                raise AuthError("下载时被重定向到登录页（认证未生效）")
        os.replace(tmp, dest)
        return size


# =============================================================================
# 3. 页面 ID 解析（数字 / viewpage / display / tiny link / spaces/pages）
# =============================================================================

def tiny_to_id(tiny: str) -> int:
    s = tiny.ljust(11, "A").replace("_", "+").replace("-", "/") + "="
    return int.from_bytes(base64.b64decode(s), "little")


def resolve_page_id(client: Client, ref: str) -> str:
    ref = ref.strip().strip('"').strip("'")
    if ref.isdigit():
        return ref
    sp = urllib.parse.urlsplit(ref)
    q = urllib.parse.parse_qs(sp.query)
    for key in ("pageId", "pageid", "contentId"):
        if key in q and q[key][0].isdigit():
            return q[key][0]
    path = sp.path
    m = re.search(r"/pages/(\d+)(?:/|$)", path)
    if m:
        return m.group(1)
    m = re.search(r"/x/([A-Za-z0-9_\-]+)", path)
    if m:
        try:
            pid = str(tiny_to_id(m.group(1)))
            client.get_json(f"/rest/api/content/{pid}", {"expand": "version"})
            return pid
        except Exception:
            with client.open(client.url(ref), headers={"Accept": "text/html"}) as r:
                final = r.geturl()
            if final != ref and "/x/" not in final:
                return resolve_page_id(client, final)
            raise ValueError(f"无法解析短链接 {ref}")
    m = re.search(r"/display/([^/]+)/(\d{4})/(\d{2})/(\d{2})/([^/?#]+)", path)
    if m:
        space, y, mo, d, title = m.groups()
        r = client.get_json("/rest/api/content", {"spaceKey": urllib.parse.unquote(space), "type": "blogpost",
                                                    "title": urllib.parse.unquote_plus(title),
                                                    "postingDay": f"{y}-{mo}-{d}"})
        if r.get("results"):
            return r["results"][0]["id"]
    m = re.search(r"/display/([^/]+)/([^/?#]+)", path)
    if m:
        space, title = urllib.parse.unquote(m.group(1)), urllib.parse.unquote_plus(m.group(2))
        r = client.get_json("/rest/api/content", {"spaceKey": space, "title": title, "type": "page"})
        if r.get("results"):
            return r["results"][0]["id"]
        raise ValueError(f"在空间 {space} 中找不到标题为「{title}」的页面")
    m = re.search(r"/download/(?:attachments|thumbnails)/(\d+)/", path)
    if m:
        return m.group(1)
    raise ValueError(f"无法从「{ref}」解析出页面 ID（支持：数字ID、viewpage.action?pageId=、/display/空间/标题、/x/短链）")


# =============================================================================
# 3.5 排除清单（config 的 deny_* 与命令行 --deny-file）
# =============================================================================

class DeniedError(Exception):
    """页面命中排除清单，拒绝读取（退出码 5）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# 排除原因分类（候选表末尾按原因计数时用）
DENY_CATS = {"space": "空间", "page_id": "页面ID", "title_keyword": "标题关键词", "ancestor": "祖先子树"}


class DenyFilter:
    """排除清单：命中清单的页面不读正文、不下附件。

    来源：config.ini 的 deny_spaces / deny_page_ids / deny_title_keywords / deny_ancestor_ids，
    命令行可用 --deny-file 追加（每行一条：space:KEY、id:123、title:关键词、ancestor:123，
    空行与 # 开头的行忽略）。未配置任何一项时 active 为 False，所有命令行为与原来完全一致。
    """

    def __init__(self, cfg: Config, deny_file: str = None):
        self.spaces = set(cfg.list("deny_spaces"))
        self.page_ids = set(cfg.list("deny_page_ids"))
        self.title_keywords = [k for k in cfg.list("deny_title_keywords") if k]
        self.ancestor_ids = set(cfg.list("deny_ancestor_ids"))
        if deny_file:
            self._load_file(deny_file)

    @property
    def active(self) -> bool:
        return bool(self.spaces or self.page_ids or self.title_keywords or self.ancestor_ids)

    def _load_file(self, path: str):
        # 读取 --deny-file 追加清单（类型:值，类型为 space/id/title/ancestor）
        for line in read_text_file(path).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            kind, sep, val = line.replace("=", ":", 1).partition(":")
            kind, val = kind.strip().lower(), val.strip()
            if not sep or not val:
                log(f"排除清单文件忽略无法识别的行：{line}")
                continue
            if kind == "space":
                self.spaces.add(val.lower())
            elif kind in ("id", "page", "page_id"):
                self.page_ids.add(val)
            elif kind == "title":
                self.title_keywords.append(val.lower())
            elif kind == "ancestor":
                self.ancestor_ids.add(val)
            else:
                log(f"排除清单文件忽略未知类型 {kind}：{line}")

    def check(self, pid, title: str = "", space_key: str = "", ancestor_ids=None):
        """检查一个页面是否被排除；命中返回 (分类, 原因)，未命中返回 None。"""
        pid = str(pid)
        if pid in self.page_ids:
            return "page_id", f"页面 ID {pid} 在排除清单中（deny_page_ids）"
        if space_key and space_key.lower() in self.spaces:
            return "space", f"空间 {space_key} 在排除清单中（deny_spaces）"
        low = (title or "").lower()
        for kw in self.title_keywords:
            if kw in low:
                return "title_keyword", f"标题含排除关键词「{kw}」（deny_title_keywords）"
        if pid in self.ancestor_ids:
            return "ancestor", f"页面 {pid} 本身在祖先排除清单中，其整棵子树被排除（deny_ancestor_ids）"
        for a in ancestor_ids or []:
            if str(a) in self.ancestor_ids:
                return "ancestor", f"页面位于被排除页面 {a} 的子树下（deny_ancestor_ids）"
        return None

    def check_page(self, client: Client, pid: str):
        """轻量取页面元数据（不取正文）并检查排除清单；返回 (元数据 dict, 命中结果或 None)。"""
        try:
            d = client.get_json(f"/rest/api/content/{pid}", {"expand": "space,version,ancestors"})
        except HttpError as e:
            if e.code == 404:
                raise HttpError(404, e.url, f"页面 {pid} 不存在，或当前账号没有查看权限")
            raise
        anc = [a.get("id") for a in (d.get("ancestors") or [])]
        hit = self.check(pid, d.get("title", ""), (d.get("space") or {}).get("key", ""), anc)
        return d, hit

    def summary_line(self, counts: dict) -> str:
        # 候选表末尾的排除计数行（只给计数，不列被排除页面）
        total = sum(counts.values())
        detail = "、".join(f"{DENY_CATS.get(k, k)} {v}" for k, v in counts.items() if v)
        return f"已排除 {total} 条" + (f"（{detail}）" if detail else "")


# =============================================================================
# 4. HTML（view 渲染格式 / storage 存储格式）→ Markdown
# =============================================================================

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
             "track", "wbr"}
BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "h1", "h2", "h3", "h4", "h5",
    "h6", "ul", "ol", "li", "table", "pre", "blockquote", "hr", "dl", "dt", "dd", "figure", "figcaption",
    "form", "fieldset", "details", "summary", "address", "center", "tr", "td", "th", "thead", "tbody", "tfoot",
    "caption", "ac:structured-macro", "ac:macro", "ac:rich-text-body", "ac:layout", "ac:layout-section",
    "ac:layout-cell", "ac:task-list", "ac:task", "ac:adf-extension", "ac:adf-node", "ac:adf-content",
}
SKIP_TAGS = {"style", "noscript", "template", "head", "title", "meta", "link", "button", "input", "select",
             "textarea", "svg", "canvas", "ac:parameter", "ac:placeholder", "ac:task-id", "ac:task-status",
             "ac:adf-attribute", "ac:adf-fallback", "colgroup", "col"}
ICON_PATHS = ("/images/icons/", "/s/", "/plugins/servlet/confluence/placeholder/", "/download/resources/",
              "/images/border/", "/rest/documentConversion/", "/plugins/servlet/macro-preview")

_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = parent

    def cls(self):
        return self.attrs.get("class", "") or ""

    def text(self):
        out = []
        for c in self.children:
            out.append(c if isinstance(c, str) else c.text())
        return "".join(out)

    def find_all(self, tag):
        for c in self.children:
            if isinstance(c, Node):
                if c.tag == tag:
                    yield c
                yield from c.find_all(tag)

    def find(self, tag):
        return next(self.find_all(tag), None)


class _DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, [(k, v if v is not None else "") for k, v in attrs], self.cur)
        self.cur.children.append(n)
        if tag not in VOID_TAGS:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        n = Node(tag, [(k, v if v is not None else "") for k, v in attrs], self.cur)
        self.cur.children.append(n)

    def handle_endtag(self, tag):
        x = self.cur
        while x is not self.root and x.tag != tag:
            x = x.parent
        if x is not self.root:
            self.cur = x.parent

    def handle_data(self, data):
        self.cur.children.append(data)

    def unknown_decl(self, data):
        if data.startswith("CDATA["):
            self.cur.children.append(data[6:])


def parse_html(text: str) -> Node:
    text = _CDATA_RE.sub(lambda m: htmlmod.escape(m.group(1), quote=False), text or "")
    b = _DomBuilder()
    b.feed(text)
    b.close()
    return b.root


_WS = re.compile(r"[ \t\r\n\f\v]+")


class HtmlToMarkdown:
    """把 Confluence 的 view/storage HTML 转为 Markdown。img_cb(attrs)->markdown 片段；link_cb(href)->绝对地址。"""

    def __init__(self, img_cb=None, link_cb=None, storage_img_cb=None):
        self.img_cb = img_cb or (lambda a: f"![{a.get('alt', '')}]({a.get('src', '')})")
        self.link_cb = link_cb or (lambda h: h)
        self.storage_img_cb = storage_img_cb or (lambda fn, page: f"![{fn}]({fn})")

    def convert(self, html_text: str) -> str:
        root = parse_html(html_text)
        blocks = self.blocks(root, 0)
        md = "\n\n".join(b for b in blocks if b.strip())
        md = re.sub(r"[ \t]+\n", "\n", md)
        md = re.sub(r"\n{3,}", "\n\n", md)
        return md.strip() + "\n"

    # ---- 分类 ----
    def is_block(self, n: Node) -> bool:
        if n.tag in BLOCK_TAGS:
            if n.tag == "ac:structured-macro" and n.attrs.get("ac:name") in ("status", "anchor", "jira",
                                                                               "emoticon", "user"):
                return False
            return True
        if n.tag == "script" and "syntaxhighlighter" in n.attrs.get("type", ""):
            return True
        if n.tag == "ac:image":
            return False
        return False

    # ---- 块级 ----
    def blocks(self, node: Node, depth: int):
        out = []
        buf = []

        def flush():
            t = "".join(buf)
            buf.clear()
            t = re.sub(r" {2,}", " ", t)
            t = re.sub(r" *\n *", "\n", t).strip()
            if t:
                out.append(t)

        for ch in node.children:
            if isinstance(ch, str):
                buf.append(_WS.sub(" ", ch.replace("\xa0", " ")))
            elif ch.tag in SKIP_TAGS:
                continue
            elif ch.tag == "script" and "syntaxhighlighter" not in ch.attrs.get("type", ""):
                continue
            elif self.is_block(ch):
                flush()
                out.extend(self.block(ch, depth))
            else:
                buf.append(self.inline(ch))
        flush()
        return out

    def block(self, n: Node, depth: int):
        t = n.tag
        cls = n.cls()
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            txt = self.inline_text(n)
            return [("#" * int(t[1]) + " " + txt)] if txt else []
        if t in ("ul", "ol"):
            return self.list_block(n, depth, t == "ol")
        if t == "ac:task-list":
            return self.task_list(n, depth)
        if t == "pre":
            return [self.code_block(n.text(), self.lang_of(n))]
        if t == "script":
            return [self.code_block(n.text(), self.lang_of(n))]
        if t == "table":
            r = self.table(n, depth)
            return [r] if r else []
        if t == "blockquote":
            inner = "\n\n".join(self.blocks(n, depth))
            return ["\n".join("> " + ln if ln else ">" for ln in inner.split("\n"))] if inner else []
        if t == "hr":
            return ["---"]
        if t == "dl":
            lines = []
            for c in n.children:
                if isinstance(c, Node) and c.tag == "dt":
                    lines.append("**" + self.inline_text(c) + "**")
                elif isinstance(c, Node) and c.tag == "dd":
                    lines.append(": " + "\n".join(self.blocks(c, depth + 1)))
            return ["\n".join(lines)]
        if t in ("ac:structured-macro", "ac:macro"):
            return self.storage_macro(n, depth)
        # Confluence view 格式的特殊容器
        if "toc-macro" in cls or "client-side-toc-macro" in cls:
            return []
        if "confluence-information-macro" in cls and "confluence-information-macro-body" not in cls:
            kind = "信息"
            for k, v in (("note", "注意"), ("warning", "警告"), ("tip", "提示"), ("information", "信息")):
                if f"confluence-information-macro-{k}" in cls:
                    kind = v
            inner = "\n\n".join(self.blocks(n, depth))
            return [self.quote(f"**[{kind}]** " + inner)] if inner else []
        if "expand-control" in cls and "expand-container" not in cls:
            txt = self.inline_text(n)
            return [f"**▶ {txt}**"] if txt else []
        if t in ("td", "th", "tr", "thead", "tbody", "tfoot"):
            return self.blocks(n, depth)
        if t == "caption":
            txt = self.inline_text(n)
            return [f"*{txt}*"] if txt else []
        return self.blocks(n, depth)

    def quote(self, text: str) -> str:
        return "\n".join("> " + ln if ln else ">" for ln in text.split("\n"))

    def lang_of(self, n: Node) -> str:
        p = n.attrs.get("data-syntaxhighlighter-params", "") + ";" + n.cls()
        m = re.search(r"brush:\s*([\w+#-]+)", p)
        if m:
            return m.group(1)
        m = re.search(r"language-([\w+#-]+)", p)
        return m.group(1) if m else ""

    def code_block(self, code: str, lang: str = "") -> str:
        code = code.replace("\r\n", "\n").strip("\n")
        fence = "~~~~" if "```" in code else "```"
        return f"{fence}{lang}\n{code}\n{fence}"

    def list_block(self, n: Node, depth: int, ordered: bool):
        lines = []
        try:
            idx = int(n.attrs.get("start", "1"))
        except ValueError:
            idx = 1
        is_task = "inline-task-list" in n.cls()
        for li in n.children:
            if isinstance(li, str):
                if li.strip():
                    lines.append("- " + li.strip())
                continue
            if li.tag in SKIP_TAGS:
                continue
            if li.tag in ("ul", "ol"):
                for ln in "\n".join(self.list_block(li, depth + 1, li.tag == "ol")).split("\n"):
                    lines.append("  " + ln)
                continue
            marker = f"{idx}." if ordered else "-"
            if is_task:
                marker = "- [x]" if "checked" in li.cls() else "- [ ]"
            sub = self.blocks(li, depth + 1) if li.tag == "li" else self.block(li, depth + 1)
            text = "\n".join(sub).strip() or ""
            first, *rest = text.split("\n") if text else [""]
            lines.append(f"{marker} {first}".rstrip())
            pad = " " * (len(marker) + 1)
            for r in rest:
                lines.append(pad + r if r.strip() else "")
            idx += 1
        return ["\n".join(lines)] if lines else []

    def task_list(self, n: Node, depth: int):
        lines = []
        for task in n.children:
            if not isinstance(task, Node) or task.tag != "ac:task":
                continue
            st = task.find("ac:task-status")
            done = st is not None and "complete" in st.text() and "incomplete" not in st.text()
            body = task.find("ac:task-body")
            txt = " ".join(self.blocks(body, depth + 1)) if body is not None else ""
            lines.append(("- [x] " if done else "- [ ] ") + txt)
        return ["\n".join(lines)] if lines else []

    def table(self, tbl: Node, depth: int) -> str:
        rows = []

        def collect(node):
            for c in node.children:
                if not isinstance(c, Node):
                    continue
                if c.tag == "tr":
                    rows.append(c)
                elif c.tag in ("thead", "tbody", "tfoot"):
                    collect(c)

        collect(tbl)
        caption = tbl.find("caption")
        grid = []
        spans = {}
        for tr in rows:
            cells = [c for c in tr.children if isinstance(c, Node) and c.tag in ("td", "th")]
            row = []
            col = 0
            ci = 0
            while True:
                if col in spans and spans[col][0] > 0:
                    row.append(spans[col][1])
                    spans[col][0] -= 1
                    col += 1
                elif ci < len(cells):
                    c = cells[ci]
                    ci += 1
                    txt = self.cell(c, depth)
                    try:
                        cs = max(1, min(int(c.attrs.get("colspan", "1") or 1), 50))
                    except ValueError:
                        cs = 1
                    try:
                        rs = max(1, min(int(c.attrs.get("rowspan", "1") or 1), 1000))
                    except ValueError:
                        rs = 1
                    for _ in range(cs):
                        row.append(txt)
                        if rs > 1:
                            spans[col] = [rs - 1, txt]
                        col += 1
                elif any(k > col and v[0] > 0 for k, v in spans.items()):
                    row.append("")
                    col += 1
                else:
                    break
            grid.append(row)
        if not grid or not any(any(c.strip() for c in r) for r in grid):
            return ""
        out = md_table(grid, header=True)
        if caption is not None:
            out = f"*{self.inline_text(caption)}*\n\n" + out
        return out

    def cell(self, c: Node, depth: int) -> str:
        parts = self.blocks(c, depth + 1)
        return "\n".join(parts).strip()

    def storage_macro(self, n: Node, depth: int):
        name = n.attrs.get("ac:name", "")
        params = {}
        for p in n.children:
            if isinstance(p, Node) and p.tag == "ac:parameter":
                params[p.attrs.get("ac:name", "")] = p.text().strip()
        body = None
        for c in n.children:
            if isinstance(c, Node) and c.tag in ("ac:rich-text-body", "ac:plain-text-body"):
                body = c
        if name in ("code", "noformat", "sql", "sql-query"):
            code = body.text() if body is not None else ""
            return [self.code_block(code, params.get("language", ""))]
        if name in ("info", "note", "warning", "tip", "panel", "tip"):
            label = {"info": "信息", "note": "注意", "warning": "警告", "tip": "提示", "panel": "面板"}[name]
            title = params.get("title", "")
            inner = "\n\n".join(self.blocks(body, depth)) if body is not None else ""
            head = f"**[{label}{(': ' + title) if title else ''}]** "
            return [self.quote(head + inner)]
        if name == "expand":
            inner = "\n\n".join(self.blocks(body, depth)) if body is not None else ""
            return [f"**▶ {params.get('title', '展开')}**\n\n{inner}"]
        if name in ("toc", "toc-zone", "children", "pagetree", "recently-updated", "contributors"):
            return [f"[宏: {name}]"] if name not in ("toc",) else []
        if name in ("drawio", "drawio-sketch", "inc-drawio", "gliffy"):
            dn = params.get("diagramName") or params.get("name") or ""
            return [f"[图表 {name}: {dn}]（渲染图见附件 {dn}.png）"]
        if name in ("include", "excerpt-include"):
            pg = n.find("ri:page")
            title = pg.attrs.get("ri:content-title", "") if pg is not None else ""
            return [f"[引用页面内容: {title}]"]
        if name in ("attachments", "viewfile", "view-file", "viewdoc", "viewxls", "viewppt", "viewpdf"):
            att = n.find("ri:attachment")
            fn = att.attrs.get("ri:filename", "") if att is not None else ""
            return [f"[附件宏: {fn or name}]"]
        if body is not None:
            return self.blocks(body, depth)
        return [f"[宏: {name} {json.dumps(params, ensure_ascii=False) if params else ''}]".strip()]

    # ---- 行内 ----
    def inline_text(self, n: Node) -> str:
        s = "".join(self.inline(c) if isinstance(c, Node) else _WS.sub(" ", c.replace("\xa0", " "))
                    for c in n.children)
        return re.sub(r"\s+", " ", s).strip()

    def wrap(self, inner: str, mark: str) -> str:
        if not inner.strip():
            return inner
        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        return f"{lead}{mark}{inner.strip()}{mark}{trail}"

    def inline(self, n: Node) -> str:
        t = n.tag
        if t in SKIP_TAGS:
            return ""
        if t == "br":
            return "\n"
        if t == "img":
            return self.img_cb(n.attrs)
        if self.is_block(n):
            return "\n" + "\n".join(self.block(n, 1)) + "\n"
        inner = "".join(self.inline(c) if isinstance(c, Node) else _WS.sub(" ", c.replace("\xa0", " "))
                        for c in n.children)
        cls = n.cls()
        if t in ("strong", "b"):
            return self.wrap(inner, "**")
        if t in ("em", "i", "cite"):
            return self.wrap(inner, "*")
        if t in ("s", "del", "strike"):
            return self.wrap(inner, "~~")
        if t in ("code", "tt", "kbd", "samp"):
            return f"`{inner.strip()}`" if inner.strip() else inner
        if t == "a":
            href = n.attrs.get("href", "")
            if "confluence-userlink" in cls or "user-mention" in cls:
                return "@" + inner.strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                return inner
            href = self.link_cb(href)
            label = inner.strip()
            if not label:
                return f"<{href}>"
            if label.startswith("!["):
                return label
            return f"[{label}]({href})"
        if t == "span" and ("status-macro" in cls or "aui-lozenge" in cls):
            return f"[{inner.strip()}]"
        if t == "ac:image":
            att = n.find("ri:attachment")
            if att is not None:
                pg = att.find("ri:page")
                return self.storage_img_cb(att.attrs.get("ri:filename", ""),
                                           pg.attrs.get("ri:content-title", "") if pg is not None else "")
            u = n.find("ri:url")
            if u is not None:
                return f"![]({u.attrs.get('ri:value', '')})"
            return ""
        if t == "ac:link":
            body = n.find("ac:link-body") or n.find("ac:plain-text-link-body")
            label = body.text().strip() if body is not None else ""
            pg = n.find("ri:page")
            if pg is not None:
                return f"[[{label or pg.attrs.get('ri:content-title', '')}]]"
            att = n.find("ri:attachment")
            if att is not None:
                return f"[附件: {label or att.attrs.get('ri:filename', '')}]"
            usr = n.find("ri:user")
            if usr is not None:
                return "@" + (label or usr.attrs.get("ri:username", "") or usr.attrs.get("ri:userkey", "")[:8])
            return label
        if t == "ac:emoticon":
            return ""
        if t in ("ac:structured-macro", "ac:macro"):
            name = n.attrs.get("ac:name", "")
            params = {p.attrs.get("ac:name", ""): p.text().strip() for p in n.children
                      if isinstance(p, Node) and p.tag == "ac:parameter"}
            if name == "status":
                return f"[{params.get('title', '')}]"
            if name == "jira":
                return f"[JIRA {params.get('key', params.get('jqlQuery', ''))}]"
            return ""
        if t == "time":
            return n.attrs.get("datetime", inner)
        return inner


# =============================================================================
# 5. Office / 文本 / 邮件 等附件内容提取（纯标准库）
# =============================================================================

NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
NS_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
NS_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
NS_V = "{urn:schemas-microsoft-com:vml}"
NS_VSD = "{http://schemas.microsoft.com/office/visio/2012/main}"
NS_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class ExtractResult:
    def __init__(self):
        self.md_path = ""
        self.summary = ""
        self.images = []  # [(path, w, h)]
        self.extra_files = []
        self.chars = 0
        self.error = ""


class MediaSaver:
    """把文档内嵌图片保存到 <名>_media/ 目录，返回绝对路径。"""

    def __init__(self, media_dir: str, cfg: Config, prefix: str = "img"):
        self.dir = media_dir
        self.cfg = cfg
        self.prefix = prefix
        self.saved = {}
        self.images = []
        self.count = 0
        self.max = cfg.int("max_doc_images") or 60
        self.min_px = cfg.int("min_image_px") or 48
        self.skipped = 0

    def save(self, key, data: bytes, ext: str, hint: str = ""):
        if key in self.saved:
            return self.saved[key]
        if self.count >= self.max:
            self.skipped += 1
            self.saved[key] = None
            return None
        ext = (ext or ".bin").lower()
        os.makedirs(self.dir, exist_ok=True)
        self.count += 1
        name = f"{self.prefix}{self.count:03d}{('_' + safe_name(hint, 30)) if hint else ''}{ext}"
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(data)
        wh = image_size(p) or (0, 0)
        if wh != (0, 0) and max(wh) < self.min_px:
            os.remove(p)
            self.count -= 1
            self.saved[key] = None
            return None
        self.saved[key] = p
        self.images.append((p, wh[0], wh[1]))
        return p

    def note(self, p):
        if not p:
            return ""
        ext = os.path.splitext(p)[1].lower()
        if ext in (".emf", ".wmf"):
            return f"[图片(EMF/WMF 矢量格式，模型可能无法直接查看): {p}]"
        if ext in (".tif", ".tiff", ".bmp"):
            return f"[图片({ext} 格式): {p}]"
        return f"![图片]({p})"


def _rels(z: zipfile.ZipFile, part: str) -> dict:
    d, f = posixpath.split(part)
    rp = f"{d}/_rels/{f}.rels" if d else f"_rels/{f}.rels"
    out = {}
    try:
        root = ET.fromstring(z.read(rp))
    except KeyError:
        return out
    except ET.ParseError:
        return out
    for rel in root:
        rid = rel.get("Id")
        tgt = rel.get("Target", "")
        mode = rel.get("TargetMode", "")
        typ = rel.get("Type", "").rsplit("/", 1)[-1]
        if mode != "External":
            tgt = tgt[1:] if tgt.startswith("/") else posixpath.normpath(posixpath.join(d, tgt))
        out[rid] = (typ, tgt, mode)
    return out


# ---------- Word (.docx) ----------

class DocxConverter:
    def __init__(self, z: zipfile.ZipFile, media: MediaSaver):
        self.z = z
        self.media = media
        self.main = "word/document.xml"
        try:
            root_rels = ET.fromstring(z.read("_rels/.rels"))
            for rel in root_rels:
                if rel.get("Type", "").endswith("/officeDocument"):
                    self.main = rel.get("Target", "").lstrip("/")
        except Exception:
            pass
        self.rels = _rels(z, self.main)
        self.styles = {}
        self.style_num = {}
        self.numfmt = {}
        self._load_styles()
        self._load_numbering()

    def _load_styles(self):
        try:
            root = ET.fromstring(self.z.read("word/styles.xml"))
        except Exception:
            return
        raw = {}
        for s in root.iter(NS_W + "style"):
            sid = s.get(NS_W + "styleId")
            nm = s.find(NS_W + "name")
            name = nm.get(NS_W + "val") if nm is not None else ""
            ol = s.find(f"{NS_W}pPr/{NS_W}outlineLvl")
            based = s.find(NS_W + "basedOn")
            try:
                olv = int(ol.get(NS_W + "val")) if ol is not None else None
            except (TypeError, ValueError):
                olv = None
            raw[sid] = (name or "", olv, based.get(NS_W + "val") if based is not None else None)
            npr = s.find(f"{NS_W}pPr/{NS_W}numPr")
            if npr is not None:
                nid = npr.find(NS_W + "numId")
                il = npr.find(NS_W + "ilvl")
                self.style_num[sid] = (nid.get(NS_W + "val") if nid is not None else "0",
                                       il.get(NS_W + "val") if il is not None else "0")

        def level(sid, depth=0):
            if sid not in raw or depth > 8:
                return 0
            name, ol, based = raw[sid]
            m = re.match(r"^(heading|标题)\s*(\d)$", name.strip(), re.I)
            if m:
                return int(m.group(2))
            if name.strip().lower() in ("title", "标题"):
                return 1
            if ol is not None and ol < 9:
                return ol + 1
            return level(based, depth + 1) if based else 0

        for sid in raw:
            self.styles[sid] = level(sid)

    def _load_numbering(self):
        try:
            root = ET.fromstring(self.z.read("word/numbering.xml"))
        except Exception:
            return
        absfmt = {}
        for an in root.iter(NS_W + "abstractNum"):
            aid = an.get(NS_W + "abstractNumId")
            for lvl in an.iter(NS_W + "lvl"):
                f = lvl.find(NS_W + "numFmt")
                absfmt[(aid, lvl.get(NS_W + "ilvl"))] = f.get(NS_W + "val") if f is not None else "decimal"
        for num in root.iter(NS_W + "num"):
            nid = num.get(NS_W + "numId")
            a = num.find(NS_W + "abstractNumId")
            if a is None:
                continue
            aid = a.get(NS_W + "val")
            for (aa, il), f in absfmt.items():
                if aa == aid:
                    self.numfmt[(nid, il)] = f

    def convert(self) -> str:
        root = ET.fromstring(self.z.read(self.main))
        body = root.find(NS_W + "body")
        out = []
        if body is not None:
            for el in body:
                self.block(el, out)
        text = "\n\n".join(x for x in out if x and x.strip())
        cm = self.comments()
        if cm:
            text += "\n\n## 文档批注\n\n" + cm
        return text

    def comments(self) -> str:
        part = None
        for _, (typ, tgt, _m) in self.rels.items():
            if typ == "comments":
                part = tgt
        if not part:
            return ""
        try:
            root = ET.fromstring(self.z.read(part))
        except Exception:
            return ""
        lines = []
        for c in root.iter(NS_W + "comment"):
            who = c.get(NS_W + "author", "")
            txt = " ".join(self.inline(p).strip() for p in c.iter(NS_W + "p"))
            if txt.strip():
                lines.append(f"- **{who}**: {txt.strip()}")
        return "\n".join(lines)

    def block(self, el, out):
        t = el.tag
        if t == NS_W + "p":
            s = self.para(el)
            if s is not None:
                out.append(s)
        elif t == NS_W + "tbl":
            out.append(self.table(el))
        elif t in (NS_W + "sdt",):
            c = el.find(NS_W + "sdtContent")
            if c is not None:
                for x in c:
                    self.block(x, out)
        elif t in (NS_W + "customXml", NS_W + "smartTag", NS_W + "ins", NS_W + "moveTo"):
            for x in el:
                self.block(x, out)

    def para(self, p):
        text = self.inline(p).strip()
        ppr = p.find(NS_W + "pPr")
        level = 0
        list_prefix = ""
        if ppr is not None:
            st = ppr.find(NS_W + "pStyle")
            sid = st.get(NS_W + "val") if st is not None else None
            if sid:
                level = self.styles.get(sid, 0)
            ol = ppr.find(NS_W + "outlineLvl")
            if ol is not None and not level:
                try:
                    v = int(ol.get(NS_W + "val"))
                    level = v + 1 if v < 9 else 0
                except (TypeError, ValueError):
                    pass
            np_ = ppr.find(NS_W + "numPr")
            num = None
            if np_ is not None:
                nid = np_.find(NS_W + "numId")
                il = np_.find(NS_W + "ilvl")
                sn = self.style_num.get(sid, ("0", "0"))
                num = (nid.get(NS_W + "val") if nid is not None else sn[0],
                       il.get(NS_W + "val") if il is not None else sn[1])
            elif sid in self.style_num:
                num = self.style_num[sid]
            if num and not level:
                nid, il = num
                if nid != "0":
                    fmt = self.numfmt.get((nid, il), "decimal")
                    mark = "-" if fmt == "bullet" else ("" if fmt == "none" else "1.")
                    try:
                        indent = "  " * int(il)
                    except ValueError:
                        indent = ""
                    list_prefix = f"{indent}{mark} " if mark else indent
        if not text:
            return None
        if level:
            return "#" * min(level, 6) + " " + text.replace("\n", " ")
        if list_prefix:
            return list_prefix + text
        return text

    def inline(self, el) -> str:
        parts = []
        for ch in el:
            t = ch.tag
            if t == NS_W + "t":
                parts.append(ch.text or "")
            elif t == NS_W + "tab":
                parts.append("\t")
            elif t in (NS_W + "br", NS_W + "cr"):
                parts.append("\n")
            elif t == NS_W + "noBreakHyphen":
                parts.append("-")
            elif t in (NS_W + "pPr", NS_W + "rPr", NS_W + "del", NS_W + "delText", NS_W + "instrText",
                       NS_MC + "Fallback", NS_W + "moveFrom", NS_W + "sectPr", NS_W + "tblPr"):
                continue
            elif t == NS_W + "hyperlink":
                inner = self.inline(ch)
                rid = ch.get(NS_R + "id")
                url = self.rels.get(rid, ("", "", ""))[1] if rid else ""
                parts.append(f"[{inner}]({url})" if url and inner.strip() else inner)
            elif t == NS_A + "blip":
                rid = ch.get(NS_R + "embed") or ch.get(NS_R + "link")
                parts.append(" " + self.image(rid) + " ")
            elif t == NS_V + "imagedata":
                parts.append(" " + self.image(ch.get(NS_R + "id")) + " ")
            elif t == NS_W + "txbxContent":
                txt = " / ".join(self.inline(p).strip() for p in ch.iter(NS_W + "p") if self.inline(p).strip())
                if txt:
                    parts.append(f" [文本框: {txt}] ")
            elif t == NS_W + "tbl":
                parts.append("\n" + self.table(ch) + "\n")
            else:
                parts.append(self.inline(ch))
        return "".join(parts)

    def image(self, rid) -> str:
        if not rid or rid not in self.rels:
            return ""
        typ, tgt, mode = self.rels[rid]
        if mode == "External":
            return f"![外链图片]({tgt})"
        try:
            data = self.z.read(tgt)
        except KeyError:
            return ""
        p = self.media.save(tgt, data, os.path.splitext(tgt)[1])
        return self.media.note(p)

    def table(self, tbl) -> str:
        rows = []
        vmerge_prev = {}
        for tr in self._iter(tbl, NS_W + "tr"):
            row = []
            col = 0
            for tc in self._iter(tr, NS_W + "tc"):
                tcpr = tc.find(NS_W + "tcPr")
                span = 1
                cont = False
                if tcpr is not None:
                    gs = tcpr.find(NS_W + "gridSpan")
                    if gs is not None:
                        try:
                            span = max(1, int(gs.get(NS_W + "val")))
                        except (TypeError, ValueError):
                            span = 1
                    vm = tcpr.find(NS_W + "vMerge")
                    if vm is not None and vm.get(NS_W + "val") in (None, "continue"):
                        cont = True
                if cont:
                    txt = vmerge_prev.get(col, "")
                else:
                    sub = []
                    for x in tc:
                        self.block(x, sub)
                    txt = "\n".join(s for s in sub if s and s.strip())
                for _ in range(span):
                    row.append(txt)
                    vmerge_prev[col] = txt
                    col += 1
            rows.append(row)
        return md_table(rows)

    def _iter(self, parent, tag):
        for c in parent:
            if c.tag == tag:
                yield c
            elif c.tag in (NS_W + "sdt", NS_W + "customXml"):
                inner = c.find(NS_W + "sdtContent") if c.tag == NS_W + "sdt" else c
                if inner is not None:
                    yield from self._iter(inner, tag)


def extract_docx(path, res: ExtractResult, media: MediaSaver) -> str:
    with zipfile.ZipFile(path) as z:
        text = DocxConverter(z, media).convert()
    res.summary = f"Word 文档，{len(text)} 字符"
    return text


# ---------- Excel (.xlsx) ----------

_DATE_IDS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59)) | {71, 72, 73, 74, 75, 76,
                                                                                          77, 78, 79, 80, 81}


def is_date_format(code: str) -> bool:
    if not code:
        return False
    c = re.sub(r'"[^"]*"', "", code)
    c = re.sub(r"\\.", "", c)
    c = re.sub(r"\[(?!h\]|m\]|s\]|hh\]|mm\]|ss\])[^\]]*\]", "", c, flags=re.I)
    c = c.split(";")[0]
    if re.search(r"[yYdDhHsS]", c) or "年" in code or "月" in code or "日" in code:
        return True
    if re.search(r"[mM]", c) and not re.search(r"[0#?]", c):
        return True
    return False


def excel_date(v: float, date1904=False) -> str:
    try:
        base = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 30)
        d = base + dt.timedelta(seconds=round(float(v) * 86400))
    except (OverflowError, ValueError):
        return str(v)
    if float(v) < 1 and not date1904:
        return d.strftime("%H:%M:%S")
    if d.hour == d.minute == d.second == 0:
        return d.strftime("%Y-%m-%d")
    return d.strftime("%Y-%m-%d %H:%M:%S")


def fmt_number(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return "%.15g" % f


def col_to_idx(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def idx_to_col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


_REF_RE = re.compile(r"([A-Za-z]+)(\d+)")


class SheetData:
    def __init__(self, name, hidden=False):
        self.name = name
        self.hidden = hidden
        self.cells = {}  # (r, c) -> str
        self.merges = []  # (r1, c1, r2, c2)
        self.truncated = False
        self.images = []
        self.notes = []

    def grid(self, max_rows=None):
        if not self.cells:
            return []
        for (r1, c1, r2, c2) in self.merges:
            v = self.cells.get((r1, c1), "")
            if v == "":
                continue
            if (r2 - r1 + 1) * (c2 - c1 + 1) > 5000:
                continue
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    self.cells.setdefault((r, c), v)
        maxr = max(r for r, _ in self.cells)
        maxc = max(c for _, c in self.cells)
        minr = min(r for r, _ in self.cells)
        minc = min(c for _, c in self.cells)
        rows = []
        for r in range(minr, maxr + 1):
            if max_rows and len(rows) >= max_rows:
                break
            rows.append([self.cells.get((r, c), "") for c in range(minc, maxc + 1)])
        while rows and not any(x.strip() for x in rows[-1]):
            rows.pop()
        return rows

    def origin(self):
        if not self.cells:
            return "A1"
        return f"{idx_to_col(min(c for _, c in self.cells))}{min(r for r, _ in self.cells) + 1}"


def write_sheets(sheets, base_md_title, res: ExtractResult, csv_dir: str, stem: str, cfg: Config, note=""):
    preview = cfg.int("excel_preview_rows") or 100
    md = [f"# {base_md_title}"]
    if note:
        md.append(note)
    total_rows = 0
    for i, sh in enumerate(sheets, 1):
        rows = sh.grid()
        total_rows += len(rows)
        title = f"## 工作表 {i}: {sh.name}" + ("（隐藏）" if sh.hidden else "")
        md.append(title)
        if not rows:
            md.append("（空表）")
        else:
            ncol = max(len(r) for r in rows)
            csv_path = os.path.join(csv_dir, safe_name(f"{stem}__{sh.name}", 70) + ".csv")
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                csv.writer(f).writerows(rows)
            res.extra_files.append(csv_path)
            md.append(f"范围起点 {sh.origin()}，共 {len(rows)} 行 × {ncol} 列"
                      + ("（已达行数上限，后面的行未读取）" if sh.truncated else "")
                      + f"。完整 CSV：{csv_path}")
            shown = rows[:preview]
            md.append(md_table(shown))
            if len(rows) > preview:
                md.append(f"…… 仅预览前 {preview} 行，其余 {len(rows) - preview} 行见 CSV。")
        if sh.merges:
            md.append("合并单元格（已用左上角值填充）：" + ", ".join(
                f"{idx_to_col(c1)}{r1 + 1}:{idx_to_col(c2)}{r2 + 1}" for r1, c1, r2, c2 in sh.merges[:30])
                      + (" …" if len(sh.merges) > 30 else ""))
        for n_ in sh.notes:
            md.append(n_)
        for p in sh.images:
            md.append(f"![工作表图片]({p})")
    res.summary = f"Excel，{len(sheets)} 个工作表，共 {total_rows} 行"
    return "\n\n".join(md)


def extract_xlsx(path, res: ExtractResult, media: MediaSaver, cfg: Config, csv_dir: str, stem: str) -> str:
    max_rows = cfg.int("excel_max_rows") or 20000
    with zipfile.ZipFile(path) as z:
        wb_part = "xl/workbook.xml"
        try:
            for rel in ET.fromstring(z.read("_rels/.rels")):
                if rel.get("Type", "").endswith("/officeDocument"):
                    wb_part = rel.get("Target", "").lstrip("/")
        except Exception:
            pass
        wb = ET.fromstring(z.read(wb_part))
        rels = _rels(z, wb_part)
        pr = wb.find(NS_S + "workbookPr")
        date1904 = pr is not None and pr.get("date1904", "0") in ("1", "true")
        shared = []
        styles_part = None
        for _, (typ, tgt, _m) in rels.items():
            if typ == "sharedStrings":
                for _ev, si in ET.iterparse(z.open(tgt), events=("end",)):
                    if si.tag == NS_S + "si":
                        parts = []
                        for c in si:
                            if c.tag == NS_S + "t":
                                parts.append(c.text or "")
                            elif c.tag == NS_S + "r":
                                t = c.find(NS_S + "t")
                                parts.append((t.text or "") if t is not None else "")
                        shared.append("".join(parts))
                        si.clear()
            elif typ == "styles":
                styles_part = tgt
        date_xf = set()
        if styles_part:
            try:
                st = ET.fromstring(z.read(styles_part))
                custom = {}
                nf = st.find(NS_S + "numFmts")
                if nf is not None:
                    for f in nf:
                        custom[int(f.get("numFmtId"))] = f.get("formatCode", "")
                xfs = st.find(NS_S + "cellXfs")
                if xfs is not None:
                    for i, xf in enumerate(xfs):
                        fid = int(xf.get("numFmtId", "0"))
                        if fid in _DATE_IDS or (fid in custom and is_date_format(custom[fid])):
                            date_xf.add(i)
            except Exception:
                pass
        sheets = []
        sh_el = wb.find(NS_S + "sheets")
        for s in (sh_el if sh_el is not None else []):
            name = s.get("name", "Sheet")
            rid = s.get(NS_R + "id")
            hidden = s.get("state", "visible") != "visible"
            if rid not in rels:
                continue
            part = rels[rid][1]
            sd = SheetData(name, hidden)
            sheets.append(sd)
            if rels[rid][0] != "worksheet":
                sd.notes.append(f"（这是 {rels[rid][0]} 类型的表，无单元格数据）")
                continue
            row_idx = -1
            col_idx = -1
            try:
                for ev, el in ET.iterparse(z.open(part), events=("start", "end")):
                    tag = el.tag
                    if ev == "start":
                        if tag == NS_S + "row":
                            r = el.get("r")
                            row_idx = int(r) - 1 if r else row_idx + 1
                            col_idx = -1
                            if row_idx >= max_rows:
                                sd.truncated = True
                                break
                        continue
                    if tag == NS_S + "c":
                        ref = el.get("r")
                        if ref:
                            m = _REF_RE.match(ref)
                            col_idx = col_to_idx(m.group(1))
                            row_idx = int(m.group(2)) - 1
                        else:
                            col_idx += 1
                        t = el.get("t", "n")
                        v = el.find(NS_S + "v")
                        val = v.text if v is not None and v.text is not None else ""
                        if t == "s":
                            try:
                                val = shared[int(val)]
                            except (ValueError, IndexError):
                                pass
                        elif t == "inlineStr":
                            is_ = el.find(NS_S + "is")
                            val = "".join(x.text or "" for x in is_.iter(NS_S + "t")) if is_ is not None else ""
                        elif t == "b":
                            val = "TRUE" if val == "1" else "FALSE"
                        elif t in ("str", "e", "d"):
                            pass
                        elif val != "":
                            sidx = int(el.get("s", "0") or 0)
                            val = excel_date(val, date1904) if sidx in date_xf else fmt_number(val)
                        if val == "":
                            f_ = el.find(NS_S + "f")
                            if f_ is not None and f_.text:
                                val = "=" + f_.text  # 公式没有缓存结果时显示公式本身
                        if val != "":
                            sd.cells[(row_idx, col_idx)] = val
                        el.clear()
                    elif tag == NS_S + "row":
                        el.clear()
                    elif tag == NS_S + "mergeCell":
                        ref = el.get("ref", "")
                        if ":" in ref:
                            a, b = ref.split(":")
                            ma, mb = _REF_RE.match(a), _REF_RE.match(b)
                            if ma and mb:
                                sd.merges.append((int(ma.group(2)) - 1, col_to_idx(ma.group(1)),
                                                  int(mb.group(2)) - 1, col_to_idx(mb.group(1))))
            except ET.ParseError as e:
                sd.notes.append(f"（解析中断：{e}）")
            # 工作表里的图片 / 图表
            for _, (typ, tgt, _m) in _rels(z, part).items():
                if typ == "drawing":
                    for _, (t2, tg2, m2) in _rels(z, tgt).items():
                        if t2 == "image" and m2 != "External":
                            try:
                                p = media.save(tg2, z.read(tg2), os.path.splitext(tg2)[1], name)
                                if p:
                                    sd.images.append(p)
                            except KeyError:
                                pass
                        elif t2 == "chart":
                            sd.notes.append("（含图表：数据来自本表单元格）")
    return write_sheets(sheets, os.path.basename(path), res, csv_dir, stem, cfg)


# ---------- PowerPoint (.pptx) ----------

def _pptx_shape_texts(z, sp_tree, media, rels, out):
    for el in sp_tree:
        t = el.tag
        if t == NS_P + "sp":
            body = el.find(NS_P + "txBody")
            if body is None:
                continue
            paras = []
            for p in body.iter(NS_A + "p"):
                s = []
                for c in p:
                    if c.tag in (NS_A + "r", NS_A + "fld"):
                        tt = c.find(NS_A + "t")
                        s.append((tt.text or "") if tt is not None else "")
                    elif c.tag == NS_A + "br":
                        s.append("\n")
                line = "".join(s).strip()
                if line:
                    lvl = 0
                    ppr = p.find(NS_A + "pPr")
                    if ppr is not None:
                        try:
                            lvl = int(ppr.get("lvl", "0"))
                        except ValueError:
                            lvl = 0
                    paras.append("  " * lvl + "- " + line)
            if paras:
                out.append("\n".join(paras))
        elif t == NS_P + "grpSp":
            _pptx_shape_texts(z, el, media, rels, out)
        elif t == NS_P + "graphicFrame":
            tbl = el.find(f".//{NS_A}tbl")
            if tbl is not None:
                rows = []
                for tr in tbl.iter(NS_A + "tr"):
                    row = []
                    for tc in tr.iter(NS_A + "tc"):
                        row.append(" ".join((x.text or "") for x in tc.iter(NS_A + "t")).strip())
                    rows.append(row)
                out.append(md_table(rows))
            elif el.find(f".//{{http://schemas.openxmlformats.org/drawingml/2006/chart}}chart") is not None:
                out.append("[图表]")
        elif t == NS_P + "pic":
            blip = el.find(f".//{NS_A}blip")
            if blip is not None:
                rid = blip.get(NS_R + "embed")
                if rid in rels and rels[rid][2] != "External":
                    try:
                        tg = rels[rid][1]
                        p = media.save(tg, z.read(tg), os.path.splitext(tg)[1])
                        if p:
                            out.append(media.note(p))
                    except KeyError:
                        pass


def extract_pptx(path, res: ExtractResult, media: MediaSaver) -> str:
    out = [f"# {os.path.basename(path)}"]
    n = 0
    with zipfile.ZipFile(path) as z:
        pres_part = "ppt/presentation.xml"
        pres = ET.fromstring(z.read(pres_part))
        prels = _rels(z, pres_part)
        lst = pres.find(NS_P + "sldIdLst")
        for sid in (lst if lst is not None else []):
            rid = sid.get(NS_R + "id")
            if rid not in prels:
                continue
            part = prels[rid][1]
            n += 1
            try:
                sl = ET.fromstring(z.read(part))
            except KeyError:
                continue
            srels = _rels(z, part)
            hidden = sl.get("show") == "0"
            title = ""
            for sp in sl.iter(NS_P + "sp"):
                ph = sp.find(f".//{NS_P}ph")
                if ph is not None and ph.get("type") in ("title", "ctrTitle"):
                    title = " ".join((x.text or "") for x in sp.iter(NS_A + "t")).strip()
                    break
            out.append(f"## 第 {n} 页{('：' + title) if title else ''}{'（隐藏）' if hidden else ''}")
            tree = sl.find(f"{NS_P}cSld/{NS_P}spTree")
            parts = []
            if tree is not None:
                _pptx_shape_texts(z, tree, media, srels, parts)
            out.extend(parts or ["（无文字）"])
            for _, (typ, tgt, _m) in srels.items():
                if typ == "notesSlide":
                    try:
                        nt = ET.fromstring(z.read(tgt))
                        txt = []
                        for sp in nt.iter(NS_P + "sp"):
                            ph = sp.find(f".//{NS_P}ph")
                            if ph is not None and ph.get("type") == "body":
                                for p in sp.iter(NS_A + "p"):
                                    line = "".join(x.text or "" for x in p.iter(NS_A + "t")).strip()
                                    if line:
                                        txt.append(line)
                        if txt:
                            out.append("> 备注：" + " / ".join(txt))
                    except Exception:
                        pass
    res.summary = f"PPT，{n} 页"
    return "\n\n".join(out)


# ---------- Visio (.vsdx) ----------

def extract_vsdx(path, res: ExtractResult) -> str:
    out = [f"# {os.path.basename(path)}"]
    with zipfile.ZipFile(path) as z:
        pages_part = "visio/pages/pages.xml"
        try:
            pages = ET.fromstring(z.read(pages_part))
        except KeyError:
            res.summary = "Visio（未找到页面）"
            return out[0]
        prels = _rels(z, pages_part)
        for pg in pages.iter(NS_VSD + "Page"):
            name = pg.get("NameU") or pg.get("Name") or "页"
            rel = pg.find(NS_VSD + "Rel")
            rid = rel.get(NS_R + "id") if rel is not None else None
            if rid not in prels:
                continue
            root = ET.fromstring(z.read(prels[rid][1]))
            texts = {}
            for sh in root.iter(NS_VSD + "Shape"):
                tx = sh.find(NS_VSD + "Text")
                if tx is not None:
                    s = re.sub(r"\s+", " ", "".join(tx.itertext())).strip()
                    if s:
                        texts[sh.get("ID")] = s
            out.append(f"## 页面：{name}")
            if texts:
                out.append("\n".join(f"- [{k}] {v}" for k, v in texts.items()))
            ends = {}
            for c in root.iter(NS_VSD + "Connect"):
                fs, fc, ts = c.get("FromSheet"), c.get("FromCell"), c.get("ToSheet")
                ends.setdefault(fs, {})["b" if fc == "BeginX" else "e"] = ts
            edges = []
            for conn, d in ends.items():
                if "b" in d and "e" in d:
                    a = texts.get(d["b"], f"#{d['b']}")
                    b = texts.get(d["e"], f"#{d['e']}")
                    lab = texts.get(conn, "")
                    edges.append(f"- {a} → {b}" + (f"（{lab}）" if lab else ""))
            if edges:
                out.append("连线关系：\n" + "\n".join(edges))
    res.summary = "Visio 图"
    return "\n\n".join(out)


# ---------- 旧版二进制 Office（.doc/.xls/.ppt）：OLE 复合文档解析 ----------

class CFB:
    """最小化的 OLE2 复合文档读取器。"""

    def __init__(self, data: bytes):
        if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise ValueError("不是 OLE 复合文档")
        self.d = data
        self.ss = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        self.mss = 1 << struct.unpack_from("<H", data, 0x20)[0]
        nfat = struct.unpack_from("<I", data, 0x2C)[0]
        first_dir = struct.unpack_from("<I", data, 0x30)[0]
        self.cutoff = struct.unpack_from("<I", data, 0x38)[0]
        first_mf = struct.unpack_from("<I", data, 0x3C)[0]
        first_difat = struct.unpack_from("<I", data, 0x44)[0]
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        sec = first_difat
        guard = 0
        while sec < 0xFFFFFFFA and guard < 10000:
            off = (sec + 1) * self.ss
            n = self.ss // 4
            vals = struct.unpack_from(f"<{n}I", data, off)
            difat.extend(vals[:-1])
            sec = vals[-1]
            guard += 1
        fat = []
        for s in difat[:nfat]:
            if s >= 0xFFFFFFFA:
                continue
            off = (s + 1) * self.ss
            if off + self.ss <= len(data):
                fat.extend(struct.unpack_from(f"<{self.ss // 4}I", data, off))
        self.fat = fat
        dirdata = self._chain(first_dir)
        self.entries = []
        for i in range(len(dirdata) // 128):
            e = dirdata[i * 128:(i + 1) * 128]
            nlen = struct.unpack_from("<H", e, 64)[0]
            name = e[:max(0, nlen - 2)].decode("utf-16-le", "replace")
            typ = e[66]
            start = struct.unpack_from("<I", e, 116)[0]
            size = struct.unpack_from("<Q", e, 120)[0]
            if self.ss == 512:
                size &= 0xFFFFFFFF
            self.entries.append((name, typ, start, size))
        root = self.entries[0] if self.entries else ("", 5, 0xFFFFFFFE, 0)
        self.ministream = self._chain(root[2]) if root[2] < 0xFFFFFFFA else b""
        self.minifat = []
        if first_mf < 0xFFFFFFFA:
            mf = self._chain(first_mf)
            self.minifat = list(struct.unpack_from(f"<{len(mf) // 4}I", mf, 0))

    def _chain(self, start):
        out = bytearray()
        s = start
        seen = 0
        while s < 0xFFFFFFFA and s < len(self.fat) and seen <= len(self.fat):
            off = (s + 1) * self.ss
            out += self.d[off:off + self.ss]
            s = self.fat[s] if s < len(self.fat) else 0xFFFFFFFE
            seen += 1
        return bytes(out)

    def _minichain(self, start):
        out = bytearray()
        s = start
        seen = 0
        while s < 0xFFFFFFFA and s < len(self.minifat) and seen <= len(self.minifat):
            off = s * self.mss
            out += self.ministream[off:off + self.mss]
            s = self.minifat[s]
            seen += 1
        return bytes(out)

    def exists(self, name):
        return any(e[0].lower() == name.lower() and e[1] == 2 for e in self.entries)

    def open(self, name) -> bytes:
        for nm, typ, start, size in self.entries:
            if typ == 2 and nm.lower() == name.lower():
                data = self._minichain(start) if size < self.cutoff else self._chain(start)
                return data[:size]
        raise KeyError(name)


def _doc_clean(text: str) -> str:
    out = []
    stack = []  # 字段：0=代码段（丢弃），1=结果段（保留）
    for ch in text:
        o = ord(ch)
        if o == 0x13:
            stack.append(0)
            continue
        if o == 0x14:
            if stack:
                stack[-1] = 1
            continue
        if o == 0x15:
            if stack:
                stack.pop()
            continue
        if stack and stack[-1] == 0:
            continue
        if ch == "\r":
            out.append("\n")
        elif ch == "\x07":
            out.append("\x07")
        elif ch in "\x0b\x0c":
            out.append("\n")
        elif ch == "\x1e":
            out.append("-")
        elif o in (0x01, 0x02, 0x03, 0x04, 0x05, 0x08, 0x1f):
            continue
        elif ch == "\xa0":
            out.append(" ")
        else:
            out.append(ch)
    s = "".join(out)
    s = s.replace("\x07\n\x07", "\x07\x07")
    s = s.replace("\x07\x07", " |\n").replace("\x07", " | ")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def extract_doc_python(path) -> str:
    cfb = CFB(open(path, "rb").read())
    wd = cfb.open("WordDocument")
    ident, = struct.unpack_from("<H", wd, 0)
    if ident != 0xA5EC:
        raise ValueError("不是 Word 97-2003 文档")
    flags, = struct.unpack_from("<H", wd, 0x0A)
    if flags & 0x0100:
        raise ValueError("文档已加密（有打开密码）")
    table = cfb.open("1Table" if flags & 0x0200 else "0Table")
    csw, = struct.unpack_from("<H", wd, 32)
    pos = 34 + csw * 2
    cslw, = struct.unpack_from("<H", wd, pos)
    lw_off = pos + 2
    ccp_text, = struct.unpack_from("<I", wd, lw_off + 12)
    pos = lw_off + cslw * 4
    pos += 2
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, pos + 33 * 8)
    clx = table[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 0x01:
        cb, = struct.unpack_from("<H", clx, i + 1)
        i += 3 + cb
    if i >= len(clx) or clx[i] != 0x02:
        raise ValueError("无法定位文本片段表")
    lcb, = struct.unpack_from("<I", clx, i + 1)
    plc = clx[i + 5:i + 5 + lcb]
    n = (lcb - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    parts = []
    total = 0
    for k in range(n):
        off = 4 * (n + 1) + k * 8
        fc_raw, = struct.unpack_from("<I", plc, off + 2)
        cnt = cps[k + 1] - cps[k]
        if fc_raw & 0x40000000:
            fc = (fc_raw & 0x3FFFFFFF) // 2
            parts.append(wd[fc:fc + cnt].decode("cp1252", "replace"))
        else:
            fc = fc_raw & 0x3FFFFFFF
            parts.append(wd[fc:fc + 2 * cnt].decode("utf-16-le", "replace"))
        total += cnt
    text = "".join(parts)
    if 0 < ccp_text <= len(text):
        text = text[:ccp_text]
    return _doc_clean(text)


def extract_ppt_python(path) -> str:
    cfb = CFB(open(path, "rb").read())
    data = cfb.open("PowerPoint Document")
    out = []
    slide = obj = 0
    seen = set()
    stack = []  # (end, type, instance)
    i = 0
    n = len(data)
    while i + 8 <= n:
        while stack and i >= stack[-1][0]:
            stack.pop()
        ver_inst, rtype, rlen = struct.unpack_from("<HHI", data, i)
        ver, inst = ver_inst & 0xF, ver_inst >> 4
        if ver == 0xF:
            stack.append((i + 8 + rlen, rtype, inst))
            if rtype == 0x03EE:  # Slide 容器（文本框里的文字在这里）
                obj += 1
                out.append(f"## 幻灯片 {obj}")
            elif rtype == 0x03F0:  # Notes 容器
                out.append("### 备注")
            i += 8
            continue
        types = {s[1] for s in stack}
        if types & {0x03F8, 0x0FC9}:  # 母版 / 讲义：模板文字，跳过
            i += 8 + rlen
            continue
        slwt = next((s for s in reversed(stack) if s[1] == 0x0FF0), None)
        if rtype == 0x03F3 and slwt is not None:
            if slwt[2] == 0:
                slide += 1
                out.append(f"## 幻灯片 {slide}（占位符文字）")
            elif slwt[2] == 2:
                out.append("### 备注")
        elif rtype in (0x0FA0, 0x0FA8):
            body = data[i + 8:i + 8 + rlen]
            s = body.decode("utf-16-le" if rtype == 0x0FA0 else "latin-1", "replace")
            s = s.replace("\r", "\n").replace("\x0b", "\n").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        i += 8 + rlen
    # 去掉没有内容的标题
    cleaned = []
    for k, line in enumerate(out):
        if line.startswith("#") and (k + 1 >= len(out) or out[k + 1].startswith("#")):
            continue
        cleaned.append(line)
    return "\n\n".join(cleaned).strip()


def _xls_unicode(buf: bytes, pos: int, cch: int, segs=None):
    """读取 BIFF8 字符串字符部分（不含长度），返回 (str, newpos)。"""
    flags = buf[pos]
    pos += 1
    high = flags & 0x01
    rich = flags & 0x08
    ext = flags & 0x04
    nrun = 0
    extlen = 0
    if rich:
        nrun, = struct.unpack_from("<H", buf, pos)
        pos += 2
    if ext:
        extlen, = struct.unpack_from("<I", buf, pos)
        pos += 4
    if high:
        s = buf[pos:pos + 2 * cch].decode("utf-16-le", "replace")
        pos += 2 * cch
    else:
        s = buf[pos:pos + cch].decode("latin-1", "replace")
        pos += cch
    pos += 4 * nrun + extlen
    return s, pos


def _rk(v: int) -> float:
    if v & 2:
        n = struct.unpack("<i", struct.pack("<I", v & 0xFFFFFFFC))[0] >> 2
        n = float(n)
    else:
        n = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return n / 100.0 if v & 1 else n


def _read_sst(records):
    """records: [(data, is_continue)]，处理跨 CONTINUE 记录的字符串。"""
    strings = []
    if not records:
        return strings
    seg_i = 0
    buf = records[0][0]
    pos = 8
    total_unique, = struct.unpack_from("<I", buf, 4)

    def need(k):
        nonlocal buf, pos, seg_i
        if pos + k <= len(buf):
            return True
        if pos >= len(buf) and seg_i + 1 < len(records):
            seg_i += 1
            buf = records[seg_i][0]
            pos = 0
            return pos + k <= len(buf)
        return pos + k <= len(buf)

    while len(strings) < total_unique:
        if not need(3):
            break
        cch, = struct.unpack_from("<H", buf, pos)
        flags = buf[pos + 2]
        pos += 3
        high = flags & 1
        nrun = extlen = 0
        if flags & 0x08:
            need(2)
            nrun, = struct.unpack_from("<H", buf, pos)
            pos += 2
        if flags & 0x04:
            need(4)
            extlen, = struct.unpack_from("<I", buf, pos)
            pos += 4
        chars = []
        remaining = cch
        while remaining > 0:
            if pos >= len(buf):
                if seg_i + 1 >= len(records):
                    remaining = 0
                    break
                seg_i += 1
                buf = records[seg_i][0]
                pos = 0
                high = buf[0] & 1
                pos = 1
            avail = (len(buf) - pos) // (2 if high else 1)
            take = min(avail, remaining)
            if high:
                chars.append(buf[pos:pos + 2 * take].decode("utf-16-le", "replace"))
                pos += 2 * take
            else:
                chars.append(buf[pos:pos + take].decode("latin-1", "replace"))
                pos += take
            remaining -= take
            if take == 0:
                break
        strings.append("".join(chars))
        skip = 4 * nrun + extlen
        while skip > 0:
            avail = len(buf) - pos
            if skip <= avail:
                pos += skip
                skip = 0
            else:
                skip -= avail
                if seg_i + 1 >= len(records):
                    break
                seg_i += 1
                buf = records[seg_i][0]
                pos = 0
    return strings


def extract_xls_python(path, cfg: Config):
    cfb = CFB(open(path, "rb").read())
    try:
        wb = cfb.open("Workbook")
    except KeyError:
        try:
            cfb.open("Book")
            raise ValueError("Excel 5.0/95 旧格式，纯 Python 不支持")
        except KeyError:
            raise ValueError("不是 Excel 97-2003 文件")
    max_rows = cfg.int("excel_max_rows") or 20000
    n = len(wb)
    i = 0
    sheets_meta = []  # (pos, name, hidden, type)
    sst_records = []
    in_sst = False
    formats = {}
    xf_fmt = []
    date1904 = False
    # 第一遍：全局区
    while i + 4 <= n:
        rtype, rlen = struct.unpack_from("<HH", wb, i)
        data = wb[i + 4:i + 4 + rlen]
        if rtype == 0x002F:
            raise ValueError("工作簿已加密（有打开密码）")
        if rtype == 0x0085:
            pos, vis, typ = struct.unpack_from("<IBB", data, 0)
            cch = data[6]
            name, _ = _xls_unicode(data, 7, cch)
            sheets_meta.append((pos, name, vis != 0, typ))
        elif rtype == 0x00FC:
            sst_records = [(data, False)]
            in_sst = True
            i += 4 + rlen
            continue
        elif rtype == 0x003C and in_sst:
            sst_records.append((data, True))
            i += 4 + rlen
            continue
        elif rtype == 0x041E:
            ifmt, cch = struct.unpack_from("<HH", data, 0)
            code, _ = _xls_unicode(data, 4, cch)
            formats[ifmt] = code
        elif rtype == 0x00E0:
            xf_fmt.append(struct.unpack_from("<H", data, 2)[0])
        elif rtype == 0x0022:
            date1904 = struct.unpack_from("<H", data, 0)[0] == 1
        elif rtype == 0x000A:
            break
        in_sst = False
        i += 4 + rlen
    sst = _read_sst(sst_records)
    date_xf = {k for k, f in enumerate(xf_fmt) if f in _DATE_IDS or is_date_format(formats.get(f, ""))}

    def num(v, xf):
        return excel_date(v, date1904) if xf in date_xf else fmt_number(v)

    sheets = []
    for pos, name, hidden, typ in sheets_meta:
        sd = SheetData(name, hidden)
        sheets.append(sd)
        if typ != 0:
            sd.notes.append("（图表/宏表，无单元格数据）")
            continue
        j = pos
        pending_formula = None
        while j + 4 <= n:
            rtype, rlen = struct.unpack_from("<HH", wb, j)
            d = wb[j + 4:j + 4 + rlen]
            j += 4 + rlen
            if rtype == 0x000A:
                break
            if rtype in (0x00FD, 0x0203, 0x027E, 0x0204, 0x0205, 0x0006) and len(d) >= 6:
                r, c, xf = struct.unpack_from("<HHH", d, 0)
                if r >= max_rows:
                    sd.truncated = True
                    continue
                if rtype == 0x00FD:
                    k, = struct.unpack_from("<I", d, 6)
                    if k < len(sst) and sst[k] != "":
                        sd.cells[(r, c)] = sst[k]
                elif rtype == 0x0203:
                    sd.cells[(r, c)] = num(struct.unpack_from("<d", d, 6)[0], xf)
                elif rtype == 0x027E:
                    sd.cells[(r, c)] = num(_rk(struct.unpack_from("<I", d, 6)[0]), xf)
                elif rtype == 0x0204:
                    cch, = struct.unpack_from("<H", d, 6)
                    s, _ = _xls_unicode(d, 8, cch)
                    if s:
                        sd.cells[(r, c)] = s
                elif rtype == 0x0205:
                    val, iserr = d[6], d[7]
                    sd.cells[(r, c)] = ("#ERR" if iserr else ("TRUE" if val else "FALSE"))
                elif rtype == 0x0006:
                    res8 = d[6:14]
                    if res8[6:8] == b"\xff\xff":
                        kind = res8[0]
                        if kind == 0:
                            pending_formula = (r, c)
                        elif kind == 1:
                            sd.cells[(r, c)] = "TRUE" if res8[2] else "FALSE"
                        elif kind == 2:
                            sd.cells[(r, c)] = "#ERR"
                    else:
                        sd.cells[(r, c)] = num(struct.unpack("<d", res8)[0], xf)
            elif rtype == 0x0207 and pending_formula:
                cch, = struct.unpack_from("<H", d, 0)
                s, _ = _xls_unicode(d, 2, cch)
                if s:
                    sd.cells[pending_formula] = s
                pending_formula = None
            elif rtype == 0x00BD and len(d) >= 6:
                r, c0 = struct.unpack_from("<HH", d, 0)
                if r >= max_rows:
                    sd.truncated = True
                    continue
                cnt = (len(d) - 6) // 6
                for k in range(cnt):
                    xf, rkv = struct.unpack_from("<HI", d, 4 + 6 * k)
                    sd.cells[(r, c0 + k)] = num(_rk(rkv), xf)
            elif rtype == 0x00E5 and len(d) >= 2:
                cnt, = struct.unpack_from("<H", d, 0)
                for k in range(cnt):
                    if 2 + 8 * k + 8 <= len(d):
                        r1, r2, c1, c2 = struct.unpack_from("<HHHH", d, 2 + 8 * k)
                        sd.merges.append((r1, c1, r2, c2))
    return sheets


# ---------- 用本机 Office / WPS 把旧格式转换为新格式（Windows，可选） ----------

_PS_CONVERT = r"""
$ErrorActionPreference = 'Stop'
$src = $env:CR_SRC; $dst = $env:CR_DST; $kind = $env:CR_KIND
$app = $null
function TryCom($ids) { foreach ($i in $ids) { try { return New-Object -ComObject $i } catch {} } return $null }
try {
  if ($kind -eq 'word') {
    $app = TryCom @('Word.Application','KWps.Application','Wps.Application')
    if (-not $app) { throw 'NO_OFFICE' }
    $app.Visible = $false; try { $app.DisplayAlerts = 0 } catch {}
    $doc = $app.Documents.Open($src, $false, $true, $false, '~')
    try { $doc.SaveAs2($dst, 16) } catch { $doc.SaveAs([ref]$dst, [ref]16) }
    $doc.Close([ref]0)
  } elseif ($kind -eq 'excel') {
    $app = TryCom @('Excel.Application','KET.Application','ET.Application')
    if (-not $app) { throw 'NO_OFFICE' }
    $app.Visible = $false; $app.DisplayAlerts = $false
    $wb = $app.Workbooks.Open($src, 0, $true, 5, '~')
    $wb.SaveAs($dst, 51); $wb.Close($false)
  } else {
    $app = TryCom @('PowerPoint.Application','KWPP.Application','WPP.Application')
    if (-not $app) { throw 'NO_OFFICE' }
    $p = $app.Presentations.Open($src, -1, 0, 0)
    $p.SaveAs($dst, 24); $p.Close()
  }
  Write-Output 'OK'
} finally {
  if ($app) { try { $app.Quit() } catch {}; [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) }
}
"""


def convert_with_office(path: str, kind: str, timeout=180):
    """返回转换后的 docx/xlsx/pptx 路径；失败抛异常。"""
    if os.name != "nt":
        raise RuntimeError("非 Windows 系统，无法调用 Office/WPS")
    ext = {"word": ".docx", "excel": ".xlsx", "ppt": ".pptx"}[kind]
    tmpdir = tempfile.mkdtemp(prefix="cr_conv_")
    dst = os.path.join(tmpdir, "converted" + ext)
    env = dict(os.environ, CR_SRC=os.path.abspath(path), CR_DST=dst, CR_KIND=kind)
    enc = base64.b64encode(_PS_CONVERT.encode("utf-16-le")).decode()
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                           env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Office/WPS 转换超时（{timeout}s），可能有弹窗卡住，请在任务管理器结束 WINWORD/EXCEL/WPS 进程")
    if not os.path.exists(dst):
        err = (r.stderr or b"").decode("mbcs" if os.name == "nt" else "utf-8", "replace")[-300:]
        if "NO_OFFICE" in err:
            raise RuntimeError("本机未找到 Office/WPS 的 COM 组件")
        raise RuntimeError("Office/WPS 转换失败：" + err.strip())
    return dst


# ---------- 其它格式 ----------

def extract_eml(path) -> str:
    msg = email.message_from_bytes(open(path, "rb").read(), policy=email.policy.default)
    out = [f"# 邮件：{msg.get('subject', '')}",
           "\n".join(f"- {k}：{msg.get(h)}" for k, h in (("发件人", "from"), ("收件人", "to"), ("抄送", "cc"),
                                                        ("时间", "date")) if msg.get(h))]
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        if body.get_content_type() == "text/html":
            content = HtmlToMarkdown().convert(content)
        out.append(content)
    atts = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    if atts:
        out.append("附件：" + ", ".join(atts))
    return "\n\n".join(out)


def extract_zip_listing(path) -> str:
    with zipfile.ZipFile(path) as z:
        lines = [f"# 压缩包目录：{os.path.basename(path)}", "", "| 文件 | 大小 |", "|---|---|"]
        for info in z.infolist()[:500]:
            name = info.filename
            if not (info.flag_bits & 0x800):
                try:
                    name = name.encode("cp437").decode("gbk")
                except Exception:
                    pass
            lines.append(f"| {md_cell(name)} | {human_size(info.file_size)} |")
        if len(z.infolist()) > 500:
            lines.append(f"…… 共 {len(z.infolist())} 个文件，仅列前 500 个")
    return "\n".join(lines)


# =============================================================================
# 6. PDF 文本 / 图片提取（纯 Python）
# =============================================================================

class PdfName(str):
    pass


class PdfKw(str):
    pass


class PdfRef:
    __slots__ = ("num", "gen")

    def __init__(self, num, gen):
        self.num, self.gen = num, gen

    def __repr__(self):
        return f"{self.num} {self.gen} R"


class PdfStream:
    __slots__ = ("dict", "raw", "num", "gen")

    def __init__(self, d, raw, num=None, gen=0):
        self.dict, self.raw = d, raw
        self.num, self.gen = num, gen

    def get(self, k, default=None):
        return self.dict.get(k, default)


_PDF_TOKEN = re.compile(rb"""
 (?P<ws>(?:[\x00\t\n\f\r ]|%[^\r\n]*)+)
|(?P<name>/[^\x00\t\n\f\r ()<>\[\]{}/%]*)
|(?P<dopen><<)|(?P<dclose>>>)
|(?P<hex><[0-9A-Fa-f\x00\t\n\f\r ]*>)
|(?P<lp>\()
|(?P<num>[+-]?(?:\d+\.?\d*|\.\d+))(?![^\x00\t\n\f\r ()<>\[\]{}/%])
|(?P<delim>[\[\]{}])
|(?P<kw>[^\x00\t\n\f\r ()<>\[\]{}/%]+)
""", re.X)
_PDF_STR_SPECIAL = re.compile(rb"[()\\]")
_ESC = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f",
        ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}


def _pdf_literal(data: bytes, i: int):
    depth = 1
    out = bytearray()
    n = len(data)
    while i < n:
        m = _PDF_STR_SPECIAL.search(data, i)
        if not m:
            out += data[i:]
            return bytes(out), n
        j = m.start()
        out += data[i:j]
        c = data[j]
        if c == 0x5C:
            if j + 1 >= n:
                return bytes(out), n
            nx = data[j + 1]
            if nx in _ESC:
                out += _ESC[nx]
                i = j + 2
            elif 0x30 <= nx <= 0x37:
                k = j + 1
                digs = b""
                while k < n and len(digs) < 3 and 0x30 <= data[k] <= 0x37:
                    digs += data[k:k + 1]
                    k += 1
                out.append(int(digs, 8) & 0xFF)
                i = k
            elif nx == 0x0D:
                i = j + 3 if data[j + 2:j + 3] == b"\n" else j + 2
            elif nx == 0x0A:
                i = j + 2
            else:
                out.append(nx)
                i = j + 2
        elif c == 0x28:
            depth += 1
            out.append(c)
            i = j + 1
        else:
            depth -= 1
            if depth == 0:
                return bytes(out), j + 1
            out.append(c)
            i = j + 1
    return bytes(out), n


def _pdf_name(raw: bytes) -> PdfName:
    if b"#" in raw:
        raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
    return PdfName(raw.decode("latin-1"))


class PdfLexer:
    def __init__(self, data: bytes, pos: int = 0, content: bool = False):
        self.data = data
        self.pos = pos
        self.n = len(data)
        self.content = content  # 内容流里没有间接引用，跳过 "n g R" 预读以提速

    def token(self):
        while True:
            if self.pos >= self.n:
                return None
            m = _PDF_TOKEN.match(self.data, self.pos)
            if not m:
                self.pos += 1
                continue
            kind = m.lastgroup
            self.pos = m.end()
            if kind == "ws":
                continue
            if kind == "name":
                return _pdf_name(m.group()[1:])
            if kind == "num":
                s = m.group()
                try:
                    return int(s)
                except ValueError:
                    try:
                        return float(s)
                    except ValueError:
                        return 0
            if kind == "lp":
                s, self.pos = _pdf_literal(self.data, self.pos)
                return s
            if kind == "hex":
                h = re.sub(rb"[^0-9A-Fa-f]", b"", m.group()[1:-1])
                if len(h) % 2:
                    h += b"0"
                return bytes.fromhex(h.decode())
            if kind in ("dopen", "dclose", "delim"):
                return PdfKw(m.group().decode())
            return PdfKw(m.group().decode("latin-1"))

    def value(self, tok=None):
        if tok is None:
            tok = self.token()
        if isinstance(tok, PdfKw):
            if tok == "<<":
                d = {}
                while True:
                    k = self.token()
                    if k is None or k == ">>":
                        return d
                    if isinstance(k, PdfName):
                        d[str(k)] = self.value()
            if tok == "[":
                arr = []
                while True:
                    t = self.token()
                    if t is None or t == "]":
                        return arr
                    arr.append(self.value(t))
            if tok == "true":
                return True
            if tok == "false":
                return False
            if tok == "null":
                return None
            return tok
        if isinstance(tok, int) and not self.content:
            save = self.pos
            t2 = self.token()
            if isinstance(t2, int) and not isinstance(t2, bool):
                t3 = self.token()
                if t3 == "R":
                    return PdfRef(tok, t2)
            self.pos = save
        return tok


def _lzw_decode(data: bytes) -> bytes:
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    bitlen = 9
    buf = 0
    nbits = 0
    prev = b""
    for byte in data:
        buf = (buf << 8) | byte
        nbits += 8
        while nbits >= bitlen:
            nbits -= bitlen
            code = (buf >> nbits) & ((1 << bitlen) - 1)
            if code == 256:
                table = table[:258]
                bitlen = 9
                prev = b""
                continue
            if code == 257:
                return bytes(out)
            if code < len(table):
                entry = table[code]
                if prev:
                    table.append(prev + entry[:1])
            elif prev:
                entry = prev + prev[:1]
                table.append(entry)
            else:
                continue
            out += entry
            prev = entry
            if len(table) + 1 >= (1 << bitlen) and bitlen < 12:
                bitlen += 1
    return bytes(out)


def _png_unpredict(data: bytes, colors: int, bpc: int, columns: int) -> bytes:
    bpp = max(1, colors * bpc // 8)
    stride = (columns * colors * bpc + 7) // 8
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    while i < len(data):
        ft = data[i]
        row = bytearray(data[i + 1:i + 1 + stride])
        if len(row) < stride:
            row += bytes(stride - len(row))
        i += 1 + stride
        if ft == 1:
            for x in range(bpp, stride):
                row[x] = (row[x] + row[x - bpp]) & 0xFF
        elif ft == 2:
            for x in range(stride):
                row[x] = (row[x] + prev[x]) & 0xFF
        elif ft == 3:
            for x in range(stride):
                left = row[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif ft == 4:
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                row[x] = (row[x] + pr) & 0xFF
        out += row
        prev = row
    return bytes(out)


def _flate(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        d = zlib.decompressobj(wbits)
        out = bytearray()
        try:
            for k in range(0, len(data), 4096):
                out += d.decompress(data[k:k + 4096])
        except zlib.error:
            pass
        if out:
            return bytes(out)
    return b""


_GLYPHS = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "quotesingle": "'", "quoteright": "’", "quoteleft": "‘", "parenleft": "(",
    "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-", "minus": "-", "period": ".",
    "slash": "/", "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "colon": ":", "semicolon": ";", "less": "<", "equal": "=",
    "greater": ">", "question": "?", "at": "@", "bracketleft": "[", "backslash": "\\", "bracketright": "]",
    "asciicircum": "^", "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}",
    "asciitilde": "~", "bullet": "•", "endash": "–", "emdash": "—", "quotedblleft": "“",
    "quotedblright": "”", "ellipsis": "…", "trademark": "™", "copyright": "©", "registered": "®",
    "degree": "°", "fi": "fi", "fl": "fl", "nbspace": " ", "periodcentered": "·", "multiply": "×",
    "divide": "÷", "section": "§", "paragraph": "¶", "dagger": "†", "yen": "¥", "sterling": "£",
}


def _glyph_to_char(name: str) -> str:
    if name in _GLYPHS:
        return _GLYPHS[name]
    if len(name) == 1:
        return name
    m = re.match(r"^uni([0-9A-Fa-f]{4,})", name)
    if m:
        try:
            h = m.group(1)
            return "".join(chr(int(h[i:i + 4], 16)) for i in range(0, len(h) - len(h) % 4, 4))
        except ValueError:
            return ""
    m = re.match(r"^u([0-9A-Fa-f]{4,6})$", name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return ""
    return ""


def _parse_cmap(data: bytes):
    cmap = {}
    spaces = []
    for blk in re.finditer(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        for lo, hi in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk.group(1)):
            spaces.append((len(lo) // 2, int(lo, 16), int(hi, 16)))

    def u16(h: bytes) -> str:
        if len(h) % 2:
            h = b"0" + h
        b = bytes.fromhex(h.decode())
        if len(b) % 2:
            b = b"\x00" + b
        return b.decode("utf-16-be", "replace")

    src_len = 0
    for blk in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", blk.group(1)):
            cmap[int(src, 16)] = u16(dst) if dst else ""
            src_len = max(src_len, len(src) // 2)
    for blk in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])",
                                      blk.group(1)):
            a, b = int(lo, 16), int(hi, 16)
            src_len = max(src_len, len(lo) // 2)
            if b - a > 0xFFFF:
                b = a + 0xFFFF
            if dst.startswith(b"<"):
                h = dst[1:-1]
                if len(h) % 2:
                    h = b"0" + h
                base = int(h, 16) if h else 0
                blen = max(2, len(h) // 2)
                for k in range(b - a + 1):
                    try:
                        cmap[a + k] = (base + k).to_bytes(blen, "big").decode("utf-16-be", "replace")
                    except OverflowError:
                        break
            else:
                for k, h in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", dst)):
                    cmap[a + k] = u16(h)
    if not spaces and src_len:
        spaces = [(src_len, 0, (1 << (8 * src_len)) - 1)]
    return cmap, spaces


class PdfFont:
    def __init__(self, pdf, fd: dict):
        r = pdf.resolve
        self.subtype = str(r(fd.get("Subtype", "")))
        self.type0 = self.subtype == "Type0"
        self.cmap = {}
        self.spaces = []
        self.mbcs = None
        self.ucs2 = False
        self.diffs = {}
        self.base_enc = "latin-1"
        self.widths = {}
        self.default_w = 1000 if self.type0 else 500
        tu = r(fd.get("ToUnicode"))
        if isinstance(tu, PdfStream):
            try:
                self.cmap, self.spaces = _parse_cmap(pdf.decode(tu)[0])
            except Exception:
                pass
        enc = r(fd.get("Encoding"))
        if self.type0:
            en = str(enc) if isinstance(enc, PdfName) else ""
            if "UCS2" in en or "UTF16" in en:
                self.ucs2 = True
            elif re.match(r"^(GBK|GBpc|GB-|GBK2K|GBT)", en):
                self.mbcs = "gb18030"
            elif re.match(r"^(B5|ETen|HKscs|CNS)", en):
                self.mbcs = "big5"
            elif "RKSJ" in en:
                self.mbcs = "shift_jis"
            elif re.match(r"^(KSC|KSCms)", en):
                self.mbcs = "euc-kr"
            desc = r(fd.get("DescendantFonts"))
            if isinstance(desc, list) and desc:
                df = r(desc[0])
                if isinstance(df, dict):
                    dw = r(df.get("DW"))
                    if isinstance(dw, (int, float)):
                        self.default_w = dw
                    w = r(df.get("W"))
                    if isinstance(w, list):
                        self._parse_w([r(x) for x in w])
        else:
            if isinstance(enc, PdfName):
                self.base_enc = {"WinAnsiEncoding": "cp1252", "MacRomanEncoding": "mac_roman"}.get(str(enc),
                                                                                                   "latin-1")
            elif isinstance(enc, dict):
                be = r(enc.get("BaseEncoding"))
                if isinstance(be, PdfName):
                    self.base_enc = {"WinAnsiEncoding": "cp1252", "MacRomanEncoding": "mac_roman"}.get(str(be),
                                                                                                       "latin-1")
                diffs = r(enc.get("Differences"))
                if isinstance(diffs, list):
                    code = 0
                    for x in diffs:
                        x = r(x)
                        if isinstance(x, int):
                            code = x
                        elif isinstance(x, PdfName):
                            self.diffs[code] = _glyph_to_char(str(x))
                            code += 1
            fc = r(fd.get("FirstChar"))
            ws = r(fd.get("Widths"))
            if isinstance(fc, int) and isinstance(ws, list):
                for k, w in enumerate(ws):
                    w = r(w)
                    if isinstance(w, (int, float)):
                        self.widths[fc + k] = w
            desc = r(fd.get("FontDescriptor"))
            if isinstance(desc, dict):
                mw = r(desc.get("MissingWidth"))
                if isinstance(mw, (int, float)) and mw > 0:
                    self.default_w = mw

    def _parse_w(self, w):
        i = 0
        while i < len(w):
            a = w[i]
            if i + 1 < len(w) and isinstance(w[i + 1], list):
                for k, x in enumerate(w[i + 1]):
                    if isinstance(x, (int, float)):
                        self.widths[a + k] = x
                i += 2
            elif i + 2 < len(w):
                b, x = w[i + 1], w[i + 2]
                if isinstance(a, int) and isinstance(b, int) and isinstance(x, (int, float)) and b - a < 70000:
                    for c in range(a, b + 1):
                        self.widths[c] = x
                i += 3
            else:
                break

    def decode(self, s: bytes):
        """返回 [(code, text)]。"""
        out = []
        if self.mbcs and not self.cmap:
            return [(None, s.decode(self.mbcs, "replace"))]
        i = 0
        n = len(s)
        if self.spaces:
            lens = sorted({sp[0] for sp in self.spaces})
        else:
            lens = [2] if self.type0 else [1]
        while i < n:
            code = None
            L = lens[-1]
            for ln in lens:
                if i + ln > n:
                    break
                c = int.from_bytes(s[i:i + ln], "big")
                if not self.spaces or any(sl == ln and lo <= c <= hi for sl, lo, hi in self.spaces):
                    code, L = c, ln
                    break
            if code is None:
                L = min(L, n - i)
                code = int.from_bytes(s[i:i + L], "big")
            i += L
            if code in self.cmap:
                ch = self.cmap[code]
            elif self.type0:
                ch = chr(code) if self.ucs2 and code < 0x110000 else ""
            elif code in self.diffs:
                ch = self.diffs[code]
            else:
                ch = bytes([code & 0xFF]).decode(self.base_enc, "replace")
            out.append((code, ch))
        return out

    def width(self, code) -> float:
        if code is None:
            return self.default_w
        return self.widths.get(code, self.default_w)


_STREAM_KW = re.compile(rb"[\x00\t\n\f\r ]*stream(\r\n|\n|\r)?")


# ---------- PDF 解密（仅限“无打开密码”的加密 PDF，即只限制复制/打印的那种）----------

def _aes_tables():
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1 | q >> 7) & 0xFF) ^ ((q << 2 | q >> 6) & 0xFF) ^ ((q << 3 | q >> 5) & 0xFF) ^ (
            (q << 4 | q >> 4) & 0xFF)
        sbox[p] = x ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i

    def mul(a, b):
        r = 0
        while b:
            if b & 1:
                r ^= a
            a = ((a << 1) ^ (0x1B if a & 0x80 else 0)) & 0xFF
            b >>= 1
        return r

    m = {k: [mul(i, k) for i in range(256)] for k in (2, 3, 9, 11, 13, 14)}
    return sbox, inv, m


_AES = None


class _AES_:
    def __init__(self, key: bytes):
        global _AES
        if _AES is None:
            _AES = _aes_tables()
        self.sbox, self.inv, self.m = _AES
        nk = len(key) // 4
        self.nr = nk + 6
        w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
        rcon = 1
        for i in range(nk, 4 * (self.nr + 1)):
            t = list(w[i - 1])
            if i % nk == 0:
                t = [self.sbox[b] for b in t[1:] + t[:1]]
                t[0] ^= rcon
                rcon = ((rcon << 1) ^ (0x1B if rcon & 0x80 else 0)) & 0xFF
            elif nk > 6 and i % nk == 4:
                t = [self.sbox[b] for b in t]
            w.append([w[i - nk][j] ^ t[j] for j in range(4)])
        self.rk = [sum(w[4 * r:4 * r + 4], []) for r in range(self.nr + 1)]

    def enc(self, b):
        sb, m2, m3, rk = self.sbox, self.m[2], self.m[3], self.rk
        s = [b[i] ^ rk[0][i] for i in range(16)]
        for r in range(1, self.nr + 1):
            s = [sb[x] for x in s]
            s = [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3], s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]
            if r != self.nr:
                t = []
                for c in range(0, 16, 4):
                    a0, a1, a2, a3 = s[c:c + 4]
                    t += [m2[a0] ^ m3[a1] ^ a2 ^ a3, a0 ^ m2[a1] ^ m3[a2] ^ a3, a0 ^ a1 ^ m2[a2] ^ m3[a3],
                          m3[a0] ^ a1 ^ a2 ^ m2[a3]]
                s = t
            k = rk[r]
            s = [s[i] ^ k[i] for i in range(16)]
        return s

    def dec(self, b):
        iv, rk = self.inv, self.rk
        m9, m11, m13, m14 = self.m[9], self.m[11], self.m[13], self.m[14]
        s = [b[i] ^ rk[self.nr][i] for i in range(16)]
        for r in range(self.nr - 1, -1, -1):
            s = [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11], s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]
            k = rk[r]
            s = [iv[s[i]] ^ k[i] for i in range(16)]
            if r:
                t = []
                for c in range(0, 16, 4):
                    a0, a1, a2, a3 = s[c:c + 4]
                    t += [m14[a0] ^ m11[a1] ^ m13[a2] ^ m9[a3], m9[a0] ^ m14[a1] ^ m11[a2] ^ m13[a3],
                          m13[a0] ^ m9[a1] ^ m14[a2] ^ m11[a3], m11[a0] ^ m13[a1] ^ m9[a2] ^ m14[a3]]
                s = t
        return s

    def cbc_decrypt(self, iv: bytes, data: bytes) -> bytes:
        out = bytearray()
        prev = iv
        for i in range(0, len(data) - len(data) % 16, 16):
            blk = data[i:i + 16]
            d = self.dec(blk)
            out += bytes(d[j] ^ prev[j] for j in range(16))
            prev = blk
        return bytes(out)

    def cbc_encrypt(self, iv: bytes, data: bytes) -> bytes:
        out = bytearray()
        prev = list(iv)
        for i in range(0, len(data), 16):
            e = self.enc([data[i + j] ^ prev[j] for j in range(16)])
            out += bytes(e)
            prev = e
        return bytes(out)


def _rc4(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    kl = len(key)
    for i in range(256):
        j = (j + S[i] + key[i % kl]) & 0xFF
        S[i], S[j] = S[j], S[i]
    i = j = 0
    out = bytearray(len(data))
    for k, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out[k] = b ^ S[(S[i] + S[j]) & 0xFF]
    return bytes(out)


_PDF_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


class PdfCrypt:
    """标准安全处理器，空用户密码。支持 RC4(40~128) / AES-128 / AES-256(R5/R6)。"""

    def __init__(self, enc: dict, id0: bytes, resolve):
        import hashlib
        self.h = hashlib
        r = resolve
        if str(r(enc.get("Filter", ""))) != "Standard":
            raise ValueError("非标准加密方式（可能是证书加密）")
        self.V = r(enc.get("V", 0)) or 0
        self.R = r(enc.get("R", 2)) or 2
        O = r(enc.get("O", b"")) or b""
        U = r(enc.get("U", b"")) or b""
        P = r(enc.get("P", 0)) or 0
        self.method = "RC4"
        self.stm_identity = False
        if self.V >= 4:
            cf = r(enc.get("CF")) or {}
            stmf = str(r(enc.get("StmF", "Identity")))
            if stmf == "Identity":
                self.stm_identity = True
            else:
                cfd = r(cf.get(stmf)) if isinstance(cf, dict) else None
                cfm = str(r(cfd.get("CFM", "V2"))) if isinstance(cfd, dict) else "V2"
                self.method = {"AESV2": "AESV2", "AESV3": "AESV3"}.get(cfm, "RC4")
        if self.R >= 5:
            self.method = "AESV3"
            UE = r(enc.get("UE", b"")) or b""
            if self.R == 5:
                ok = self.h.sha256(U[32:40]).digest() == U[:32]
                ik = self.h.sha256(U[40:48]).digest()
            else:
                ok = self._hash6(b"", U[32:40], b"") == U[:32]
                ik = self._hash6(b"", U[40:48], b"")
            if not ok:
                raise ValueError("PDF 设置了打开密码")
            self.key = _AES_(ik).cbc_decrypt(bytes(16), UE[:32])
            return
        length = r(enc.get("Length", 40)) or 40
        n = 5 if self.R == 2 else max(5, min(16, length // 8))
        md = self.h.md5(_PDF_PAD + O[:32] + struct.pack("<I", P & 0xFFFFFFFF) + id0)
        if self.R >= 4 and r(enc.get("EncryptMetadata", True)) is False:
            md.update(b"\xff\xff\xff\xff")
        d = md.digest()
        if self.R >= 3:
            for _ in range(50):
                d = self.h.md5(d[:n]).digest()
        self.key = d[:n]
        if self.R == 2:
            ok = _rc4(self.key, _PDF_PAD) == U[:32]
        else:
            x = _rc4(self.key, self.h.md5(_PDF_PAD + id0).digest())
            for i in range(1, 20):
                x = _rc4(bytes(b ^ i for b in self.key), x)
            ok = x == U[:16]
        if not ok:
            raise ValueError("PDF 设置了打开密码")

    def _hash6(self, pw, salt, udata):
        k = self.h.sha256(pw + salt + udata).digest()
        i = 0
        while True:
            k1 = (pw + k + udata) * 64
            e = _AES_(k[:16]).cbc_encrypt(k[16:32], k1)
            k = (self.h.sha256, self.h.sha384, self.h.sha512)[sum(e[:16]) % 3](e).digest()
            i += 1
            if i >= 64 and e[-1] <= i - 32:
                break
        return k[:32]

    def decrypt(self, num: int, gen: int, data: bytes) -> bytes:
        if self.stm_identity or not data:
            return data
        if self.method == "AESV3":
            key = self.key
        else:
            ext = self.key + struct.pack("<I", num)[:3] + struct.pack("<I", gen)[:2]
            if self.method == "AESV2":
                ext += b"sAlT"
            key = self.h.md5(ext).digest()[:min(len(self.key) + 5, 16)]
        if self.method == "RC4":
            return _rc4(key, data)
        if len(data) < 32:
            return b""
        out = _AES_(key).cbc_decrypt(data[:16], data[16:])
        pad = out[-1] if out else 0
        return out[:-pad] if 0 < pad <= 16 else out


class PDFDoc:
    OBJ_RE = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")

    def __init__(self, data: bytes):
        k = data.find(b"%PDF")
        if k < 0 or k > 1024:
            raise ValueError("不是 PDF 文件")
        self.data = data
        self.offsets = {}
        self.gens = {}
        for m in self.OBJ_RE.finditer(data):
            self.offsets[int(m.group(1))] = m.end()
            self.gens[int(m.group(1))] = int(m.group(2))
        self.cache = {}
        self.objstm = {}
        self._objstm_data = {}
        self._loading = set()
        self.crypt = None
        self.crypt_error = ""
        encs = list(re.finditer(rb"/Encrypt\s*(\d+)\s+\d+\s+R", data))
        self.encrypted = bool(encs) or bool(re.search(rb"/Encrypt\s*<<", data))
        if self.encrypted:
            try:
                if encs:
                    enc = self.get(int(encs[-1].group(1)))
                else:
                    k2 = data.rfind(b"/Encrypt")
                    lx = PdfLexer(data, k2 + 8)
                    enc = lx.value()
                id0 = b""
                ids = list(re.finditer(rb"/ID\s*\[\s*", data))
                if ids:
                    v = PdfLexer(data, ids[-1].end()).value()
                    id0 = v if isinstance(v, bytes) else b""
                self.cache.clear()
                self.crypt = PdfCrypt(enc, id0, self.resolve)
            except Exception as e:
                self.crypt_error = str(e)
            self.cache.clear()
        self._find_objstms()
        roots = re.findall(rb"/Root\s+(\d+)\s+(\d+)\s+R", data)
        self.root_num = int(roots[-1][0]) if roots else None

    def _find_objstms(self):
        for num, off in list(self.offsets.items()):
            if b"/ObjStm" not in self.data[off:off + 400]:
                continue
            try:
                st = self.get(num)
                if not isinstance(st, PdfStream) or st.get("Type") != "ObjStm":
                    continue
                content = self.decode(st)[0]
                first = self.resolve(st.get("First"))
                nn = self.resolve(st.get("N"))
                head = content[:first].split()
                self._objstm_data[num] = (content, first)
                for k in range(min(nn, len(head) // 2)):
                    onum, ooff = int(head[2 * k]), int(head[2 * k + 1])
                    if onum not in self.offsets:
                        self.objstm[onum] = (num, first + ooff)
            except Exception:
                continue

    def get(self, num):
        if num in self.cache:
            return self.cache[num]
        if num in self._loading:
            return None
        self._loading.add(num)
        try:
            val = None
            if num in self.offsets:
                lx = PdfLexer(self.data, self.offsets[num])
                val = lx.value()
                if isinstance(val, dict):
                    m = _STREAM_KW.match(self.data, lx.pos)
                    if m:
                        start = m.end()
                        length = self.resolve(val.get("Length"))
                        raw = None
                        if isinstance(length, int) and length >= 0:
                            tail = self.data[start + length:start + length + 32]
                            if b"endstream" in tail:
                                raw = self.data[start:start + length]
                        if raw is None:
                            end = self.data.find(b"endstream", start)
                            raw = self.data[start:end if end > 0 else len(self.data)].rstrip(b"\r\n")
                        val = PdfStream(val, raw, num, self.gens.get(num, 0))
            elif num in self.objstm:
                sn, off = self.objstm[num]
                content = self._objstm_data[sn][0]
                val = PdfLexer(content, off).value()
            self.cache[num] = val
            return val
        finally:
            self._loading.discard(num)

    def resolve(self, v, depth=0):
        while isinstance(v, PdfRef) and depth < 32:
            v = self.get(v.num)
            depth += 1
        return v

    def decode(self, st: PdfStream, for_image=False):
        """返回 (bytes, image_filter)。image_filter 为 DCT/JPX/CCITT/JBIG2 时 bytes 为原始编码数据。"""
        filters = self.resolve(st.get("Filter"))
        parms = self.resolve(st.get("DecodeParms"))
        if filters is None:
            filters = []
        elif not isinstance(filters, list):
            filters = [filters]
        if not isinstance(parms, list):
            parms = [parms] * len(filters)
        data = st.raw
        if self.crypt is not None and st.num is not None and st.get("Type") != "XRef":
            data = self.crypt.decrypt(st.num, st.gen, data)
        for idx, f in enumerate(filters):
            f = str(self.resolve(f))
            p = self.resolve(parms[idx]) if idx < len(parms) else None
            p = p if isinstance(p, dict) else {}
            if f in ("FlateDecode", "Fl"):
                data = _flate(data)
            elif f in ("LZWDecode", "LZW"):
                data = _lzw_decode(data)
            elif f in ("ASCIIHexDecode", "AHx"):
                h = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])
                data = bytes.fromhex((h + b"0" * (len(h) % 2)).decode())
            elif f in ("ASCII85Decode", "A85"):
                s = re.sub(rb"\s", b"", data)
                if s.startswith(b"<~"):
                    s = s[2:]
                s = s.split(b"~>")[0]
                data = base64.a85decode(s)
            elif f in ("RunLengthDecode", "RL"):
                out = bytearray()
                i = 0
                while i < len(data):
                    L = data[i]
                    if L == 128:
                        break
                    if L < 128:
                        out += data[i + 1:i + 2 + L]
                        i += 2 + L
                    else:
                        out += data[i + 1:i + 2] * (257 - L)
                        i += 2
                data = bytes(out)
            elif f in ("DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode"):
                return data, f
            else:
                raise ValueError(f"不支持的 PDF 过滤器 {f}")
            pred = self.resolve(p.get("Predictor", 1)) or 1
            if pred >= 10 and f in ("FlateDecode", "Fl", "LZWDecode", "LZW") and not for_image:
                data = _png_unpredict(data, self.resolve(p.get("Colors", 1)) or 1,
                                      self.resolve(p.get("BitsPerComponent", 8)) or 8,
                                      self.resolve(p.get("Columns", 1)) or 1)
        return data, None

    def pages(self):
        out = []
        seen = set()

        def walk(ref, inherited, depth=0):
            node = self.resolve(ref)
            if not isinstance(node, dict) or depth > 60:
                return
            key = ref.num if isinstance(ref, PdfRef) else id(node)
            if key in seen:
                return
            seen.add(key)
            res = node.get("Resources", inherited)
            kids = self.resolve(node.get("Kids"))
            if node.get("Type") == "Pages" or isinstance(kids, list):
                for k in kids or []:
                    walk(k, res, depth + 1)
            else:
                out.append((node, self.resolve(res)))

        if self.root_num is not None:
            root = self.get(self.root_num)
            if isinstance(root, dict):
                walk(root.get("Pages"), None)
        if not out:
            for num in sorted(set(self.offsets) | set(self.objstm)):
                o = self.get(num)
                if isinstance(o, dict) and o.get("Type") == "Page":
                    out.append((o, self.resolve(o.get("Resources"))))
        return out


class PdfTextExtractor:
    def __init__(self, pdf: PDFDoc):
        self.pdf = pdf
        self.font_cache = {}

    def font(self, ref):
        key = ref.num if isinstance(ref, PdfRef) else id(ref)
        if key not in self.font_cache:
            fd = self.pdf.resolve(ref)
            self.font_cache[key] = PdfFont(self.pdf, fd) if isinstance(fd, dict) else None
        return self.font_cache[key]

    def page_text(self, page: dict, resources) -> str:
        r = self.pdf.resolve
        contents = r(page.get("Contents"))
        if isinstance(contents, PdfStream):
            contents = [contents]
        data = b""
        if isinstance(contents, list):
            chunks = []
            for c in contents:
                c = r(c)
                if isinstance(c, PdfStream):
                    try:
                        chunks.append(self.pdf.decode(c)[0])
                    except Exception:
                        pass
            data = b"\n".join(chunks)
        self.lines = []
        self.cur = []
        self.last_y = None
        self.last_x_end = None
        self.force_nl = False
        self.run(data, resources if isinstance(resources, dict) else {}, 0)
        self.newline()
        text = "\n".join(self.lines)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def newline(self, blank=False):
        line = "".join(self.cur).rstrip()
        self.lines.append(line)
        if blank:
            self.lines.append("")
        self.cur = []

    def run(self, data: bytes, res: dict, depth: int):
        r = self.pdf.resolve
        fonts = r(res.get("Font")) or {}
        xobjs = r(res.get("XObject")) or {}
        if not isinstance(fonts, dict):
            fonts = {}
        if not isinstance(xobjs, dict):
            xobjs = {}
        lx = PdfLexer(data, content=True)
        operands = []
        font = None
        size = 1.0
        tm = [1, 0, 0, 1, 0, 0]
        tlm = [1, 0, 0, 1, 0, 0]
        leading = 0.0
        stack = []

        def moved():
            pass  # 换行/空格只在真正输出文字时根据位置判断（见 show）

        def show(s: bytes):
            if font is None:
                return
            fs_v = abs(size * (tm[3] or tm[0])) or 10
            fs_h = abs(size * (tm[0] or tm[3])) or 10
            x, y = tm[4], tm[5]
            if self.last_y is not None:
                dy = abs(y - self.last_y)
                if self.force_nl or dy > max(fs_v * 0.5, 1.0):
                    self.newline(blank=dy > fs_v * 2.2)
                elif self.last_x_end is not None and (x > self.last_x_end + fs_h * 0.2
                                                      or x < self.last_x_end - fs_h * 4):
                    if self.cur and not self.cur[-1].endswith(" "):
                        self.cur.append(" ")
            elif self.force_nl and self.cur:
                self.newline()
            self.force_nl = False
            adv = 0.0
            for code, ch in font.decode(s):
                if ch and "" <= ch[0] <= "":  # Symbol/Wingdings 私用区符号（项目符号等）
                    ch = {"": "•", "": "▪", "": "➢", "": "✓", "": "●",
                          "": "■", "": "◆", "": "❖"}.get(ch[0], "•")
                self.cur.append(ch)
                adv += font.width(code) / 1000.0 * fs_h
            tm[4] += adv
            self.last_y = y
            self.last_x_end = tm[4]

        while True:
            tok = lx.token()
            if tok is None:
                break
            if isinstance(tok, PdfKw) and tok not in ("[", "<<", "true", "false", "null", "]", ">>", "{", "}"):
                op = str(tok)
                try:
                    if op == "BT":
                        tm = [1, 0, 0, 1, 0, 0]
                        tlm = list(tm)
                    elif op == "Tf" and len(operands) >= 2:
                        fref = fonts.get(str(operands[-2]))
                        font = self.font(fref) if fref is not None else None
                        size = float(operands[-1]) if isinstance(operands[-1], (int, float)) else 1.0
                    elif op == "TL" and operands:
                        leading = float(operands[-1])
                    elif op in ("Td", "TD") and len(operands) >= 2:
                        tx, ty = float(operands[-2]), float(operands[-1])
                        if op == "TD":
                            leading = -ty
                        tlm = [tlm[0], tlm[1], tlm[2], tlm[3], tlm[4] + tx * tlm[0] + ty * tlm[2],
                               tlm[5] + tx * tlm[1] + ty * tlm[3]]
                        tm = list(tlm)
                        moved()
                    elif op == "Tm" and len(operands) >= 6:
                        tm = [float(x) for x in operands[-6:]]
                        tlm = list(tm)
                        moved()
                    elif op in ("T*", "'", '"'):
                        tlm = [tlm[0], tlm[1], tlm[2], tlm[3], tlm[4] - leading * tlm[2],
                               tlm[5] - leading * tlm[3]]
                        tm = list(tlm)
                        if leading == 0:
                            self.force_nl = True
                        if op != "T*" and operands and isinstance(operands[-1], bytes):
                            show(operands[-1])
                    elif op == "Tj" and operands and isinstance(operands[-1], bytes):
                        show(operands[-1])
                    elif op == "TJ" and operands and isinstance(operands[-1], list):
                        for it in operands[-1]:
                            if isinstance(it, bytes):
                                show(it)
                            elif isinstance(it, (int, float)):
                                fs = abs(size * (tm[0] or tm[3])) or 10
                                tm[4] -= it / 1000.0 * fs
                                if it < -250 and self.cur and not self.cur[-1].endswith(" "):
                                    self.cur.append(" ")
                        self.last_x_end = tm[4]
                    elif op == "q":
                        stack.append((font, size))
                    elif op == "Q" and stack:
                        font, size = stack.pop()
                    elif op == "Do" and operands and depth < 4:
                        xo = r(xobjs.get(str(operands[-1])))
                        if isinstance(xo, PdfStream) and xo.get("Subtype") == "Form":
                            sub_res = r(xo.get("Resources")) or res
                            try:
                                self.newline()
                                self.last_y = None
                                self.run(self.pdf.decode(xo)[0], sub_res if isinstance(sub_res, dict) else res,
                                         depth + 1)
                                self.newline()
                                self.last_y = None
                            except Exception:
                                pass
                    elif op == "BI":
                        m = re.compile(rb"\sID\s").search(data, lx.pos)
                        if m:
                            e = re.compile(rb"\sEI(?=[\s]|$)").search(data, m.end())
                            lx.pos = e.end() if e else len(data)
                except (TypeError, ValueError, IndexError):
                    pass
                operands = []
            else:
                operands.append(lx.value(tok))


def extract_pdf(path, res: ExtractResult, media: MediaSaver, cfg: Config) -> str:
    data = open(path, "rb").read()
    pdf = PDFDoc(data)
    if pdf.encrypted and pdf.crypt is None:
        res.summary = f"PDF 已加密，无法解析（{pdf.crypt_error or '未知加密方式'}）"
        return (f"# {os.path.basename(path)}\n\n{res.summary}。\n"
                "建议：请文档提供方给出未加密版本，或在浏览器中打开该 PDF 截图后让模型识图。")
    pages = pdf.pages()
    maxp = cfg.int("pdf_max_pages") or 500
    ext = PdfTextExtractor(pdf)
    out = [f"# {os.path.basename(path)}（共 {len(pages)} 页）"]
    empty_pages = []
    total = 0
    img_seen = set()
    min_px = cfg.int("min_image_px") or 48
    for i, (page, resrc) in enumerate(pages[:maxp], 1):
        try:
            txt = ext.page_text(page, resrc)
        except Exception as e:
            txt = f"（本页解析失败：{e}）"
        total += len(txt.strip())
        if len(txt.strip()) < 3:
            empty_pages.append(i)
        out.append(f"## 第 {i} 页\n\n{txt}")
        # 页面中的图片
        try:
            xobjs = pdf.resolve((resrc or {}).get("XObject")) if isinstance(resrc, dict) else None
            if isinstance(xobjs, dict):
                for nm, ref in xobjs.items():
                    key = ref.num if isinstance(ref, PdfRef) else id(ref)
                    if key in img_seen:
                        continue
                    img_seen.add(key)
                    p = _pdf_image(pdf, pdf.resolve(ref), media, f"p{i}", min_px)
                    if p:
                        out.append(media.note(p))
        except Exception:
            pass
    if len(pages) > maxp:
        out.append(f"…… 超过页数上限 {maxp}，其余页未解析")
    if pages and len(empty_pages) >= max(1, len(pages[:maxp]) * 0.6):
        out.insert(1, f"> **注意：大部分页面没有文字层，疑似扫描件。** 请查看下方导出的页面图片进行识图（共 {len(media.images)} 张）。")
    res.summary = f"PDF，{len(pages)} 页，{total} 字符" + (f"，{len(empty_pages)} 页无文字（可能是扫描页）" if empty_pages else "")
    return "\n\n".join(out)


def _pdf_image(pdf: PDFDoc, xo, media: MediaSaver, hint: str, min_px: int):
    if not isinstance(xo, PdfStream) or xo.get("Subtype") != "Image":
        return None
    r = pdf.resolve
    w, h = r(xo.get("Width")), r(xo.get("Height"))
    if not isinstance(w, int) or not isinstance(h, int) or max(w, h) < min_px:
        return None
    if r(xo.get("ImageMask")):
        return None
    filters = r(xo.get("Filter"))
    flist = [str(r(f)) for f in (filters if isinstance(filters, list) else [filters] if filters else [])]
    key = ("pdfimg", id(xo))
    if flist and flist[-1] in ("DCTDecode", "DCT"):
        data, _ = pdf.decode(xo)
        return media.save(key, data, ".jpg", hint)
    if flist and flist[-1] in ("JPXDecode",):
        data, _ = pdf.decode(xo)
        return media.save(key, data, ".jp2", hint)
    if flist and flist[-1] in ("CCITTFaxDecode", "CCF", "JBIG2Decode"):
        return None
    bpc = r(xo.get("BitsPerComponent")) or 8
    cs = r(xo.get("ColorSpace"))
    palette = None
    if isinstance(cs, list) and cs and str(r(cs[0])) == "ICCBased":
        icc = r(cs[1])
        n = r(icc.get("N")) if isinstance(icc, PdfStream) else 3
        cs = PdfName({1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(n, "DeviceRGB"))
    if isinstance(cs, list) and cs and str(r(cs[0])) in ("Indexed", "I"):
        base = r(cs[1])
        if isinstance(base, list) and base and str(r(base[0])) == "ICCBased":
            icc = r(base[1])
            base = PdfName("DeviceRGB" if (r(icc.get("N")) if isinstance(icc, PdfStream) else 3) == 3 else "DeviceGray")
        lookup = r(cs[3])
        if isinstance(lookup, PdfStream):
            lookup = pdf.decode(lookup)[0]
        if not isinstance(lookup, bytes):
            return None
        if str(base) in ("DeviceGray", "G", "CalGray"):
            palette = b"".join(bytes([x, x, x]) for x in lookup)
        elif str(base) in ("DeviceRGB", "RGB", "CalRGB"):
            palette = lookup
        else:
            return None
        palette = palette[:3 * 256]
        if len(palette) % 3:
            palette = palette[:len(palette) - len(palette) % 3]
        color_type, channels = 3, 1
    elif str(cs) in ("DeviceGray", "G", "CalGray"):
        color_type, channels = 0, 1
    elif str(cs) in ("DeviceRGB", "RGB", "CalRGB"):
        color_type, channels = 2, 3
    else:
        return None
    if bpc not in (1, 2, 4, 8) or (color_type == 2 and bpc != 8):
        return None
    parms = r(xo.get("DecodeParms"))
    if isinstance(parms, list):
        parms = r(parms[-1]) if parms else None
    pred = r(parms.get("Predictor", 1)) if isinstance(parms, dict) else 1
    os.makedirs(media.dir, exist_ok=True)
    if media.count >= media.max:
        media.skipped += 1
        return None
    tmp = os.path.join(media.dir, f"_tmp_{id(xo)}.png")
    try:
        if flist == ["FlateDecode"] and pred and pred >= 10:
            ok = write_png(tmp, w, h, color_type, bpc, palette=palette, filtered_zlib=xo.raw)
        else:
            raw, _ = pdf.decode(xo, for_image=True)
            if pred and pred >= 10:  # 数据已是 PNG 行过滤格式，直接压缩即可
                ok = write_png(tmp, w, h, color_type, bpc, palette=palette, filtered_zlib=zlib.compress(raw, 6))
            else:
                ok = write_png(tmp, w, h, color_type, bpc, raw=raw, palette=palette)
        if not ok:
            return None
        data = open(tmp, "rb").read()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return media.save(key, data, ".png", hint)


# =============================================================================
# 7. 附件内容提取总入口
# =============================================================================

def extract_file(path: str, out_dir: str, cfg: Config, label: str = None) -> ExtractResult:
    """提取本地文件内容为 Markdown，写到 out_dir/<名>.md，内嵌图片写到 out_dir/<名>_media/。"""
    res = ExtractResult()
    path = os.path.abspath(path)
    out_dir = os.path.abspath(out_dir)
    name = label or os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    # 同名不同格式（如 报表.xls 与 报表.xlsx）不能互相覆盖，所以 stem 带上扩展名
    stem = safe_name(os.path.splitext(name)[0], 55) + (("_" + ext[1:]) if ext else "")
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, safe_name(name, 70) + ".md")
    media = MediaSaver(os.path.join(out_dir, stem + "_media"), cfg)
    text = None
    try:
        sig = open(path, "rb").read(8)
        if ext in IMG_EXT:
            wh = image_size(path)
            res.images.append((path, *(wh or (0, 0))))
            res.summary = f"图片 {wh[0]}x{wh[1]}" if wh else "图片"
            if ext not in VIEWABLE_IMG:
                res.summary += f"（{ext} 格式，模型可能无法直接查看）"
            return res
        if ext in (".docx", ".docm", ".dotx", ".dotm") or (sig[:2] == b"PK" and ext in (".wps",)):
            text = extract_docx(path, res, media)
        elif ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
            text = extract_xlsx(path, res, media, cfg, out_dir, stem)
        elif ext in (".pptx", ".pptm", ".ppsx", ".potx"):
            text = extract_pptx(path, res, media)
        elif ext in (".vsdx", ".vsdm"):
            text = extract_vsdx(path, res)
        elif ext == ".pdf" or sig[:4] == b"%PDF":
            text = extract_pdf(path, res, media, cfg)
        elif ext in (".doc", ".dot", ".wps", ".rtf", ".xls", ".xlt", ".et", ".ppt", ".pps", ".pot", ".dps"):
            text = _extract_legacy(path, ext, res, media, cfg, out_dir, stem)
        elif ext in (".csv", ".tsv"):
            raw = read_text_file(path)
            rows = list(csv.reader(io.StringIO(raw), delimiter="\t" if ext == ".tsv" else ","))
            pv = cfg.int("excel_preview_rows") or 100
            text = f"# {name}\n\n共 {len(rows)} 行\n\n" + md_table(rows[:pv]) + (
                f"\n\n…… 仅预览前 {pv} 行，完整内容见原文件 {path}" if len(rows) > pv else "")
            res.summary = f"CSV，{len(rows)} 行"
        elif ext in (".html", ".htm", ".mhtml"):
            text = HtmlToMarkdown().convert(read_text_file(path))
            res.summary = "HTML"
        elif ext == ".eml":
            text = extract_eml(path)
            res.summary = "邮件"
        elif ext in (".zip", ".jar", ".war") or (sig[:2] == b"PK" and ext not in TEXT_EXT):
            text = extract_zip_listing(path)
            res.summary = "压缩包（仅列目录）"
        elif ext in TEXT_EXT or _looks_text(path):
            text = read_text_file(path)
            if ext in (".json",):
                try:
                    text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
                except Exception:
                    pass
            res.summary = f"文本，{len(text)} 字符"
        else:
            res.summary = f"不支持提取的格式（{ext or '无扩展名'}），已下载原文件"
            return res
    except zipfile.BadZipFile:
        res.error = "文件损坏或不是有效的 Office 文件"
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
    if res.error and text is None:
        res.summary = "提取失败：" + res.error
        return res
    text = text or ""
    if media.images:
        text += "\n\n## 文档内图片（可用识图能力查看）\n\n" + "\n".join(
            f"- {p}（{w}x{h}）" for p, w, h in media.images)
    if media.skipped:
        text += f"\n\n（另有 {media.skipped} 张图片超出上限 max_doc_images 未导出）"
    write_text(md_path, text)
    res.md_path = md_path
    res.images.extend(media.images)
    res.chars = len(text)
    return res


def _looks_text(path) -> bool:
    try:
        b = open(path, "rb").read(4096)
    except Exception:
        return False
    if not b or b"\x00" in b:
        return False
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            b.decode("gb18030")
            return True
        except UnicodeDecodeError:
            return False


def _extract_legacy(path, ext, res: ExtractResult, media: MediaSaver, cfg: Config, out_dir, stem):
    mode = (cfg.legacy_office or "auto").lower()
    kind = "word" if ext in (".doc", ".dot", ".wps", ".rtf") else ("excel" if ext in (".xls", ".xlt", ".et") else "ppt")
    py_err = ""

    def via_office():
        conv = convert_with_office(path, kind)
        try:
            if kind == "word":
                return extract_docx(conv, res, media)
            if kind == "excel":
                return extract_xlsx(conv, res, media, cfg, out_dir, stem)
            return extract_pptx(conv, res, media)
        finally:
            shutil.rmtree(os.path.dirname(conv), ignore_errors=True)

    if mode == "com":
        t = via_office()
        res.summary += "（经本机 Office/WPS 转换）"
        return t
    text = None
    if mode in ("auto", "python") and ext != ".rtf":
        try:
            if kind == "word":
                text = extract_doc_python(path)
                res.summary = f"Word 97-2003 文档，{len(text)} 字符（纯文本，表格以 | 分隔，图片未导出）"
            elif kind == "excel":
                sheets = extract_xls_python(path, cfg)
                text = write_sheets(sheets, os.path.basename(path), res, out_dir, stem, cfg)
            else:
                text = extract_ppt_python(path)
                res.summary = "PPT 97-2003（纯文本）"
        except Exception as e:
            py_err = str(e)
            text = None
    if (not text or not text.strip()) and mode == "auto" and os.name == "nt":
        try:
            t = via_office()
            res.summary += "（经本机 Office/WPS 转换）"
            return t
        except Exception as e:
            py_err = (py_err + "；" if py_err else "") + str(e)
    if text is None:
        if ext == ".rtf" and not py_err:
            py_err = "RTF 需要本机 Office/WPS 转换（仅 Windows）"
        raise RuntimeError(py_err or "无法解析")
    return text


# =============================================================================
# 8. 页面导出（正文 + 图片 + 附件 + 评论）
# =============================================================================

def output_root(cfg: Config, override: str = None) -> str:
    cands = [override, cfg.output_dir, os.path.join(os.getcwd(), "confluence_out"),
             os.path.join(os.path.expanduser("~"), "confluence_out")]
    for c in cands:
        if not c:
            continue
        try:
            os.makedirs(c, exist_ok=True)
            test = os.path.join(c, ".write_test")
            open(test, "w").close()
            os.remove(test)
            return os.path.abspath(c)
        except OSError:
            continue
    return tempfile.mkdtemp(prefix="confluence_out_")


def _fmt_time(s: str) -> str:
    if not s:
        return ""
    return s.replace("T", " ")[:16]


class PageExporter:
    def __init__(self, client: Client, cfg: Config, args):
        self.c = client
        self.cfg = cfg
        self.args = args
        self.deny = DenyFilter(cfg, getattr(args, "deny_file", None))
        self.root = output_root(cfg, getattr(args, "out", None))
        self.pool = futures.ThreadPoolExecutor(max_workers=max(1, min(cfg.int("workers") or 4, 8)))

    def fetch(self, pid: str, version=None):
        expand = "body.storage,body.view,version,space,ancestors,metadata.labels,history"
        params = {"expand": expand}
        if version:
            params.update(status="historical", version=str(version))
        try:
            return self.c.get_json(f"/rest/api/content/{pid}", params)
        except HttpError as e:
            if e.code in (500, 502, 503, 504, 400):
                log("渲染视图失败，改用存储格式…")
                params["expand"] = expand.replace("body.view,", "")
                return self.c.get_json(f"/rest/api/content/{pid}", params)
            if e.code == 404:
                raise HttpError(404, e.url, f"页面 {pid} 不存在，或当前账号没有查看权限")
            raise

    def export(self, ref: str):
        pid = resolve_page_id(self.c, ref)
        if self.deny.active:
            # 先用轻量元数据查排除清单，命中就拒绝：不取正文、不下附件
            _, hit = self.deny.check_page(self.c, pid)
            if hit:
                raise DeniedError(f"页面 {pid} 被排除清单拒绝读取：{hit[1]}")
        args = self.args
        d = self.fetch(pid, getattr(args, "version", None))
        title = d.get("title", "")
        space = d.get("space") or {}
        ver = d.get("version") or {}
        hist = d.get("history") or {}
        page_dir = os.path.join(self.root, pid + (f"_v{args.version}" if getattr(args, "version", None) else ""))
        att_dir = os.path.join(page_dir, "att")
        txt_dir = os.path.join(page_dir, "att_text")
        os.makedirs(page_dir, exist_ok=True)
        meta_path = os.path.join(page_dir, "meta.json")
        prev = {}
        if os.path.exists(meta_path) and not getattr(args, "force", False):
            try:
                old_meta = json.load(open(meta_path, encoding="utf-8"))
                prev = {a["id"]: a for a in old_meta.get("attachments", []) + old_meta.get("ref_images", [])}
            except Exception:
                prev = {}
        body = d.get("body") or {}
        view_html = (body.get("view") or {}).get("value") or ""
        storage = (body.get("storage") or {}).get("value") or ""
        write_text(os.path.join(page_dir, "page.storage.xml"), storage)

        # ---------- 附件列表 ----------
        atts = []
        try:
            for a in self.c.paged(f"/rest/api/content/{pid}/child/attachment", {"expand": "version"}, limit=100):
                atts.append(a)
        except HttpError as e:
            log(f"获取附件列表失败：{e}")
        mode = getattr(args, "attachments", "all")
        max_bytes = (self.cfg.int("max_attachment_mb") or 50) * 1024 * 1024
        skip_ext = set(self.cfg.list("skip_extensions"))
        plan = []  # (key, url, dest, meta_dict)
        att_meta = []
        used_names = set()
        for a in atts:
            fn = a.get("title", "file")
            ext = os.path.splitext(fn)[1].lower()
            size = (a.get("extensions") or {}).get("fileSize", 0) or 0
            mtype = (a.get("extensions") or {}).get("mediaType", "")
            link = (a.get("_links") or {}).get("download", "")
            local_name = safe_name(fn)
            if local_name.lower() in used_names:
                local_name = safe_name(f"{a.get('id')}_{fn}")
            used_names.add(local_name.lower())
            m = {"id": a.get("id"), "title": fn, "size": size, "mediaType": mtype,
                 "version": (a.get("version") or {}).get("number"), "download": self.c.url(link) if link else "",
                 "local": "", "status": ""}
            is_img = ext in IMG_EXT or mtype.startswith("image/")
            m["is_image"] = is_img
            att_meta.append(m)
            if not link:
                m["status"] = "无下载地址"
                continue
            if mode == "none":
                m["status"] = "未下载（--attachments none）"
                continue
            if mode == "images" and not is_img:
                m["status"] = "未下载（仅下载图片模式）"
                continue
            if ext in skip_ext:
                m["status"] = f"跳过（{ext} 在 skip_extensions 中）"
                continue
            if size and size > max_bytes:
                m["status"] = f"跳过（{human_size(size)} 超过上限 {self.cfg.int('max_attachment_mb')}MB）"
                continue
            dest = os.path.join(att_dir, local_name)
            plan.append((self.c.norm_key(link), self.c.url(link), dest, m))

        # ---------- 正文中引用的图片（含其它页面的附件图片）----------
        local_map = {}
        att_keys = {p[0]: p for p in plan}
        for k, (_, _, dest, m) in att_keys.items():
            local_map[k] = dest
        extra = []
        if view_html and mode != "none":
            for n in parse_html(view_html).find_all("img"):
                src = n.attrs.get("data-image-src") or n.attrs.get("src") or ""
                if not src or src.startswith("data:") or "emoticon" in n.cls():
                    continue
                if not self.c.is_local(src):
                    continue
                key = self.c.norm_key(src)
                if any(x in key for x in ICON_PATHS) or not key.startswith("/download/"):
                    continue
                if key in local_map:
                    continue
                fn = posixpath.basename(key)
                mm = re.search(r"/download/attachments/(\d+)/", key)
                dest = os.path.join(att_dir, safe_name(f"ref{mm.group(1) if mm else ''}_{fn}"))
                local_map[key] = dest
                extra.append((key, self.c.html_url(src), dest, {"title": fn, "id": "ref:" + key, "status": "",
                                                                 "is_image": True, "version": None}))

        # ---------- 并发下载（按版本缓存）----------
        def do_dl(item):
            key, url, dest, m = item
            old = prev.get(m.get("id"))
            if (old and old.get("version") == m.get("version") and old.get("local") == dest
                    and os.path.exists(dest) and (not m.get("size") or os.path.getsize(dest) == m["size"])):
                m["local"] = dest
                m["status"] = "已缓存"
                m["_cached"] = True
                return
            try:
                self.c.download(url, dest, max_bytes)
                m["local"] = dest
                m["status"] = "已下载"
            except AuthError:
                raise
            except Exception as e:
                m["status"] = f"下载失败：{e}"
                local_map.pop(key, None)

        jobs = list(plan) + extra
        if jobs:
            log(f"[{pid}] 下载 {len(jobs)} 个文件（{self.pool._max_workers} 并发）…")
            for f in futures.as_completed([self.pool.submit(do_dl, j) for j in jobs]):
                f.result()

        # ---------- 附件内容提取 ----------
        if not getattr(args, "no_extract", False):
            for m in att_meta:
                if not m.get("local") or m.get("is_image"):
                    if m.get("local") and m.get("is_image"):
                        wh = image_size(m["local"])
                        m["wh"] = list(wh) if wh else None
                    continue
                old = prev.get(m["id"]) or {}
                if m.get("_cached") and old.get("extract_md") and os.path.exists(old["extract_md"]):
                    m["extract_md"] = old["extract_md"]
                    m["extract_summary"] = old.get("extract_summary", "")
                    m["extract_images"] = old.get("extract_images", [])
                    continue
                log(f"[{pid}] 提取 {m['title']} …")
                r = extract_file(m["local"], txt_dir, self.cfg, label=os.path.basename(m["local"]))
                m["extract_md"] = r.md_path
                m["extract_summary"] = r.summary
                m["extract_images"] = [p for p, _, _ in r.images if p != m["local"]]
        for m in att_meta:
            m.pop("_cached", None)

        # ---------- 正文 → Markdown ----------
        def img_cb(attrs):
            cls = attrs.get("class", "")
            alt = attrs.get("alt", "") or attrs.get("data-linked-resource-default-alias", "")
            if "emoticon" in cls:
                return ""
            src = attrs.get("data-image-src") or attrs.get("src") or ""
            if not src or src.startswith("data:"):
                return ""
            key = self.c.norm_key(src) if self.c.is_local(src) else None
            if key and key in local_map and os.path.exists(local_map[key]):
                return f"![{alt or posixpath.basename(key)}]({local_map[key]})"
            if key and any(x in key for x in ICON_PATHS):
                return ""
            return f"![{alt}]({self.c.html_url(src)})"

        def link_cb(href):
            return self.c.html_url(href) if (href.startswith("/") and not href.startswith("//")) else href

        def storage_img_cb(fn, page_title):
            key = f"/download/attachments/{pid}/{fn}"
            if not page_title and key in local_map and os.path.exists(local_map[key]):
                return f"![{fn}]({local_map[key]})"
            return f"![{fn}]（附件 {fn}{('，位于页面 ' + page_title) if page_title else ''}）"

        conv = HtmlToMarkdown(img_cb, link_cb, storage_img_cb)
        body_md = conv.convert(view_html) if view_html else conv.convert(storage)

        # draw.io / Gliffy 图（view 中常常只是脚本占位）
        diag = []
        for m_ in re.finditer(r'<ac:structured-macro[^>]*ac:name="(drawio|drawio-sketch|inc-drawio|gliffy)"(.*?)</ac:structured-macro>',
                              storage, re.S):
            nm = re.search(r'<ac:parameter ac:name="(?:diagramName|name)">([^<]+)</ac:parameter>', m_.group(2))
            if nm:
                dn = htmlmod.unescape(nm.group(1))
                png = next((x for x in att_meta if x["title"] in (dn + ".png", dn) and x.get("local")
                            and x["title"].lower().endswith(".png")), None)
                diag.append(f"- {m_.group(1)}「{dn}」" + (f"：![{dn}]({png['local']})" if png else "（未找到渲染图附件）"))

        # ---------- 评论 ----------
        comments_md = ""
        if not getattr(args, "no_comments", False):
            try:
                try:
                    cms = list(self.c.paged(f"/rest/api/content/{pid}/child/comment",
                                            {"expand": "body.view,version,history,ancestors,extensions.inlineProperties",
                                             "depth": "all"}, limit=100, max_items=500))
                except HttpError:
                    cms = list(self.c.paged(f"/rest/api/content/{pid}/child/comment",
                                            {"expand": "body.view,version,history,ancestors", "depth": "all"},
                                            limit=100, max_items=500))
                def cm_account(cm):
                    # 评论作者账号标识（优先 username，其次 userKey、displayName），用于判断是否同一账号
                    for src in (((cm.get("history") or {}).get("createdBy") or {}),
                                ((cm.get("version") or {}).get("by") or {})):
                        for k in ("username", "userKey", "displayName"):
                            if src.get(k):
                                return src[k]
                    return ""

                recs = []
                for cm in cms:
                    who = ((cm.get("history") or {}).get("createdBy") or {}).get("displayName") or \
                          ((cm.get("version") or {}).get("by") or {}).get("displayName", "")
                    when = _fmt_time(((cm.get("history") or {}).get("createdDate")) or
                                     (cm.get("version") or {}).get("when", ""))
                    txt = HtmlToMarkdown(img_cb, link_cb).convert(
                        ((cm.get("body") or {}).get("view") or {}).get("value", "")).strip()
                    recs.append({"cm": cm, "who": who, "when": when, "txt": txt})
                ai_idx = set()
                if not getattr(args, "include_ai_comments", False):
                    # 「【AI 核对】」开头的评论是 AI 自己写的，其回复链里同一账号的评论也归入 AI 组，
                    # 单独成节，永远不当证据；--include-ai-comments 时才按普通评论混排输出
                    ai_acct = {}
                    for i, r in enumerate(recs):
                        if r["txt"].lstrip().startswith("【AI 核对】"):
                            ai_idx.add(i)
                            ai_acct[str(r["cm"].get("id"))] = cm_account(r["cm"])
                    changed = True
                    while changed:
                        changed = False
                        for i, r in enumerate(recs):
                            if i in ai_idx:
                                continue
                            anc = [str(a.get("id")) for a in (r["cm"].get("ancestors") or [])
                                   if a.get("type") == "comment"]
                            if any(a in ai_acct and cm_account(r["cm"]) == ai_acct[a] for a in anc):
                                ai_idx.add(i)
                                ai_acct[str(r["cm"].get("id"))] = cm_account(r["cm"])
                                changed = True

                def cm_line(r):
                    ext_ = r["cm"].get("extensions") or {}
                    sel = ((ext_.get("inlineProperties") or {}).get("originalSelection") or "")
                    loc = "（行内评论，针对「" + sel[:60] + "」）" if sel else ""
                    txt = r["txt"].replace("\n", "\n  ")
                    return f"- **{r['who']}** {r['when']}{loc}：{txt}"

                human = [cm_line(r) for i, r in enumerate(recs) if i not in ai_idx]
                ai = [cm_line(r) for i, r in enumerate(recs) if i in ai_idx]
                secs = []
                if human:
                    secs.append(f"## 评论（{len(human)}）\n\n" + "\n".join(human))
                if ai:
                    secs.append("## AI 自动核对评论（不得作为证据）\n\n" + "\n".join(ai))
                if secs:
                    comments_md = "\n\n".join(secs)
            except HttpError as e:
                vlog(f"获取评论失败：{e}")

        # ---------- 组装 page.md ----------
        crumbs = " / ".join(a.get("title", "") for a in (d.get("ancestors") or []))
        labels = ", ".join(l.get("name", "") for l in (((d.get("metadata") or {}).get("labels") or {})
                                                        .get("results") or []))
        webui = (d.get("_links") or {}).get("webui", "")
        page_url = self.c.url(webui) if webui else ""
        # 引用标识：供知识库记录来源，格式固定（页面 ID / 版本 / 最后修改时间 / URL）
        citation = (f"引用：Confluence 页面 {pid} 版本 {ver.get('number', '')}"
                    f"（最后修改 {_fmt_time(ver.get('when', ''))}）{page_url}")
        head = [f"# {title}", citation, ""]
        head.append(f"> 页面ID: {pid} | 空间: {space.get('name', '')} ({space.get('key', '')}) | 版本: v{ver.get('number', '')}"
                    f" | 最后修改: {(ver.get('by') or {}).get('displayName', '')} {_fmt_time(ver.get('when', ''))}")
        head.append(f"> 创建: {((hist.get('createdBy') or {}).get('displayName', ''))} {_fmt_time(hist.get('createdDate', ''))}"
                    + (f" | 标签: {labels}" if labels else ""))
        if crumbs:
            head.append(f"> 路径: {crumbs} / {title}")
        head.append(f"> 链接: {self.c.url(webui) if webui else ''}")
        head.append(f"> 本地目录: {page_dir}")
        parts = ["\n".join(head), "---", body_md.strip() or "（正文为空）"]
        if diag:
            parts.append("## 页面中的流程图/示意图\n\n" + "\n".join(diag))
        if att_meta:
            lines = [f"## 附件（{len(att_meta)}）", "", "| # | 文件名 | 大小 | 状态 | 内容提取 |", "|---|---|---|---|---|"]
            for i, m in enumerate(att_meta, 1):
                ex = ""
                if m.get("extract_md"):
                    ex = f"{m.get('extract_summary', '')} → {m['extract_md']}"
                elif m.get("extract_summary"):
                    ex = m["extract_summary"]
                elif m.get("is_image") and m.get("local"):
                    ex = "图片" + (f" {m['wh'][0]}x{m['wh'][1]}" if m.get("wh") else "")
                lines.append(f"| {i} | {md_cell(m['title'])} | {human_size(m['size'])} | {md_cell(m['status'])} | {md_cell(ex)} |")
            parts.append("\n".join(lines))
            imgs = [m for m in att_meta if m.get("is_image") and m.get("local")]
            doc_imgs = [(m["title"], p) for m in att_meta for p in (m.get("extract_images") or [])]
            ref_imgs = [m for _, _, _, m in extra if m.get("local")]
            if imgs or doc_imgs or ref_imgs:
                il = ["### 可查看的图片文件（多模态模型可直接识图）", ""]
                for m in imgs:
                    il.append(f"- {m['title']}: {m['local']}")
                for m in ref_imgs:
                    il.append(f"- (正文引用) {m['title']}: {m['local']}")
                for t, p in doc_imgs[:200]:
                    il.append(f"- (来自 {t}) {p}")
                parts.append("\n".join(il))
        if comments_md:
            parts.append(comments_md)
        page_md = "\n\n".join(parts) + "\n"
        md_path = os.path.join(page_dir, "page.md")
        write_text(md_path, page_md)
        meta = {"id": pid, "title": title, "space": space.get("key"), "version": ver.get("number"),
                "last_modified": _fmt_time(ver.get("when", "")), "citation": citation,
                "url": page_url, "fetched": dt.datetime.now().isoformat(timespec="seconds"),
                "page_md": md_path, "attachments": att_meta,
                "ref_images": [m for _, _, _, m in extra if m.get("local")]}
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        return {"id": pid, "title": title, "dir": page_dir, "md": md_path, "md_text": page_md, "atts": att_meta}


def list_children(client: Client, pid: str, limit=500):
    return list(client.paged(f"/rest/api/content/{pid}/child/page", {"expand": "version"}, limit=100,
                             max_items=limit))


# =============================================================================
# 9. 命令行
# =============================================================================

def emit(text: str, limit: int):
    if limit and len(text) > limit:
        cut = text[:limit]
        print(cut)
        print(f"\n…… 输出已截断（共 {len(text)} 字符，显示前 {limit}）。完整内容请直接读取上面列出的 .md 文件。")
    else:
        print(text)


def cmd_check(cfg: Config, args):
    print(f"配置文件: {cfg.path or '（未找到 config.ini，仅使用环境变量）'}")
    print(f"base_url: {cfg.base_url or '（未填写）'}")
    c = Client(cfg)
    print(f"认证方式: {c.effective_mode}  用户: {cfg.username or '-'}  "
          f"密码: {'已填写' if cfg.password else '未填写'}  令牌: {'已填写' if cfg.token else '未填写'}")
    t0 = time.time()
    try:
        with c.open(c.url("/rest/applinks/1.0/manifest"), headers={"Accept": "application/json"}, retries=1) as r:
            mf = json.loads(r.read().decode("utf-8", "replace"))
        print(f"服务器: {mf.get('name', '')}  类型: {mf.get('typeId', '')}  版本: {mf.get('version', '')}")
    except Exception as e:
        vlog(f"manifest 获取失败: {e}")
    me = c.get_json("/rest/api/user/current")
    if me.get("type") == "anonymous" or not me.get("username") and not me.get("displayName"):
        print("⚠ 当前是匿名身份：认证没有生效，只能看到匿名可见的内容。请检查 config.ini。")
    else:
        print(f"✔ 登录成功：{me.get('displayName', '')} ({me.get('username', '')})，耗时 {time.time() - t0:.1f}s")
    try:
        sp = c.get_json("/rest/api/space", {"limit": 5})
        print(f"可访问空间示例: {', '.join(s.get('key', '') + ' ' + s.get('name', '') for s in sp.get('results', []))}")
    except Exception as e:
        print(f"空间列表获取失败: {e}")
    print(f"输出目录: {output_root(cfg, getattr(args, 'out', None))}")


def cmd_page(cfg: Config, args):
    c = Client(cfg)
    ex = PageExporter(c, cfg, args)
    limit = args.max_chars if args.max_chars is not None else cfg.int("max_print_chars")
    results = []
    for ref in args.refs:
        if args.recursive:
            results.extend(export_tree(ex, c, cfg, ref, args))
        else:
            results.append(ex.export(ref))
    ex.pool.shutdown()
    if len(results) == 1 and not args.recursive:
        r = results[0]
        out = r["md_text"]
        if args.print == "all":
            for m in r["atts"]:
                if m.get("extract_md") and os.path.exists(m["extract_md"]):
                    out += f"\n\n========== 附件：{m['title']} ==========\n\n" + read_text_file(m["extract_md"])
        if args.print != "none":
            emit(out, limit)
        print(f"\n[完成] 页面已保存：{r['md']}")
        return
    print(f"共导出 {len(results)} 个页面：\n")
    for r in results:
        na = sum(1 for m in r["atts"] if m.get("local"))
        ind = r.get("indent", "")
        print(f"{ind}- {r['title']}  (ID {r['id']}, 附件 {na})\n{ind}  {r['md']}")
    if args.print != "none" and results:
        budget = limit
        print("\n" + "=" * 60)
        for r in results:
            if budget <= 0:
                print("\n…… 其余页面内容请直接读取上面列出的 page.md 文件。")
                break
            txt = r["md_text"]
            emit(txt, budget)
            budget -= len(txt)
            print("=" * 60)


def export_tree(ex: PageExporter, c: Client, cfg: Config, ref: str, args):
    maxp = args.max_pages or cfg.int("max_recursive_pages") or 50
    out = []
    root_id = resolve_page_id(c, ref)
    queue = [(root_id, 0)]
    seen = set()
    while queue and len(out) < maxp:
        pid, dep = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            r = ex.export(pid)
        except DeniedError as e:
            if pid == root_id:
                raise  # 递归的根页面本身被排除：按直接读取处理，拒绝并退出码 5
            log(f"页面 {pid} 已跳过：{e}")
            continue
        except (HttpError, ValueError) as e:
            log(f"页面 {pid} 导出失败：{e}")
            continue
        r["indent"] = "  " * dep
        out.append(r)
        if dep < args.depth:
            try:
                kids = list_children(c, pid)
                queue[0:0] = [(k["id"], dep + 1) for k in kids]
            except HttpError as e:
                log(f"获取 {pid} 子页面失败：{e}")
    if queue:
        log(f"已达页面数上限 {maxp}，剩余 {len(queue)} 个未导出（可用 --max-pages 调整）")
    return out


def cmd_children(cfg: Config, args):
    c = Client(cfg)
    pid = resolve_page_id(c, args.ref)
    deny = DenyFilter(cfg, getattr(args, "deny_file", None))
    space_key = ""
    root_ancestors = []
    if deny.active:
        # 根页面命中排除清单时按直接读取处理：拒绝（退出码 5）；子页面逐个过滤
        meta, hit = deny.check_page(c, pid)
        if hit:
            raise DeniedError(f"页面 {pid} 被排除清单拒绝读取：{hit[1]}")
        space_key = (meta.get("space") or {}).get("key", "")
        root_ancestors = [str(a.get("id")) for a in (meta.get("ancestors") or [])]
    skipped = [0]

    def walk(p, dep, anc):
        # anc 是 p 的祖先 ID 链（树根在前）：沿树传递，任一节点命中排除就整棵子树不列、只计数
        for k in list_children(c, p):
            if deny.active and deny.check(k.get("id"), k.get("title", ""), space_key, anc + [str(p)]):
                skipped[0] += 1
                continue
            ver = k.get("version") or {}
            print(f"{'  ' * dep}- {k.get('title')}  (ID {k.get('id')}, v{ver.get('number', '')}, "
                  f"{_fmt_time(ver.get('when', ''))})")
            if dep + 1 < args.depth:
                walk(k["id"], dep + 1, anc + [str(p)])

    d = c.get_json(f"/rest/api/content/{pid}", {"expand": "version"})
    print(f"{d.get('title')}  (ID {pid})")
    walk(pid, 0, root_ancestors)
    if skipped[0]:
        print(f"（已按排除清单过滤 {skipped[0]} 个子页面）")


def cmd_search(cfg: Config, args):
    c = Client(cfg)
    q = args.query.strip()
    if args.cql or re.search(r"\b(type|space|title|text|label|creator|contributor|ancestor|lastmodified|created)\s*(=|~|!=|in)\s*",
                             q, re.I):
        cql = q
    else:
        esc = q.replace("\\", "\\\\").replace('"', '\\"')
        cql = f'{"title" if args.title else "text"} ~ "{esc}"'
        types = {"page": "type = page", "blog": "type = blogpost", "attachment": "type = attachment",
                 "default": "type in (page, blogpost)", "all": ""}
        t = types.get(args.type, "")
        if t:
            cql += " and " + t
        if args.space:
            cql += f' and space = "{args.space}"'
    print(f"CQL: {cql}\n")
    items = []
    try:
        d = c.get_json("/rest/api/search", {"cql": cql, "limit": args.limit, "excerpt": "highlight"})
        for r in d.get("results", []):
            ct = r.get("content") or {}
            items.append({
                "id": ct.get("id", ""), "type": ct.get("type", ""), "title": r.get("title") or ct.get("title", ""),
                "space": (r.get("resultGlobalContainer") or {}).get("title", ""),
                "when": _fmt_time(r.get("lastModified", "")), "url": c.url(r.get("url", "")) if r.get("url") else "",
                "excerpt": r.get("excerpt", ""),
            })
        total = d.get("totalSize", len(items))
    except HttpError as e:
        if e.code not in (404, 400, 501):
            raise
        d = c.get_json("/rest/api/content/search", {"cql": cql, "limit": args.limit, "expand": "space,version"})
        for r in d.get("results", []):
            items.append({"id": r.get("id"), "type": r.get("type"), "title": r.get("title"),
                          "space": (r.get("space") or {}).get("name", ""),
                          "when": _fmt_time((r.get("version") or {}).get("when", "")),
                          "url": c.url((r.get("_links") or {}).get("webui", "")), "excerpt": ""})
        total = d.get("totalSize", len(items))
    deny = DenyFilter(cfg, getattr(args, "deny_file", None))
    denied_n = 0
    if deny.active and items:
        # 排除清单对搜索结果同样生效：先按 ID/标题查，需要空间或祖先信息时再轻量取元数据
        need_meta = bool(deny.spaces or deny.ancestor_ids)
        kept = []
        for it in items:
            hit = deny.check(it["id"], it["title"])
            if not hit and need_meta and it["id"]:
                try:
                    _, hit = deny.check_page(c, it["id"])
                except HttpError:
                    hit = None
            if hit:
                denied_n += 1
            else:
                kept.append(it)
        items = kept
    print(f"共 {total} 条，显示 {len(items)} 条：\n")
    for i, it in enumerate(items, 1):
        ex = htmlmod.unescape(it["excerpt"] or "")
        ex = ex.replace("@@@hl@@@", "**").replace("@@@endhl@@@", "**")
        ex = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", ex)).strip()[:200]
        print(f"{i}. [{it['type']}] {it['title']}  (ID {it['id']}) — {it['space']} {it['when']}")
        if it["url"]:
            print(f"   {it['url']}")
        if ex:
            print(f"   摘要: {ex}")
    if denied_n:
        print(f"\n（已按排除清单过滤 {denied_n} 条）")


def _ts_key(s: str) -> float:
    # ISO 时间转时间戳（解析失败返回 0，排序时排在最后）
    try:
        return dt.datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def cmd_find(cfg: Config, args):
    # 多角度查找：每个词各自组 CQL 并发查询，合并去重后三层排序，输出候选表 + candidates.md/json
    terms = list(args.term or [])
    if args.terms_file:
        for line in read_text_file(args.terms_file).splitlines():
            t = line.strip()
            if t and not t.startswith("#"):
                terms.append(t)
    terms = list(dict.fromkeys(terms))  # 去重并保持顺序
    if not terms:
        raise ValueError("find 需要至少一个检索词（--term 词 或 --terms-file 文件）")
    c = Client(cfg)
    deny = DenyFilter(cfg, getattr(args, "deny_file", None))

    def build_cql(term):
        esc = term.replace("\\", "\\\\").replace('"', '\\"')
        parts = [f'text ~ "{esc}"', f"type = {args.type}"]
        spaces = [s for s in (args.space or []) if s]
        if len(spaces) == 1:
            parts.append(f'space = "{spaces[0]}"')
        elif spaces:
            parts.append("space in (" + ", ".join(f'"{s}"' for s in spaces) + ")")
        if args.since:
            parts.append(f'lastmodified >= "{args.since}"')
        return " and ".join(parts)

    pool_size = max((args.limit or 20) * 3, 50)

    def run(term):
        return term, list(c.paged("/rest/api/content/search",
                                  {"cql": build_cql(term), "expand": "space,version,ancestors"},
                                  limit=100, max_items=pool_size))

    merged = {}
    with futures.ThreadPoolExecutor(max_workers=max(1, min(cfg.int("workers") or 4, 8))) as pool:
        for term, results in pool.map(run, terms):
            for r in results:
                pid = str(r.get("id", ""))
                if not pid:
                    continue
                ent = merged.get(pid)
                if ent is None:
                    ver = r.get("version") or {}
                    space = r.get("space") or {}
                    webui = (r.get("_links") or {}).get("webui", "")
                    ent = {"id": pid, "title": r.get("title", ""), "type": r.get("type", ""),
                           "space_key": space.get("key", ""), "space_name": space.get("name", ""),
                           "version": ver.get("number"), "when": ver.get("when", ""),
                           "ancestors": [a.get("id") for a in (r.get("ancestors") or [])],
                           "crumbs": " / ".join(a.get("title", "") for a in (r.get("ancestors") or [])),
                           "url": c.url(webui) if webui else "", "matched": []}
                    merged[pid] = ent
                if term not in ent["matched"]:
                    ent["matched"].append(term)
    counts = {}
    cands = []
    for ent in merged.values():
        if deny.active:
            hit = deny.check(ent["id"], ent["title"], ent["space_key"], ent["ancestors"])
            if hit:
                counts[hit[0]] = counts.get(hit[0], 0) + 1
                continue
        ent["title_hit"] = any(t.lower() in ent["title"].lower() for t in ent["matched"])
        cands.append(ent)
    # 三层排序：命中词数多的在前 → 标题命中的在前 → 最近修改的在前
    cands.sort(key=lambda e: (-len(e["matched"]), 0 if e["title_hit"] else 1, -_ts_key(e["when"])))
    total_found = len(cands)
    if args.limit:
        cands = cands[: args.limit]
    rows = [["页面ID", "标题", "空间", "版本", "最后修改", "命中词", "面包屑", "URL"]]
    for e in cands:
        rows.append([e["id"], e["title"], f"{e['space_name']} ({e['space_key']})".strip(),
                     f"v{e['version']}" if e["version"] is not None else "",
                     _fmt_time(e["when"]), "、".join(e["matched"]), e["crumbs"] or "-", e["url"]])
    table = md_table(rows) if len(rows) > 1 else "（无结果）"
    root = output_root(cfg, getattr(args, "out", None))
    md_path = os.path.join(root, "candidates.md")
    json_path = os.path.join(root, "candidates.json")
    head = ["# 查找候选", "", f"检索词：{'、'.join(terms)}"]
    if args.space:
        head.append(f"限定空间：{'、'.join(args.space)}")
    if args.since:
        head.append(f"最后修改不早于：{args.since}")
    head.append(f"共 {total_found} 条，显示 {len(cands)} 条")
    md_text = "\n".join(head) + "\n\n" + table + "\n"
    if counts:
        md_text += "\n" + deny.summary_line(counts) + "\n"
    write_text(md_path, md_text)
    data = {"terms": terms, "type": args.type, "count": total_found, "shown": len(cands),
            "denied": {"total": sum(counts.values()), "by_reason": counts},
            "candidates": [{"id": e["id"], "title": e["title"], "type": e["type"],
                            "space_key": e["space_key"], "space_name": e["space_name"],
                            "version": e["version"], "last_modified": _fmt_time(e["when"]),
                            "matched_terms": e["matched"], "title_hit": e["title_hit"],
                            "breadcrumb": e["crumbs"], "url": e["url"]} for e in cands]}
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"检索词：{'、'.join(terms)}")
    print(f"共 {total_found} 条，显示 {len(cands)} 条：\n")
    print(table)
    if counts:
        print("\n" + deny.summary_line(counts))
    print(f"\n候选表已写入：{md_path} / {json_path}")


def cmd_stale(cfg: Config, args):
    # 过期检测：refs 文件每行「页面ID 版本号」，逐页查当前版本并分四类输出
    # （已更新 / 未变 / 已删除或无权限 / 查询失败：只有 404、403 算已删除或无权限）
    c = Client(cfg)
    rows = [["页面ID", "标题", "记录版本", "当前版本", "状态"]]
    n_up = n_same = n_gone = n_fail = 0
    for line in read_text_file(args.file).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in re.split(r"[\s,;]+", line) if p]
        if len(parts) < 2 or not parts[1].isdigit():
            log(f"stale 忽略无法解析的行：{line}")
            continue
        pid, recorded = parts[0], int(parts[1])
        try:
            d = c.get_json(f"/rest/api/content/{pid}", {"expand": "version"})
        except HttpError as e:
            if e.code in (404, 403):
                rows.append([pid, "", str(recorded), "-", "已删除或无权限"])
                n_gone += 1
            else:
                rows.append([pid, "", str(recorded), "-", "查询失败"])
                n_fail += 1
            continue
        cur = (d.get("version") or {}).get("number")
        if cur == recorded:
            status = "未变"
            n_same += 1
        else:
            status = f"已更新（记录 {recorded} → 当前 {cur}）"
            n_up += 1
        rows.append([pid, d.get("title", ""), str(recorded), str(cur if cur is not None else "-"), status])
    print(md_table(rows) if len(rows) > 1 else "（refs 文件里没有有效的「页面ID 版本号」行）")
    print(f"\n共 {n_up + n_same + n_gone + n_fail} 条：已更新 {n_up}，未变 {n_same}，"
          f"已删除或无权限 {n_gone}，查询失败 {n_fail}")


def cmd_spaces(cfg: Config, args):
    c = Client(cfg)
    n = 0
    for s in c.paged("/rest/api/space", {"type": args.type} if args.type != "all" else {}, limit=100,
                     max_items=args.limit):
        n += 1
        print(f"- {s.get('key')}  {s.get('name')}  ({s.get('type')})")
    print(f"\n共 {n} 个空间")


def cmd_space(cfg: Config, args):
    c = Client(cfg)
    key = args.key
    if args.tree:
        roots = list(c.paged(f"/rest/api/space/{key}/content/page", {"depth": "root"}, limit=100))
        deny = DenyFilter(cfg, getattr(args, "deny_file", None))
        skipped = [0]

        def walk(p, dep, anc):
            # anc 是 p 的祖先 ID 链（树根在前）：沿树传递，命中排除的节点整棵子树不列、只计数
            for k in list_children(c, p):
                if deny.active and deny.check(k.get("id"), k.get("title", ""), key, anc + [str(p)]):
                    skipped[0] += 1
                    continue
                print(f"{'  ' * dep}- {k.get('title')}  (ID {k.get('id')})")
                if dep + 1 < args.depth:
                    walk(k["id"], dep + 1, anc + [str(p)])

        for r in roots:
            if deny.active and deny.check(r.get("id"), r.get("title", ""), key, []):
                skipped[0] += 1
                continue
            print(f"- {r.get('title')}  (ID {r.get('id')})")
            if args.depth > 1:
                walk(r["id"], 1, [])
        if skipped[0]:
            print(f"（已按排除清单过滤 {skipped[0]} 个页面）")
        return
    n = 0
    for p in c.paged("/rest/api/content", {"spaceKey": key, "type": "page", "expand": "version"}, limit=100,
                     max_items=args.limit):
        n += 1
        ver = p.get("version") or {}
        print(f"- {p.get('title')}  (ID {p.get('id')}, {_fmt_time(ver.get('when', ''))})")
    print(f"\n共列出 {n} 个页面（上限 {args.limit}，可用 --limit 调整；--tree 查看层级）")


def cmd_attachments(cfg: Config, args):
    c = Client(cfg)
    pid = resolve_page_id(c, args.ref)
    deny = DenyFilter(cfg, getattr(args, "deny_file", None))
    if deny.active:
        # 附件同样受排除清单约束：先查页面元数据，命中就拒绝（退出码 5），不列附件
        _, hit = deny.check_page(c, pid)
        if hit:
            raise DeniedError(f"页面 {pid} 被排除清单拒绝读取：{hit[1]}")
    rows = [["#", "ID", "文件名", "大小", "类型", "版本", "下载地址"]]
    for i, a in enumerate(c.paged(f"/rest/api/content/{pid}/child/attachment", {"expand": "version"}), 1):
        e = a.get("extensions") or {}
        rows.append([str(i), a.get("id", ""), a.get("title", ""), human_size(e.get("fileSize", 0)),
                     e.get("mediaType", ""), str((a.get("version") or {}).get("number", "")),
                     c.url((a.get("_links") or {}).get("download", ""))])
    print(md_table(rows) if len(rows) > 1 else "该页面没有附件")


def cmd_download(cfg: Config, args):
    c = Client(cfg)
    url = c.url(args.url) if not re.match(r"^https?://", args.url) else args.url
    deny = DenyFilter(cfg, getattr(args, "deny_file", None))
    if deny.active:
        # 排除清单生效时下载也要过检查：能从地址解析出页面 ID 就查该页；
        # 解析不出页面 ID 的地址无法确认归属，一律拒绝（退出码 5）
        m = re.search(r"/download/attachments/(\d+)(?:/|$)", urllib.parse.urlsplit(url).path)
        if not m:
            raise DeniedError("排除清单已启用，但下载地址解析不出页面 ID，拒绝下载")
        _, hit = deny.check_page(c, m.group(1))
        if hit:
            raise DeniedError(f"页面 {m.group(1)} 被排除清单拒绝下载：{hit[1]}")
    root = output_root(cfg, args.out)
    fn = safe_name(urllib.parse.unquote(posixpath.basename(urllib.parse.urlsplit(url).path)) or "download.bin")
    dest = os.path.join(root, "downloads", fn)
    size = c.download(url, dest, (cfg.int("max_attachment_mb") or 50) * 1024 * 1024 * 4)
    print(f"已下载：{dest}（{human_size(size)}）")
    if not args.no_extract:
        r = extract_file(dest, os.path.join(root, "downloads", "text"), cfg)
        print(f"内容提取：{r.summary}" + (f" → {r.md_path}" if r.md_path else ""))
        if r.md_path:
            emit(read_text_file(r.md_path), args.max_chars if args.max_chars is not None else cfg.int("max_print_chars"))


def cmd_extract(cfg: Config, args):
    path = os.path.abspath(args.file)
    if not os.path.exists(path):
        raise SystemExit(f"文件不存在：{path}")
    out = args.out or os.path.join(output_root(cfg), "extract")
    r = extract_file(path, out, cfg)
    print(f"{os.path.basename(path)}：{r.summary}")
    if r.md_path:
        print(f"Markdown：{r.md_path}\n")
        emit(read_text_file(r.md_path), args.max_chars if args.max_chars is not None else cfg.int("max_print_chars"))
    elif r.images:
        print("这是图片文件，可直接用识图能力查看：" + r.images[0][0])


def main(argv=None):
    global VERBOSE
    setup_console(os.environ.get("CONFLUENCE_CONSOLE_ENCODING") or "utf-8")
    ap = argparse.ArgumentParser(
        prog="confluence.py",
        description="只读访问 Confluence（页面/图片/附件/搜索），纯 Python 标准库实现。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  confluence.py check\n"
               "  confluence.py page 123456\n"
               "  confluence.py page \"http://IP:8090/pages/viewpage.action?pageId=123456\" --print all\n"
               "  confluence.py page 123456 --recursive --depth 2\n"
               "  confluence.py search \"关键字\" --space KEY\n"
               "  confluence.py find --term 支付 --term TXN001 --term 订单表\n"
               "  confluence.py stale --file refs.txt\n"
               "  confluence.py children 123456 --depth 3\n"
               "  confluence.py extract 本地文件.docx\n")
    ap.add_argument("--config", help="配置文件路径（默认在 skill 根目录找 config.ini）")
    ap.add_argument("-v", "--verbose", action="store_true", help="输出调试信息")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("check", help="测试连接与登录")
    p.add_argument("--out", help="输出目录")

    p = sub.add_parser("page", help="获取页面（正文+图片+附件+评论），可多个")
    p.add_argument("refs", nargs="+", help="页面 ID 或 URL（可多个）")
    p.add_argument("--attachments", choices=["all", "images", "none"], default="all",
                   help="附件下载：all=全部(默认) images=仅图片 none=不下载")
    p.add_argument("--no-extract", action="store_true", help="下载附件但不提取内容")
    p.add_argument("--no-comments", action="store_true", help="不获取评论")
    p.add_argument("--include-ai-comments", action="store_true",
                   help="把「【AI 核对】」评论按普通评论输出（默认单独成节，不作证据）")
    p.add_argument("--deny-file", help="追加排除清单文件（每行 类型:值，见 config 的 deny_* 说明）")
    p.add_argument("--page-version", dest="version", type=int, help="获取指定历史版本号")
    p.add_argument("--recursive", action="store_true", help="连同子页面一起导出")
    p.add_argument("--depth", type=int, default=1, help="递归深度（默认 1 层子页面）")
    p.add_argument("--max-pages", type=int, help="递归最多导出的页面数")
    p.add_argument("--print", choices=["page", "all", "none"], default="page",
                   help="控制台输出：page=页面正文(默认) all=正文+所有附件文本 none=只输出路径")
    p.add_argument("--max-chars", type=int, help="控制台最多输出字符数（默认见 config.ini）")
    p.add_argument("--force", action="store_true", help="忽略缓存重新下载")
    p.add_argument("--out", help="输出目录")

    p = sub.add_parser("children", help="列出子页面树")
    p.add_argument("ref")
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--deny-file", help="追加排除清单文件")

    p = sub.add_parser("search", help="搜索（关键字或 CQL）")
    p.add_argument("query")
    p.add_argument("--space", help="限定空间 KEY")
    p.add_argument("--type", default="default", choices=["default", "page", "blog", "attachment", "all"])
    p.add_argument("--title", action="store_true", help="只搜标题")
    p.add_argument("--cql", action="store_true", help="把 query 当作原生 CQL")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--deny-file", help="追加排除清单文件")

    p = sub.add_parser("find", help="多角度查找：多个词一次查完，合并去重排序后输出候选表")
    p.add_argument("--term", action="append", help="检索词（可多次给出）")
    p.add_argument("--terms-file", help="检索词文件（每行一个词）")
    p.add_argument("--space", action="append", help="限定空间 KEY（可多次给出）")
    p.add_argument("--since", help="只看最后修改不早于该日期的页面（YYYY-MM-DD）")
    p.add_argument("--type", default="page", choices=["page", "blogpost"], help="内容类型（默认 page）")
    p.add_argument("--limit", type=int, default=20, help="候选表最多条数（默认 20）")
    p.add_argument("--deny-file", help="追加排除清单文件")
    p.add_argument("--out", help="输出目录")

    p = sub.add_parser("stale", help="过期检测：对比 refs 文件记录的版本与当前版本")
    p.add_argument("--file", required=True, help="引用清单文件（每行：页面ID 版本号）")

    p = sub.add_parser("spaces", help="列出空间")
    p.add_argument("--type", default="global", choices=["global", "personal", "all"])
    p.add_argument("--limit", type=int, default=500)

    p = sub.add_parser("space", help="列出某空间的页面")
    p.add_argument("key")
    p.add_argument("--tree", action="store_true", help="按层级显示")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--deny-file", help="追加排除清单文件")

    p = sub.add_parser("attachments", help="列出页面附件")
    p.add_argument("ref")
    p.add_argument("--deny-file", help="追加排除清单文件")

    p = sub.add_parser("download", help="下载任意 Confluence 地址（带认证）并提取内容")
    p.add_argument("url")
    p.add_argument("--no-extract", action="store_true")
    p.add_argument("--max-chars", type=int)
    p.add_argument("--out")
    p.add_argument("--deny-file", help="追加排除清单文件")

    p = sub.add_parser("extract", help="提取本地文件内容（docx/xlsx/pptx/pdf/doc/xls/ppt/vsdx/...）")
    p.add_argument("file")
    p.add_argument("--out")
    p.add_argument("--max-chars", type=int)

    args = ap.parse_args(argv)
    VERBOSE = args.verbose
    cfg = load_config(args.config)
    setup_console(cfg.console_encoding or "utf-8")
    if not args.cmd:
        ap.print_help()
        return 0
    handlers = {"check": cmd_check, "page": cmd_page, "children": cmd_children, "search": cmd_search,
                "find": cmd_find, "stale": cmd_stale,
                "spaces": cmd_spaces, "space": cmd_space, "attachments": cmd_attachments,
                "download": cmd_download, "extract": cmd_extract}
    try:
        handlers[args.cmd](cfg, args)
        return 0
    except AuthError as e:
        print(f"[认证错误] {e}", file=sys.stderr)
        return 3
    except NetError as e:
        print(f"[网络错误] {e}", file=sys.stderr)
        return 4
    except DeniedError as e:
        print(f"[已拒绝] {e}", file=sys.stderr)
        return 5
    except HttpError as e:
        print(f"[请求错误] {e}", file=sys.stderr)
        return 5
    except ValueError as e:
        print(f"[参数错误] {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
