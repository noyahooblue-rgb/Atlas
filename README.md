# specpeek

Markdown 规格人工审查辅助工具：扫描目录下的 `.md` 文件，为编号
（业务规则、验收标准、实现任务、外部规则、待确认项、发布前核查项）
建立定义与引用索引，做追溯检查，并生成单文件审查页。

只用 Python 标准库，Python 3.8+，Windows 与 Linux 都能运行；
输入文件按 UTF-8 读取（兼容 BOM），只读，不修改输入文件；
生成的 HTML 不引用任何外部 CDN、字体与脚本，断网可用。

## 安装运行

无需安装第三方包，克隆仓库后在仓库根目录运行：

```bash
python -m specpeek check examples
python -m specpeek html examples -o review.html
python -m unittest
```

`check` 有异常时退出码为非 0，可接入 CI；无异常时退出码为 0。

## 编号配置

默认配置在 `specpeek/default_config.json`，覆盖 R、AC、T、BDR、OI、PC
六类编号。自定义时传 `--config 配置文件路径`：

```bash
python -m specpeek check examples --config my-config.json
```

配置格式：`categories` 每项含 `id`、`name`、`pattern`（正则，不带捕获组）；
`coverage` 每项声明一对覆盖关系（`from` 源类别、`to` 目标类别、`label` 缺口文案）。
定义处识别方式：标题行、列表项开头、行首编号优先，表格首列次之
（追溯总表首列不会抢走正式定义）。

覆盖语义：某一行同时出现源编号与目标类别编号时，视作一次追溯链接；
源编号若从未出现在任何这样的行里，即为覆盖缺口。

## 审查页

- 正文中每个编号显示为「编号（短名）」，鼠标悬停弹出定义处原文和位置，点击跳到定义清单；
- 顶部显示追溯检查的异常汇总；
- 每个定义可打勾标记「已审」并写审查意见，状态存在浏览器 localStorage（读写包在 try/catch 里），点「导出审查意见为 Markdown」可下载意见文件。

## 已知限制与未完成项

- 差异审查（两个版本之间）是下一期功能：接口已预留在 `specpeek/diff.py`，本期调用会报未实现；
- Markdown 渲染只覆盖标题、表格、列表、代码块与段落，不追求完整 CommonMark；
- 覆盖关系按「同行共现」判定，不做语义理解；
- 代码块内的编号不计入索引与引用。
