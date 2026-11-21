# precheck — 提交前检查工具包

用机器结果代替智能体自述的三个小工具，专为离线内网环境设计：
纯 Python 标准库实现，不联网、不装任何第三方包，复制即用。
对被检查的文件严格只读。

| 子命令 | 解决的问题 |
| --- | --- |
| `snapshot` / `verify` | 已批准的文档在批准后被改动，靠指纹清单复核并细分差异 |
| `junit-tally` | 报告里写的测试用例数和实际对不上，按 JUnit XML 机器对账 |
| `diffstat` | 一轮改动到底改了多少，说不清时用 git 只读命令统计 |
## 安装

把整个 `precheck/` 目录复制到任意位置即可，没有别的依赖。
需要 Python 3.8 或以上。

两种运行方式等价（在 `precheck/` 的上一层目录执行第一种，
在 `precheck/` 目录内执行第二种）：

```bat
python -m precheck --help
python precheck.py --help
```

每个子命令都支持 `--json` 输出给其他脚本用，`--encoding` 指定输出
编码（默认 `utf-8`，Windows 控制台下也不会因编码报错）。

## 子命令 1：snapshot / verify（文件指纹）

`snapshot` 对给定的一组文件、目录或通配模式计算 sha256，写出一份
JSON 清单，含相对路径、大小、sha256、行尾类型统计（CRLF/LF/CR
各多少）、是否有 BOM、逐行 sha256、记录时间，可用 `--label` 加说明。
支持 `--exclude` 排除（可多次使用）。

`verify` 拿清单复核当前文件，逐个报告：一致 / 内容变了 / 缺失 /
多出。对「内容变了」再细分并明确提示：只差行尾（CRLF/LF）、
只差 BOM、只差文件末尾空行或空白、仅行尾与 BOM 等的组合差异、
其他实质变化；实质变化给出首个差异所在的行号（只报行号，
不输出文件内容）。

例子：

```bat
rem 例 1：给批准版文档目录生成清单
python -m precheck snapshot docs -o approved.json --label "v1 批准版"

rem 例 2：复核当前文件是否和批准版一致
python -m precheck verify approved.json

rem 例 3：用通配只收 md 文件并排除草稿目录，结果给脚本用
python -m precheck snapshot "docs\*.md" --exclude drafts/ -o snap.json --json
```

### 审批块：只比「正文」

被批准的 Markdown 文档末尾常有一个提交工具每轮改写的审批块
（以 ```` ```stage-approvals ```` 开头、以单独一行 ```` ``` ```` 结束的围栏代码块），
整文件哈希因此每轮必变。`snapshot` 加 `--strip-fenced NAME`（可多次）
后，清单会额外记录「正文」指纹：去掉每个 info 字符串等于 NAME 的
围栏块（含开头和结束两行）后剩下的字节，其余字节原样保留，
不归一行尾、不去 BOM。清单同时记录整文件 sha256、正文 sha256、
剥离了几个块和各块起止行号；规则写在清单的 `strip_fenced` 字段里，
`verify` 自动照办，不用再传参。

- 整文件变了、正文一致：报「只差审批块」，**退出码 0**，但会逐个列出；
  审批块被整个删掉也算这一类；
- 正文变了：照旧细分为只差行尾 / BOM / 末尾空白 / 实质变化，
  首个差异行号按**原文件**计；
- 审批块开头没有闭合：`snapshot` 和 `verify` 都报错退出 2，并给出开头行号；
- 其他语言的围栏块（如 ```` ```java ````）不受影响，嵌在别的围栏块里的
  同名开头行也不会被当作审批块。

```bat
rem 例 4：批准版文档带审批块
python -m precheck snapshot docs\design.md -o approved.json --strip-fenced stage-approvals
python -m precheck verify approved.json
```

清单里记录的是快照时各来源目录的绝对路径；整棵目录搬走后，
用 `verify 清单 --base 新目录` 可按相对路径在新位置复核
（多出检查只对目录型来源生效）。

## 子命令 2：junit-tally（用例对账）

输入一个或多个 JUnit XML 报告文件或目录（目录递归找 `*.xml`），
支持 `testsuite` 与 `testsuites` 两种根元素（含嵌套）。按文件、
按测试类汇总 tests / failures / errors / skipped 以及总计。

- 同名测试类出现在不同模块（不同报告文件）时分别计数并提示；
- 同一个报告被重复传入（同文件两次、复制件、目录与文件重叠）
  按内容去重并提示；
- 空目录或合计零用例时明确报「零用例」，退出码 1，不能当通过；
- `--expect-tests N`、`--expect-failures N`、`--expect-errors N`、
  `--expect-skipped N` 与声称值比对，不一致退出 1 并指出差多少；
- XML 损坏或根元素不是 JUnit 报告时报错退出 2，不打印堆栈；
- `testsuite` 上的 tests/failures/errors/skipped 属性与实际 `testcase`
  统计对不上时给警告（按实际统计计数，不改退出码）；
- 含 `flakyFailure`、`flakyError`、`rerunFailure`、`rerunError` 的用例
  单独计数（`--json` 里为 `flaky_total`），并警告「重跑后才通过」，
  不改退出码。

例子：

```bat
rem 例 1：统计一个 surefire 报告目录
python -m precheck junit-tally target\surefire-reports

rem 例 2：和报告里声称的用例数对账
python -m precheck junit-tally reports-a reports-b --expect-tests 120 --expect-failures 0

rem 例 3：多个报告文件，结果输出 JSON
python -m precheck junit-tally TEST-a.xml TEST-b.xml --json
```

## 子命令 3：diffstat（改动量）

输入一个或多个 git 工作区路径，输出固定格式表格和 JSON：每个仓库的
已修改、已新增、已删除、未跟踪文件清单，以及受跟踪文件相对基线
（`--base`，默认 `HEAD`）的增删行数；未跟踪文件单独统计行数，
二进制未跟踪文件只标明是二进制、不数行数。可用 `--include` /
`--exclude` 过滤（可多次使用）。

只调用 git 的只读命令（`status`、`rev-parse --verify`、
`diff --numstat`），绝不执行任何会改动仓库的命令；所有 git 调用都带
`GIT_OPTIONAL_LOCKS=0`，`status` 不会顺手写回 `.git/index`。
非 git 目录报错退出 2。

`--base` 防注入：以 `-` 开头的值直接拒绝（退出 2）；其余值先用
`rev-parse --verify` 解析成完整提交号，交给 `diff` 的只是这个提交号，
因此用户输入永远不会被 git 当作选项（不依赖 `--end-of-options`）。

只差行尾：已修改文件若在 `--ignore-cr-at-eol` 下增删为 0，表格里
标「（只差行尾）」，`--json` 里为 `"eol_only": true`。本机 git 不支持
该选项时不做标注并给出提示。

### 声明清单对账（--declared）

`--declared FILE`：FILE 是 UTF-8 文本，每行一个相对仓库根的路径，
支持 fnmatch 风格通配（`*`、`?` 不跨目录，`**` 跨目录，以 `/` 结尾
表示整个目录），`#` 开头为注释，空行忽略，`/` 与 `\` 都接受。

- 已修改、已新增、已删除、重命名（新旧路径任一命中即算登记）、
  未跟踪文件中，不在声明里的列为「未声明改动」，**退出 1**；
- 声明了但实际没改的列为「声明未改动」，只提示，不影响退出码；
- 与 `--include` / `--exclude` 一起用时先过滤再对账；
- `--json` 里每个仓库的 `declared.undeclared` 与
  `declared.declared_unchanged` 就是这两张清单。

### Java 类名（--java-classes）

把已修改、已新增、未跟踪文件中位于 `src/main/java/` 下（前面可带模块
目录）的 `.java` 路径转成全限定类名，去重排序后逗号分隔输出一行，
可直接填进 PIT 的 `-DtargetClasses`。已删除文件、`package-info.java`、
`module-info.java` 不输出；不在该路径约定下的文件一律不猜。

例子：

```bat
rem 例 1：看当前工作区改了多少
python -m precheck diffstat .

rem 例 2：相对某个提交统计，并排除构建产物
python -m precheck diffstat . --base v1.0-tag --exclude target/

rem 例 3：一次统计两个仓库，输出 JSON
python -m precheck diffstat repo-a repo-b --json

rem 例 4：收工对账：排除构建产物后核对是否都在登记里，并给出 PIT 类名
python -m precheck diffstat . --exclude target/ --declared round-07-files.txt --java-classes
```

声明清单示例（虚构）：

```text
# 第 7 轮登记
src/main/java/com/example/demo/OrderService.java
src/main/java/com/example/demo/**/*.java
docs\design.md
```

## 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常：verify 全部一致 / junit-tally 对账通过 / diffstat 统计完成 |
| 1 | 检查不通过：verify 有变了/缺失/多出（「只差审批块」不算）；junit-tally 对不上声称值或零用例；diffstat `--declared` 有未声明改动 |
| 2 | 参数错误、路径不存在、读文件或清单/XML 解析失败、非 git 目录等预期内错误（不打印堆栈） |
## 测试

共 85 个 unittest 用例（原有 50 个 + 本次新增 35 个），覆盖：只改行尾、
只多末尾空行、BOM 增删、中文路径与空格路径、GBK 字节文件名、二进制
文件、同名测试类、重复报告去重、空报告目录、testsuites 嵌套、损坏 XML、
非 git 目录、未跟踪二进制文件；以及审批块剥离（只改审批块、正文与审批块
同时改、两个块、未闭合、CRLF、java 围栏、整块删除）、`--base` 注入被拒
且不写任何文件、声明清单对账（通配、重命名、反斜杠、未跟踪、先排除再
对账）、只差行尾标注、Java 类名、junit 属性不符与 flaky 计数等场景。在仓库根目录执行一条命令跑完：

```bat
python -m unittest discover -s precheck/tests -v
```

（diffstat 的用例需要本机有 git，没有 git 时自动跳过那部分。）

## 已知限制

- 清单按「来源目录的绝对路径 + 相对路径」定位文件；整树搬移后
  需用 `verify --base` 复核，且 `--base` 模式下多出检查只覆盖
  目录型来源，文件/通配型来源不做多出断言。
- 清单内含逐行 sha256，行数很多的大文件会让清单体积明显变大；
  本工具面向文档与源码级文件，不适合给大型二进制产物建清单。
- junit-tally 以 testsuite 的 name 作为测试类名；同一报告文件内
  同名 testsuite 会合并计数。没有 testcase 子元素的报告退回用
  testsuite 属性上的计数，属性缺失按 0 计。
- diffstat 的「已新增」指已暂存的新文件；未暂存的新文件算未跟踪。
  重命名按「已修改」计并标注原路径。未跟踪文件行数按本工具读取
  文件内容统计，二进制判定为前 8192 字节含 NUL 字节。
- 文件名为非法 UTF-8 字节序列（如 POSIX 上的 GBK 字节文件名）时，
  清单可正常生成与读回，但终端与 `--json` 输出中的该文件名可能
  显示为替换字符。
- 审批块识别按 CommonMark 围栏规则的子集：开头行最多 3 个空格缩进，
  ``` 或 ~~~ 至少 3 个，info 字符串整体等于 NAME；结束行用同种字符、
  长度不少于开头。缩进代码块、HTML 注释里的围栏不做特殊处理。
- `--declared` 的通配区分大小写；Windows 上大小写不同的路径会被当作
  未声明。
- 「只差行尾」依赖 git 的 `--ignore-cr-at-eol`（git 2.16+）；只能识别
  CR 差异，不识别行内其他空白。
- 本工具在 Linux + Python 3.12 上开发测试，语法按 Python 3.8
  兼容编写；Windows 特有路径形态（盘符、反斜杠）在代码中按
  os.sep/altsep 统一处理，但未在真实 Windows 机器上跑过测试。
