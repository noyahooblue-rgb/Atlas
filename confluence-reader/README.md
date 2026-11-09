# confluence-reader 使用说明（给人看的）

一个让 AI 助手只读访问 Confluence 的 skill。**纯 Python 标准库实现，不需要 pip 安装任何东西**，内网直接可用。

## 安装（3 步）

1. 把整个 `confluence-reader` 文件夹放到 iCode 的 skills 目录（与原来的 `confluence-extract` 同级）。
2. 用记事本打开 `config.ini`，填写三项：
   ```ini
   base_url = http://你的ConfluenceIP:端口
   username = 你的用户名
   password = 你的密码
   ```
3. 打开命令行测试：
   ```
   python confluence-reader\scripts\confluence.py check
   ```
   看到「✔ 登录成功」就可以了。之后在 iCode 里直接说「读一下这个 Confluence 页面 <链接>」即可。

## 能做什么

| 内容 | 支持情况 |
|---|---|
| 页面正文 | 转成 Markdown：标题、列表、表格（含合并单元格）、代码块、提示框、任务列表、展开块、状态标签、链接 |
| 页面图片 | 下载到本地，Markdown 中替换为本地路径，多模态模型可直接识图；含引用其它页面的图片 |
| Word | .docx 完整（标题层级、列表、表格、超链接、文本框、批注、内嵌图片导出）；.doc 纯文本 |
| Excel | .xlsx/.xls：所有工作表、合并单元格、日期格式、隐藏表标注、表内图片；完整数据另存 CSV |
| PPT | .pptx：每页标题/文字/表格/图片/备注；.ppt 纯文本 |
| PDF | 文字提取（含中文字体）；内嵌图片导出；扫描件自动识别并导出页面图片供识图；“禁止复制/打印”的加密 PDF 也能读（有打开密码的除外） |
| 其它 | Visio(.vsdx，含连线关系)、CSV、HTML、邮件(.eml)、各种文本/代码文件、压缩包目录 |
| 其它功能 | 评论（含行内评论）、标签、面包屑路径、历史版本、子页面树、空间列表、关键字/CQL 搜索、递归导出整个目录 |
| 效率 | 附件并发下载；按附件版本缓存，再次读取同一页面不重复下载；一次可读多个页面 |

## 认证方式（任选其一）

- **用户名 + 密码**：最简单，填 `username` / `password`。
- **个人访问令牌（推荐，Confluence 7.9+）**：头像 → 设置 → 个人访问令牌 → 创建，填到 `token`。可随时吊销，不暴露密码。
- **Cookie**：公司用单点登录、前两种都不行时使用，见 `config.ini` 注释。

也可以不把密码写进文件，改用环境变量：`CONFLUENCE_PASSWORD`、`CONFLUENCE_TOKEN`（同理还有 `CONFLUENCE_BASE_URL`、`CONFLUENCE_USERNAME`）。

## 安全说明

- 工具只发 GET 请求读取数据（表单登录除外），不会修改 Confluence 上的任何内容。
- 认证失败会**立即停止、不重试**，避免多次失败触发验证码或锁定域账号。
- `config.ini` 中是明文密码：不要提交到 Git（已附带 `.gitignore`），不要发给别人。条件允许优先用令牌。
- 默认不走系统代理，请求只发往你配置的 Confluence 地址。

## 常见问题

- **iCode 里中文乱码**：`config.ini` 里 `console_encoding = gbk`。生成的 .md 文件始终是 UTF-8。
- **.doc/.xls/.ppt 内容不全**：`legacy_office = com`，借助本机 Office/WPS 转换（Windows，较慢但保留表格和图片）。
- **地址有 context path**：浏览器地址如 `http://10.1.1.1:8090/confluence/pages/...`，则 `base_url = http://10.1.1.1:8090/confluence`。
- **输出太长被截断**：完整内容在 `confluence_out/<页面ID>/page.md`，AI 会自动去读。

## 命令行速查

```
python scripts/confluence.py -h                    查看全部命令
python scripts/confluence.py check                 测试连接
python scripts/confluence.py page <ID或URL>         读取页面
python scripts/confluence.py page <ID> --print all 连附件全文一起输出
python scripts/confluence.py page <ID> --recursive --depth 2
python scripts/confluence.py search "关键字" --space KEY
python scripts/confluence.py children <ID> --depth 3
python scripts/confluence.py extract 本地文件.docx
```
