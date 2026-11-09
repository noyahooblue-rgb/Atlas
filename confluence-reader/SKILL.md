---
name: confluence-reader
description: 只读访问公司内网 Confluence：读取页面正文（转 Markdown）、页面图片、附件（Word/Excel/PPT/PDF/Visio/doc/xls/ppt 等）内容，以及评论、子页面树、空间、关键字/CQL 搜索。当用户给出 Confluence 链接、页面 ID，或要求查找/阅读/总结 Confluence 上的文档、需求、设计、接口说明时使用。纯 Python 标准库实现，无需安装任何依赖。
---

# Confluence 读取工具（confluence-reader）

## 运行方式

- 脚本：`scripts/confluence.py`（只依赖 Python 3.8+ 标准库，**不需要 pip 安装任何包**，不要尝试安装 requests 等库）
- 优先用 `run_skill_script` 运行 `scripts/confluence.py`，参数照下文传入。
- 如果 `run_skill_script` 不可用或报错，改用命令行：`python "<本skill目录>/scripts/confluence.py" <命令> <参数>`
  （本机系统 Python 也可以，例如 `C:\Users\<用户>\AppData\Local\Programs\Python\Python312\python.exe`）
- 配置在 skill 根目录的 `config.ini`（地址、账号由用户自己填写）。**不要读取、打印或修改 config.ini 中的密码/令牌。**

## 命令速查

| 目的 | 命令 |
|---|---|
| 测试连接/登录 | `check` |
| 读取页面（正文+图片+附件+评论） | `page <页面ID或URL> [更多ID或URL…]` |
| 同时把附件全文打印出来 | `page <ID> --print all` |
| 只要正文和图片，不下载其它附件 | `page <ID> --attachments images` |
| 只要正文 | `page <ID> --attachments none --no-comments` |
| 连同子页面一起读 | `page <ID> --recursive --depth 2 [--max-pages 30]` |
| 读历史版本 | `page <ID> --page-version 5` |
| 列出子页面树 | `children <ID> --depth 3` |
| 关键字搜索 | `search "关键字" [--space KEY] [--title] [--type page/blog/attachment/all] [--limit 20]` |
| CQL 搜索 | `search 'space = ABC and label = "接口" and lastmodified > now("-30d")' --cql` |
| 列出空间 / 空间内页面 | `spaces` / `space KEY [--tree --depth 2]` |
| 列出页面附件 | `attachments <ID>` |
| 下载任意 Confluence 地址并提取 | `download "<链接或 /download/... 路径>"` |
| 提取本地文件内容 | `extract <本地文件路径>` |

页面参数支持：纯数字 ID、`.../pages/viewpage.action?pageId=123`、`.../display/空间KEY/页面标题`、`.../x/短链`、`.../spaces/KEY/pages/123/...`。

## 推荐工作流

1. **读页面**：`page <URL>`。控制台会输出页面 Markdown（含元信息、正文、附件表、图片清单、评论）。
   - 输出被截断时，用文件读取工具直接读 `page.md`（路径在输出末尾）。
2. **看图片**：输出中的「可查看的图片文件」列出了本地图片绝对路径（正文配图、附件图片、Word/PPT/Excel/PDF 中导出的内嵌图片、draw.io/Gliffy 渲染图）。
   用户的问题涉及图片内容（截图、流程图、架构图、原型、表格截图）时，**用读图/读文件工具打开这些图片进行识图**，不要只根据文件名猜测。
3. **读附件**：附件表的「内容提取」列给出了每个附件转成的 `.md` 文件路径，按需读取；Excel 完整数据在同目录 `.csv`。
   - 想一次拿全部内容：`page <ID> --print all`（注意输出很长时会被截断，再去读文件）。
4. **找文档**：不知道页面 ID 时先 `search "关键字"`，再对结果 `page <ID>`；看目录结构用 `children` 或 `space KEY --tree`。
5. **多个页面**：一次调用传多个 ID（`page 111 222 333`），比多次调用高效；整个目录用 `--recursive`。
6. 再次读取同一页面时，未变化的附件会走本地缓存，速度很快；需要强制刷新加 `--force`。

## 输出目录结构

```
confluence_out/<页面ID>/
  page.md            页面 Markdown（主入口）
  page.storage.xml   原始存储格式（宏参数等细节需要时查看）
  meta.json          元数据与附件清单
  att/               下载的附件和图片原文件
  att_text/          附件提取结果：<文件名>.md、Excel 的 .csv、<文件名>_media/ 内嵌图片
```

## 规则（必须遵守）

- **只读**：本工具只做读取；不要尝试创建、修改、删除 Confluence 内容。
- **认证失败（退出码 3 / 出现「认证错误」）立即停止**，把错误信息原样告诉用户，让用户检查 `config.ini`。
  不要换方式反复重试——连续失败会触发验证码或锁定账号。
- 网络错误（退出码 4）：提示用户检查 base_url、内网连通性、代理设置；可重试一次。
- 页面不存在或无权限（404）：告诉用户可能是 ID 错误或该账号无权限。
- 回答时以页面和附件的实际内容为准；内容中没有的信息不要编造。引用时注明来自哪个页面/附件。
- 「疑似扫描件」的 PDF：文字层为空，需查看导出的页面图片识图。
- `.emf/.wmf/.tif` 等格式图片模型可能无法直接查看，如实告知用户。
- 设置了**打开密码**的 PDF / Office 文件无法解析，如实告知用户（只限制复制/打印的加密 PDF 可以正常读取）。

## 故障排查

| 现象 | 处理 |
|---|---|
| 【配置未填写】 | 让用户编辑 skill 目录下 `config.ini` 的 base_url / username / password |
| 认证失败 401 | 用户名密码错误或账号无 API 权限；公司若用 SSO，改填 `cookie` 或 `token` |
| 触发验证码 | 让用户先用浏览器登录一次 Confluence 并输入验证码 |
| 返回登录页面 | 服务器不接受当前认证方式，改用 `token` 或 `cookie` |
| 网络错误/超时 | 核对 IP 端口；内网一般不需代理（默认已绕过系统代理）；https 自签证书设 `verify_ssl = false` |
| 中文乱码 | 把 `config.ini` 中 `console_encoding` 改为 `gbk`，或直接读取生成的 .md 文件（始终是 UTF-8） |
| .doc/.xls/.ppt 提取不全 | `config.ini` 中设 `legacy_office = com`，借助本机 Office/WPS 转换（保留表格和图片） |
