# confluence-commenter

Confluence「只发评论」工具：编码助手核对完文档页面与当前代码之后，只能在
页面底部留一条简短评论指出不一致——不能改页面、不能删评论、不发行内评论。
与只读的 confluence-reader 配套使用，读写分离、令牌分离。

只用 Python 标准库，Python 3.8+，Windows 与 Linux 都能运行。

## 安装

无需安装第三方包。把整个 `confluence-commenter/` 目录放到 skill 目录即可。
仓库根目录另附 `confluence-commenter.zip`（内容与本目录相同，不含 tests/），
解压即是一个可直接分发的 skill 包。

## 配置

```bash
cp config.ini.example config.ini # Windows: copy config.ini.example config.ini
```

在 `config.ini` 里填：

- `base_url`：Confluence 地址，可带 context path，如 `http://host:8090/confluence`
- `allow_url_hosts`：敏感扫描的 URL 主机白名单（`host` 或 `host:port`，逗号分隔）。
评论里贴白名单主机的站内链接会放行；`base_url` 的主机（含端口）永远自动在
白名单内。详见下方「内容扫描」。
- `token`：个人访问令牌（推荐，与只读令牌分开）；或 `username` + `password`（Basic 认证）
- `verify_ssl`：内网自签证书可设 `false`
- `console_encoding`：默认 `utf-8`，Windows 旧控制台可设 `gbk`
- `[limits]`：正文字符/行数上限、单次运行发送上限（默认 300 字符 / 5 行 / 10 条）
- `[deny]`：拒绝清单——空间 key、页面 ID、标题关键词，命中就拒绝发送

`config.ini` 已被 `.gitignore` 忽略，不会进仓库。也可以用环境变量覆盖凭据：
`CONFLUENCE_W_BASE_URL`、`CONFLUENCE_W_TOKEN`、`CONFLUENCE_W_USERNAME`、
`CONFLUENCE_W_PASSWORD`（另可用 `CONFLUENCE_W_CONFIG` 指定配置文件路径、
`CONFLUENCE_W_RUN_ID` 标记单次运行）。

工具默认**不走系统代理**，只访问 `base_url` 这一个地址。

## 命令速查

| 命令 | 作用 |
| --- | --- |
| `check` | 测试连接与认证（取当前用户） |
| `preview <页面ID或URL> --verdict wrong\|partial (--text... \| --file...) [--code-ref...] [--followup]` | 取页面标题/版本，生成最终评论，跑全部检查，原样打印将要发送的内容，不发送 |
| `send... --yes` | 参数同 preview；**必须带 `--yes`** 才真正发送，不带直接拒绝 |
| `status [--page ID]` | 对日志里每条已发评论查状态：未回复 / 有回复（列回复人、时间、前 100 字）/ 页面已更新 / 评论已删除 |
| `log` | 列出本地发送日志 |

退出码：`0` 成功；`2` 参数或规则拒绝；`3` 认证失败；`4` 网络错误；
`5` 服务器错误；`6` 发送结果不确定（POST 遇网络错误，先核实、不要直接重发）。

评论的最终形态由工具拼装：`` + 正文 + 固定落款
`（以上由 AI 依据当前代码核对生成{，代码版本：<code-ref>}；如有不同意见请直接回复本评论）`，
正文经 HTML 转义、按行转成段落后以 storage 格式发出。

## 安全说明

- **写操作只有两种**：新建页面底部评论、回复自己之前发的评论（ancestors 指向
父评论）。代码里没有 PUT / DELETE 请求（测试有源码扫描用例证明），不改页面、
不加标签、不删任何东西。
- **认证失败立即停**：401/403 或返回登录页时只请求 1 次、退出码 3、绝不重试；
GET 网络错误最多重试 1 次、退出码 4；**POST 一律不重试**，遇网络错误用
退出码 6 报「发送结果不确定」（评论可能已发到服务器），并在日志里记一条
`status=uncertain` 记录，先核实、不要直接重发。凭据不打印、不写日志。
- **内容扫描**：正文发送前强制检查——长度/行数上限、禁止代码块、禁止
verdict=correct（一致不发），并扫描敏感信息（IP、带主机名的 URL、邮箱、
手机号、身份证号、银行卡号、密钥赋值、JDBC 串、私钥头），命中即拒绝并
打码指出规则。扫描在中文语境下同样有效：数字紧贴汉字、复合键名
（`api_key=`、`access_token:` 等）、中文键名（`密码：`、`口令=`、`密钥：`）
与全角分隔符都会拦。**站内链接例外**：URL 主机（含端口）命中白名单时，
该 URL 整体放行（其中的 IP 也不触发 IP 规则）——白名单是 config 的
`allow_url_hosts`（`host` 或 `host:port`，逗号分隔）加上 `base_url` 的
主机（含端口，自动并入）。判定用 `urllib.parse` 解析真实主机，
`http://白名单主机@evil.com/` 这类 userinfo 伪装、`?x=白名单主机`、
`白名单主机.evil.com` 都不算白名单；协议只认 http/https；URL 的 query
里带 `token`/`password`/`session` 类参数名（如 `access_token`）时，
即使主机在白名单也照样拦截。不在白名单的 URL 和 IP 照旧拦截。
评论保持纯文本链接即可，工具不做任何转换。落款里的 `--code-ref` 也受检：限 64 字符、只允许
字母/数字/`.`/`-`/`_`/`/`/`:`/`@`，且最终评论全文（含落款）会再统一
过一遍敏感扫描。
- **防误发**：同一页面同一正文幂等拒绝（哈希只算页面 ID + 正文，
换 code-ref 不能绕过；发送结果不确定的记录也算已发）；同页有未结的
本工具评论时拒绝新发（`--followup` 才能以回复形式跟进）；单次运行
发送上限；config 拒绝清单。每次发送在 `logs/comments.jsonl` 写一行
记录（时间、页面、版本、评论 ID、内容哈希、verdict、code-ref）。

## 测试

```bash
cd confluence-commenter
python -m unittest discover -s tests -v
```

测试用标准库 `http.server` 起了一个模拟 Confluence（Bearer/Basic 认证、
页面/评论/子评论、可模拟评论被删、页面版本递增、context path、返回登录页），
覆盖认证、preview/send、内容规则、每条敏感信息规则、拒绝清单、幂等与跟进、
status 四种状态、运行上限、context path、gbk 输出、源码无 PUT/DELETE 与
Python 3.8 语法自检。

## 已知限制

- 仅适配 Confluence Server/Data Center 的 REST API v1（`/rest/api/content`）；
Confluence Cloud 的 API v2 未适配。
- status 的「页面已更新」只比较版本号；页面被改名等不在比较范围内。
- 单次运行计数依赖 `CONFLUENCE_W_RUN_ID` 或本地 1 小时窗口的状态文件
（`logs/run_state.json`），多机并行时各机独立计数。
