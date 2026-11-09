# confluence-pages

Confluence「AI 起草页面」工具：编码助手把整理好的文档（知识沉淀、
对外确认单等）发布到 Confluence 的 **AI 专用空间**里供人审核。
它只做两件事：在白名单空间内新建页面；修改自己建的、没被人改过、
也还没被批准的页面。不编辑别人的页面，不删除、不移动、不改名、
不改权限、不传附件。与 confluence-reader（只读）、
confluence-commenter（只发评论）配套使用。

只用 Python 标准库，Python 3.8+，Windows 与 Linux 都能运行。

## 安装

无需安装第三方包。把整个 `confluence-pages/` 目录放到 skill 目录即可。
仓库根目录另附 `confluence-pages.zip`（内容与本目录相同，不含 tests/），
解压即是一个可直接分发的 skill 包。

## 配置

```bash
cp config.ini.example config.ini # Windows: copy config.ini.example config.ini
```

在 `config.ini` 里填：

- `base_url`：Confluence 地址，可带 context path，如 `http://host:8090/confluence`
- `token`：个人访问令牌（与 confluence-commenter 共用同一个写入令牌）；
  或 `username` + `password`（Basic 认证）
- `verify_ssl`：内网自签证书可设 `false`
- `console_encoding`：默认 `utf-8`，Windows 旧控制台可设 `gbk`
- `[pages] allow_spaces`：**必填**，AI 专用空间的 KEY，可多个。
  为空时 create / update 一律拒绝；所有写操作、status、pull 都只
  覆盖这些空间
- `[pages] allow_parent_ids`：可选的父页面白名单。配了就要求父页面
  同时在名单里；没配则 allow_spaces 内任意父页面下都能新建（含空间首页）
- `[pages] allow_url_hosts`：可选，敏感扫描的 URL 主机白名单
  （`host` 或 `host:port`，可多个）。白名单内的 URL 及其中的 IP
  不会被扫描拦截；base_url 的主机（含端口）永远自动在白名单内。
  详见「安全说明」的 URL 主机白名单一条
- `[limits]`：正文大小上限（默认 204800 字节 = 200KB）、单次运行
  写操作上限（默认 10 次）
- `[deny]`：拒绝清单——空间 key、页面 ID、标题关键词，命中就拒绝

`config.ini` 已被 `.gitignore` 忽略，不会进仓库。也可以用环境变量
覆盖凭据：`CONFLUENCE_W_BASE_URL`、`CONFLUENCE_W_TOKEN`、
`CONFLUENCE_W_USERNAME`、`CONFLUENCE_W_PASSWORD`（另可用
`CONFLUENCE_W_CONFIG` 指定配置文件路径、`CONFLUENCE_W_RUN_ID`
标记单次运行）。工具默认**不走系统代理**，只访问 `base_url` 这一个地址。

## 推荐配置

- **请管理员新建 AI 专用空间**，写入令牌对应的账号只在这个空间有
  “新建/编辑页面”权限，在其他空间只有“查看 + 评论”权限。这样就算
  工具出错，服务器也会拦住——工具自身的 `allow_spaces` 校验是第一道
  闸，服务器权限是第二道闸，两道都应当配。
- **confluence-reader 的 deny_spaces 建议配上 AI 空间的 KEY**。这样
  助手找资料、做核对时不会把 AI 自己写的页面当作证据；需要看 AI
  页面时，用本工具的 `pull`。
- **审核人只需要关注 AI 空间里带 `ai-pending` 标签的页面**。审核
  通过后把标签改为 `ai-approved`，页面即冻结，AI 不再修改；有修改
  意见就在页面下评论，status 会把评论列出来。

## 命令速查

| 命令 | 作用 |
| --- | --- |
| `check` | 测试连接与认证（取当前用户，并显示 allow_spaces 配置状态） |
| `create --parent <ID> --title <标题> --file <正文.md> [--yes]` | 在父页面下新建页面；不带 `--yes` 只预览（打印将要写入的 storage 全文），不发任何写请求 |
| `update <页面ID> --file <正文.md> [--yes]` | 修改本工具自建页面；不带 `--yes` 只预览，输出新旧正文的 unified diff（按 Markdown 文本比较） |
| `status [页面ID]` | 列出登记表里 allow_spaces 内的页面状态：待审核 / 已批准 / 被人修改过 / 已删除 / 有评论（列评论人、时间、前 100 字） |
| `pull <页面ID> [--out <路径>]` | 把服务器当前正文转成 Markdown 存到本地（默认 `logs/pull/<页面ID>.md`），供人改过之后对比差异 |
| `log` | 列出本地写操作日志（`logs/ops.jsonl`） |

退出码：`0` 成功；`2` 参数或规则拒绝；`3` 认证失败；`4` 网络错误；
`5` 服务器错误；`6` 写操作结果不确定（POST/PUT 遇网络错误，先核实、不要直接重试）。

## 工作方式

- **新建**：父页面必须存在、所在空间在 `allow_spaces` 内、不在拒绝
  清单里；（配了 `allow_parent_ids` 时）父页面还要在名单里。同空间
  已有同名页面会被拒绝。新页面正文顶部自动加 info 宏横幅
  （“本页由 AI 起草，状态：待审核……”），并自动加标签
  `ai-authored`、`ai-pending`；同时写入本地登记表
  （`logs/pages.jsonl`：页面 ID、标题、空间、父页面、创建时间、
  本工具最后写入的版本号、内容哈希）。
- **修改**：必须同时满足五条，缺一拒绝并说明原因——(a) 页面在登记表
  里；(b) 服务器上创建人等于当前账号；(c) 带 `ai-authored` 标签；
  (d) 当前版本号等于登记表里的版本（没人改过；有人改过会提示
  “页面已被人修改，人的修改优先，请先 pull 查看差异”）；
  (e) 没有 `ai-approved` 标签。另外页面所在空间必须在
  `allow_spaces` 内，这一条在五条之前单独检查。修改只换正文
  （标题、父页面、标签都不动，横幅保留），以版本号 +1 提交；
  服务器返回 409 版本冲突就拒绝。
  说明：Confluence 的更新接口要求请求体回传 `title` 字段，工具回传
  的是页面原标题、值完全不变，不构成改名。
- **Markdown 转换**：正文 Markdown 转 storage 格式，支持标题 1–4 级、
  段落、粗体、斜体、行内代码、有序/无序列表（含两级嵌套）、表格、
  代码块（转 code 宏，保留语言）、链接、引用块、分隔线；所有文本做
  HTML 转义；不支持的语法（图片、5 级以上标题、删除线等）原样输出
  并给出警告。`pull` 做反向转换，本工具生成的页面可往返一致
  （Markdown→storage→Markdown 内容等价；列表缩进等会规范化为
  每级 2 空格）。
- **链接协议白名单**：Markdown 链接与图片 src 只允许三类目标——
  http/https、以 `/` 开头的站内相对路径、以 `#` 开头的锚点。其他
  协议（javascript、data、vbscript、file 等，不区分大小写，含前后
  空白、百分号编码 `java%73cript:`、HTML 实体编码 `&#106;avascript:`
  这类绕过写法）一律不生成链接，只输出转义后的纯文本并给出警告。
  URL 内的括号按配对解析（`/a_(b)` 不会被截断），也支持
  `<URL>` 包裹写法。
- **站内页面链接转原生链接**：Markdown 链接、以及正文中单独一行的
  裸网址，只要指向本站（base_url）页面就识别，支持
  `/pages/viewpage.action?pageId=数字`、`/display/空间KEY/标题`、
  `/spaces/空间KEY/pages/数字/...` 三种形式（base_url 带 context
  path 时按其路径之后的部分匹配）。识别后用 GET 取目标页标题与
  空间，生成 `<ac:link>` 原生页面链接——写了链接文字的用作正文，
  没写的显示目标页标题；目标页取不到（404/无权限）时退回普通
  `<a href>` 并给出警告，不中断。预览输出会逐条列出识别出的站内
  链接（原网址 → 目标页标题（ID））。`pull` 时 ac:link 还原成
  `[标题](/display/空间KEY/标题)` 形式的 Markdown 链接，往返一致。
  给页面做链接不读目标页正文，本工具的拒绝清单管的是写入目标，
  不限制链接目标（目标在其他空间也照常生成链接）。

## 安全说明

- **写请求只有三种**（有源码测试证明）：POST 新建页面、POST 加标签、
  PUT 更新自建页面正文（全代码只有这一处 PUT）。代码里没有任何
  删除类请求，也没有移动、改名、改权限、上传附件的功能。
- **认证失败立即停**：401/403 或返回登录页时只请求 1 次、退出码 3、
  绝不重试；GET 网络错误最多重试 1 次、退出码 4；**POST/PUT 一律
  不重试**，遇网络错误用退出码 6 报“结果不确定”，并在日志里记一条
  `status=uncertain` 记录，先核实、不要直接重试。
- **内容扫描**：标题 + 正文 + 横幅在写入前统一过敏感扫描（与
  confluence-commenter 返工后同一套规则：IP、带主机名的 URL、邮箱、
  手机号、身份证号、银行卡号、密钥赋值、JDBC 串、私钥头；中文语境
  下同样有效——数字紧贴汉字、复合键名、中文键名、全角分隔符都会
  拦），命中即拒绝并打码指出规则。
- **URL 主机白名单**：config 的 `[pages] allow_url_hosts`（`host`
  或 `host:port`，可多个）配置扫描放行的主机；**base_url 的主机
  （含端口）永远自动在白名单内**。扫描时逐个用标准库解析 URL 的
  真实主机与端口（缺省端口按 http 80 / https 443 归一化）：主机
  （条目带端口时端口还须相等；条目不带端口只比主机）在白名单内
  时，该 URL 整体不算「带主机名的 URL」，其中出现的 IP 也不触发
  IP 规则——站内链接（哪怕是 IP:端口形式的完整网址）因此能正常
  写入。不在白名单的 URL 与 IP 照旧拦截。两点例外与防伪装：
  白名单 URL 的 query 参数名只要包含 token/password/session
  （如 `access_token`）照样拦截；`http://白名单@evil.com/`
  （userinfo 伪装）、`http://evil.com/?x=白名单`、
  `http://白名单.evil.com/` 都按真实主机判断，不算白名单；
  协议只认 http/https（ftp 永不放行）。
- **本地状态**：登记表与日志都在 `logs/` 下（已被 .gitignore 忽略）。
  登记表是判断“页面是不是本工具建的”的依据，不要手工伪造；
  换机器使用前先迁移 `logs/` 并核对。

## 测试

```bash
cd confluence-pages
python -m unittest discover -s tests -v
```

测试用标准库 `http.server` 起了一个模拟 Confluence（Bearer/Basic
认证、页面增查、同名检查、标签、带版本号与 409 的 PUT 更新、评论、
创建人、context path、登录页、可模拟 POST/PUT 响应丢失），覆盖：
allow_spaces 空间规则（含补充修订的 5 类空间用例）、新建全流程、
update 的 5a–5e 逐条拒绝与正例、diff 预览、409、POST/PUT 断线后的
退出码 6、敏感扫描中文语境、Markdown 每种语法与往返一致、status
五种状态、pull、写操作上限、context path、gbk 输出、源码写请求
纪律与 Python 3.8 语法自检；以及返工与追加项：链接协议白名单
（禁用协议与编码绕过、图片 src）、URL 括号配对与 <> 包裹解析、
扫描 URL 主机白名单（放行/端口不符/三类伪装/带 token 参数拦截）、
站内页面链接转 ac:link（三种地址形式、404 退回、裸网址、预览
清单、pull 往返）。

## 已知限制

- 仅适配 Confluence Server/Data Center 的 REST API v1
  （`/rest/api/content`）；Confluence Cloud 的 API v2 未适配。
- 新建页面成功但加标签失败时，页面已登记、命令以标签那一步的
  退出码报错：请人工给页面补上 `ai-authored`、`ai-pending` 标签，
  否则后续 update 会被 5c 拦下（这是有意的安全侧行为）。
- Markdown 子集之外的内容（图片、HTML 混排、宏、嵌套引用等）不在
  支持范围内：转换时会原样保留文本并警告，pull 回来的复杂页面
  可能与原文不完全等价；往返一致只保证本工具支持的语法子集。
- status 的“被人修改过”只比较版本号；人只改标签（如批准）不算
  修改正文，会按“已批准”显示。
- 单次运行计数依赖 `CONFLUENCE_W_RUN_ID` 或本地 1 小时窗口状态
  文件，多机并行时各机独立计数。
- 未在真实 Confluence 与 Windows 实机上测试（测试均为模拟服务器
  + 本机 Python 环境）；首次接入真实环境时请先在 AI 空间里小范围
  试建一页验证。
