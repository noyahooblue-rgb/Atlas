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

`--strip-fenced NAME`（可多次使用）声明「审批块」：文档里 info
字符串等于 NAME 的围栏代码块（如 ` ```stage-approvals `）每次提交
都会被改写，清单会同时记录整文件 sha256 和剥离这些块之后的
「正文」sha256，以及剥离了几个块、各块起止行号。规则写进清单，
`verify` 自动执行，不用再传参。正文的定义是去掉命中块（从开头
围栏行到结束围栏行，含这两行）后剩下的字节，其余字节原样保留，
不做行尾归一化、不去 BOM。待剥离块没有闭合时，snapshot 和
verify 都报错退出 2 并指出起始行号。

`verify` 拿清单复核当前文件，逐个报告：一致 / 内容变了 / 缺失 /
多出。对「内容变了」再细分并明确提示：只差行尾（CRLF/LF）、
只差 BOM、只差文件末尾空行或空白、仅行尾与 BOM 等的组合差异、
其他实质变化；实质变化给出首个差异所在的行号（只报行号，
不输出文件内容）。清单带正文指纹时，整文件变了但正文一致的
文件单独归类为「只差审批块」：不算检查不通过（退出码仍为 0），
但会在输出里明确列出；正文变了仍按上面的细分分类，行号按
原文件计（审批块占的行也算）。

例子：

```bat
rem 例 1：给批准版文档目录生成清单
python -m precheck snapshot docs -o approved.json --label "v1 批准版"

rem 例 2：复核当前文件是否和批准版一致
python -m precheck verify approved.json

rem 例 3：用通配只收 md 文件并排除草稿目录，结果给脚本用
python -m precheck snapshot "docs\*.md" --exclude drafts/ -o snap.json --json

rem 例 4：文档末尾有每次提交都会改写的审批块，剥离后再比对正文
python -m precheck snapshot docs -o approved.json --strip-fenced stage-approvals
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
- testsuite 上的 tests / failures / errors / skipped 属性与实际
  testcase 统计对不上时给警告（不改退出码）；
- 出现 flakyFailure / flakyError / rerunFailure / rerunError 的
  用例单独计数，并警告这些用例是「重跑后才通过」；
- 空目录或合计零用例时明确报「零用例」，退出码 1，不能当通过；
- `--expect-tests N`、`--expect-failures N`、`--expect-errors N`、
  `--expect-skipped N` 与声称值比对，不一致退出 1 并指出差多少；
- XML 损坏或根元素不是 JUnit 报告时报错退出 2，不打印堆栈。

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
二进制未跟踪文件只标明是二进制、不数行数。只差行尾（CRLF/LF）
的已修改文件会标注「只差行尾」。可用 `--include` / `--exclude`
过滤（可多次使用）。

只调用 git 的只读命令（`status`、`diff --numstat`），绝不执行
任何会改动仓库的命令；所有 git 调用都带 `GIT_OPTIONAL_LOCKS=0`，
避免 status 刷新索引时写 `.git/index`。非 git 目录报错退出 2。
`--base` 必须是单个版本引用（如提交、标签、分支名），不能以
`-` 开头（以 `-` 开头会直接报错退出 2，防止被 git 当成选项
解析）；调用 diff 时还会给基线拼上 `^{tree}` 后缀，双重保证
基线不会被当成选项。

`--declared FILE` 做声明清单对账：FILE 是 UTF-8 文本，每行一个
相对仓库根目录的路径，支持 fnmatch 风格通配（`*` 不跨目录、
`**` 跨目录），`#` 开头为注释，空行忽略，路径分隔符 `/` 和
`\` 都接受。实际改动（先经过 `--include` / `--exclude` 过滤）
里，已修改、已新增、已删除、重命名（新旧路径任一命中即算已
声明）、未跟踪文件凡不在声明里的，列为「未声明改动」，退出 1；
声明了但实际没改的列为「声明未改动」，只提示，不影响退出码。
`--json` 输出里包含这两张清单。声明文件不存在或读不了时
退出 2。

`--java-classes` 把改动和未跟踪文件里路径含 `src/main/java/`
片段的 `.java` 路径转成全限定类名，逗号分隔输出一行，方便直接
填进 PIT 的 `-DtargetClasses`。片段可位于路径任意位置：仓库根
目录下（`src/main/java/...`）或任意模块目录下
（`<模块>/src/main/java/...`，模块可多层），多模块工程同样适用；
同一路径出现多次片段时以最后一次为准；`src/test/java/` 不算。
汇总行之后还会按模块逐行再输出一遍（根目录文件的模块记为 `.`），
格式如 `模块 mod-a：com.a.A,com.a.B`；`--json` 里除原有的
`java_classes` 汇总清单外，另有 `java_classes_by_module` 按模块
分组。已删除的 `.java` 文件不输出。

例子：

```bat
rem 例 1：看当前工作区改了多少
python -m precheck diffstat .

rem 例 2：相对某个提交统计，并排除构建产物
python -m precheck diffstat . --base v1.0-tag --exclude target/

rem 例 3：一次统计两个仓库，输出 JSON
python -m precheck diffstat repo-a repo-b --json

rem 例 4：和本轮开工时登记的声明清单对账
python -m precheck diffstat . --declared declared.txt --exclude target/

rem 例 5：输出本轮改动涉及的 Java 类（给 PIT 用）
python -m precheck diffstat . --java-classes
```

## 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常：verify 全部一致（「只差审批块」也算通过）/ junit-tally 对账通过 / diffstat 统计完成且无未声明改动 |
| 1 | 检查不通过：verify 有变了/缺失/多出；junit-tally 对不上声称值或零用例；diffstat 有未声明改动 |
| 2 | 参数错误、路径不存在、读文件或清单/XML 解析失败、非 git 目录、声明文件不存在、审批块未闭合、基线以 `-` 开头等预期内错误（不打印堆栈） |

## 测试

共 87 个 unittest 用例，覆盖：只改行尾、只多末尾空行、BOM 增删、
中文路径与空格路径、GBK 字节文件名、二进制文件、同名测试类、
重复报告去重、空报告目录、testsuites 嵌套、损坏 XML、非 git
目录、未跟踪二进制文件等场景，以及本次返工新增的：`--base`
参数注入被拒且不写出任何文件、审批块只改/正文同改/两个审批块/
未闭合/CRLF/其他语言围栏块/整块删除、声明清单对账（未声明的
已修改与未跟踪文件、通配、重命名、反斜杠路径、声明文件缺失）、
testsuite 属性对不上警告、flaky 用例计数、只差行尾标注、
`--java-classes` 转换（含多模块路径：根模块、一层与两层模块、
按模块分组输出、已删除类不输出）。在仓库根目录执行一条命令跑完：

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
- diffstat 的 `--base` 只接受单个版本引用，不支持 `A..B` 这类
  范围写法（内部会给基线拼 `^{tree}` 后缀）。「只差行尾」标注
  依赖 `git diff --ignore-cr-at-eol`，极旧的 git 不支持该选项
  时自动跳过标注，其余统计不受影响。
- 正文指纹只处理反引号围栏块（` ``` `），不处理 `~~~` 围栏；
  info 字符串须与 `--strip-fenced` 给的名字完全相等才剥离。
  声明清单里无通配符的条目按「精确路径或目录前缀」命中：
  写一个目录名即声明其下全部文件。
- 文件名为非法 UTF-8 字节序列（如 POSIX 上的 GBK 字节文件名）时，
  清单可正常生成与读回，但终端与 `--json` 输出中的该文件名可能
  显示为替换字符。
- 本工具在 Linux + Python 3.12 上开发测试，语法按 Python 3.8
  兼容编写；Windows 特有路径形态（盘符、反斜杠）在代码中按
  os.sep/altsep 统一处理，但未在真实 Windows 机器上跑过测试。
