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
二进制未跟踪文件只标明是二进制、不数行数。可用 `--include` /
`--exclude` 过滤（可多次使用）。

只调用 git 的只读命令（`status`、`diff --numstat`），绝不执行
任何会改动仓库的命令。非 git 目录报错退出 2。

例子：

```bat
rem 例 1：看当前工作区改了多少
python -m precheck diffstat .

rem 例 2：相对某个提交统计，并排除构建产物
python -m precheck diffstat . --base v1.0-tag --exclude target/

rem 例 3：一次统计两个仓库，输出 JSON
python -m precheck diffstat repo-a repo-b --json
```

## 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常：verify 全部一致 / junit-tally 对账通过 / diffstat 统计完成 |
| 1 | 检查不通过：verify 有变了/缺失/多出；junit-tally 对不上声称值或零用例 |
| 2 | 参数错误、路径不存在、读文件或清单/XML 解析失败、非 git 目录等预期内错误（不打印堆栈） |

## 测试

共 50 个 unittest 用例，覆盖：只改行尾、只多末尾空行、BOM 增删、
中文路径与空格路径、GBK 字节文件名、二进制文件、同名测试类、
重复报告去重、空报告目录、testsuites 嵌套、损坏 XML、非 git
目录、未跟踪二进制文件等场景。在仓库根目录执行一条命令跑完：

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
- 本工具在 Linux + Python 3.12 上开发测试，语法按 Python 3.8
  兼容编写；Windows 特有路径形态（盘符、反斜杠）在代码中按
  os.sep/altsep 统一处理，但未在真实 Windows 机器上跑过测试。
