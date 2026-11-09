# confluence-pages —— 使用说明（给助手）

这个 skill 只做两件事：把你整理好的文档**新建**到 Confluence 的 AI 专用
空间里供人审核；在人还没动过、也还没批准之前，**修改**你自己建的页面。
它不能改别人的页面，不能删、不能移动、不能改名。配套的另外两个 skill：
confluence-reader（只读，找资料、核对证据用）、confluence-commenter
（核对发现问题时发评论用）。读写分开，不要混用。

脚本：`scripts/confluence_pages.py`（只用 Python 标准库）。配置在 skill
根目录的 `config.ini`（由 `config.ini.example` 复制填写）。

## 什么时候用

1. **只用于发布 AI 整理好、需要人审核的文档**（知识沉淀、对外确认单等）。
核对别人页面发现问题时，用 confluence-commenter 发评论指出不一致，
**不要**用这个工具新建页面，更不要想办法去改别人的页面。
2. **所有新建都放在 AI 专用空间**（config 的 `allow_spaces`，管理员专门
给 AI 建的空间）。工具会强制校验：父页面所在空间不在 `allow_spaces` 里
就拒绝；`allow_spaces` 没配时 create/update 一律拒绝。不要试图绕过。
3. **AI 空间里的页面永远不作为核对证据。** 那里面是 AI 自己起草的东西，
拿它当依据等于自己证明自己。核对、找资料用 confluence-reader 时，
也不要采信 AI 空间的页面（推荐在 reader 的 deny_spaces 里配上 AI
空间 KEY）；确实需要看 AI 页面内容时，用本工具的 `pull`。

## 页面生命周期

- 新建的页面自带横幅（“本页由 AI 起草，状态：待审核……”）和两个标签：
`ai-authored`、`ai-pending`，并登记在本地登记表（`logs/pages.jsonl`）。
- 审核人把标签 `ai-pending` 改为 `ai-approved` 后，页面**冻结**：
工具会拒绝再改（条件 5e），你也不要尝试绕过。
- 人在页面上直接改过（版本号和登记表对不上）时，工具拒绝 update
（条件 5d）。这时用 `pull` 把服务器当前正文拉下来看差异，交给用户决定
怎么处理，**不要**覆盖人的修改。
- update 能改的页面必须同时满足：登记表里有记录、创建人是当前账号、
带 `ai-authored` 标签、版本与登记一致、没有 `ai-approved` 标签
（5a–5e，代码逐条强制，缺一不可）。

## 命令用法

```bash
# 1. 先测连接与认证
python scripts/confluence_pages.py check

# 2. 新建：先不带 --yes 预览（会跑全部检查并打印将要写入的 storage 正文）
python scripts/confluence_pages.py create --parent <父页面ID> --title <标题> --file <正文.md>
# 用户确认后再执行
python scripts/confluence_pages.py create --parent <父页面ID> --title <标题> --file <正文.md> --yes

# 3. 修改自建页面：不带 --yes 预览（输出新旧正文的 unified diff，按 Markdown 比较）
python scripts/confluence_pages.py update <页面ID> --file <新正文.md>
python scripts/confluence_pages.py update <页面ID> --file <新正文.md> --yes

# 4. 状态：待审核 / 已批准 / 被人修改过 / 已删除 / 有评论（列评论人、时间、前 100 字）
python scripts/confluence_pages.py status [页面ID]

# 5. 把服务器当前正文转成 Markdown 存到本地（默认 logs/pull/<页面ID>.md）
python scripts/confluence_pages.py pull <页面ID>

# 6. 本地写操作日志
python scripts/confluence_pages.py log
```

## 硬性流程与注意事项

1. **先预览，后 --yes。** create/update 不带 `--yes` 只预览、不发送。
把预览结果（create 是将要写入的全文，update 是 diff）给用户确认后再
加 `--yes` 执行，不要跳过确认。
2. 正文是 Markdown 文件，工具转成 Confluence storage 格式。支持：
标题 1–4 级、段落、粗/斜体、行内代码、有序/无序列表（含两级嵌套）、
表格、代码块（转 code 宏，保留语言名）、链接、引用块、分隔线。
不支持的语法（图片、5 级以上标题、删除线等）会原样输出并在预览里
给出警告——看到警告先改文档，不要带着警告提交。正文上限默认
200KB（config 可调）。所有文本都会做 HTML 转义，正文里写 `<标签>`
只会当普通文字显示。
3. **内容里不得出现真实姓名、IP、账号、客户数据**，也不得出现手机号、
身份证号、银行卡号、邮箱、带主机名的内网链接、`password=`/`token:`
形式的赋值等敏感信息。标题、正文、横幅会统一过敏感扫描，命中即拒绝
并打码指出规则。被拦截时**修改内容本身，不要改写规避扫描**。
4. **认证失败（退出码 3）立即停下**，把情况告诉用户，不要换账号、
换令牌或换工具重试。网络错误：GET 工具内部最多重试 1 次（退出码 4）；
写操作（退出码 6）表示 POST/PUT 时网络出错、操作可能已在服务器生效：
先 `status`/`pull` 核实，**不要直接重试**（日志里有 uncertain 记录）。
版本冲突 409 同理：先 pull 看差异，交给用户决定。
5. 防误发是代码强制的，不要试图绕过：同一空间同名页面会被拒建；
config 拒绝清单（空间、页面 ID、标题关键词）命中就拒绝；单次运行
写操作上限默认 10 次（config 可调；同一批任务请在环境变量
`CONFLUENCE_W_RUN_ID` 里设固定值，计数才准）。
6. 凭据只在 `config.ini` 或 `CONFLUENCE_W_*` 环境变量里，永远不要把
令牌、密码写进页面正文、日志或对话输出。本工具与 commenter 共用
同一个写入令牌口径（`CONFLUENCE_W_*`）。
