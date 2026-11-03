#!/usr/bin/env python3
"""生成规模测试用的虚构规格（社区食堂订餐系统，纯属编造）。

用法：

    python tools/gen_scale_spec.py [输出目录] [--bench]

在输出目录下生成 old/ 与 new/ 两个版本：约 700 行、60 条规则、
20 条验收标准、15 个任务，交叉引用齐全（check 应无异常）；new
相对 old 有少量新增、删除与内容改变，供 diff 演示与计时。
--bench 会在进程内对 old 跑 check、html，对 old→new 跑 diff
并打印各自耗时。内容由固定模板生成，无随机因素，结果可复现。

注意：正文里提及编号时不放在行首，避免被识别成定义行。
"""

import shutil
import sys
import time
from pathlib import Path

CHAPTERS = ["订餐", "支付", "取餐", "退单", "评价", "通知"]
RULE_VERBS = [
    "校验用户提交的信息完整性", "记录本次操作的时间与结果", "在界面上给出明确的提示",
    "限制同一用户的重复提交", "保存处理结果供后续查询", "在异常时回滚到操作前状态",
    "按顺序处理排队中的请求", "对超时的请求给出失败提示", "汇总当日数据生成日报",
    "在状态变化时通知相关人员",
]
TASK_NAMES = [
    "搭建订餐入口页面", "实现菜单维护接口", "实现下单校验逻辑", "接通支付回调处理",
    "实现取餐码生成", "实现退单审核流程", "实现评价提交与展示", "实现通知推送通道",
    "实现日报汇总任务", "实现权限校验中间件", "实现操作日志记录", "实现异常告警",
    "实现数据备份脚本", "实现接口限流", "实现灰度开关",
]


def rule_number(flat_index):
    """规则的扁平序号 1..60 换成 R<章>.<条>。"""
    return "R%d.%d" % ((flat_index - 1) // 10 + 1, (flat_index - 1) % 10 + 1)


def ac_rules(ai):
    """第 ai 条验收标准关联的 3 条规则（60 条恰好被 20 条 AC 全覆盖）。"""
    return [rule_number(3 * ai - 2), rule_number(3 * ai - 1), rule_number(3 * ai)]


def build_spec(changed=False):
    """返回 {文件名: 文本}。changed=True 生成新版本（含差异）。"""
    requirement = []
    requirement.append("# 社区食堂订餐系统 · 需求规格（虚构样例）")
    requirement.append("")
    requirement.append("本文件纯属虚构，仅用于 specpeek 规模测试。系统面向社区食堂的")
    requirement.append("订餐、支付、取餐、退单、评价与通知六个环节，规则按章节编号。")
    requirement.append("")
    for ci, chapter in enumerate(CHAPTERS, start=1):
        requirement.append("## %s规则" % chapter)
        requirement.append("")
        requirement.append("| 编号 | 规则内容 | 备注 |")
        requirement.append("| --- | --- | --- |")
        for ri in range(1, 11):
            number = "R%d.%d" % (ci, ri)
            if changed and number == "R6.10":
                continue  # 新版本删除这条规则
            text = "系统应当%s，适用于%s环节第 %d 项约定" % (
                RULE_VERBS[ri - 1], chapter, ri)
            if changed and number == "R2.3":
                text = "系统应当在提交前校验用户的信息完整性，缺失时逐项列出，适用于支付环节第 3 项约定"
            requirement.append("| %s | %s。 | 章节 %s |" % (number, text, chapter))
        requirement.append("")
    requirement.append("## 规则详解")
    requirement.append("")
    for ci, chapter in enumerate(CHAPTERS, start=1):
        for ri in range(1, 11):
            number = "R%d.%d" % (ci, ri)
            if changed and number == "R6.10":
                continue
            requirement.append(
                "规则 %s 的说明：本条细化%s环节的处理约定。执行时先确认前置条件，"
                "再按规则内容处理，最后把结果写入记录，供后续查询与审计。" % (number, chapter))
            requirement.append("")
            requirement.append(
                "边界情况：输入为空、重复提交与处理超时三种情形都要给出可理解的"
                "提示；其中与 %s 相邻的约定同时生效时，以先发生的处理结果为准。" % number)
            requirement.append("")
    if changed:
        requirement.append("## 补充规则")
        requirement.append("")
        requirement.append("| 编号 | 规则内容 | 备注 |")
        requirement.append("| --- | --- | --- |")
        requirement.append("| R7.1 | 系统应当支持代订功能，由订餐人填写就餐人信息。 | 新版本新增 |")
        requirement.append("")
    requirement.append("## 验收标准")
    requirement.append("")
    requirement.append("| 编号 | 验收内容 | 关联规则 |")
    requirement.append("| --- | --- | --- |")
    for ai in range(1, 21):
        text = "按约定完成一次%s流程，系统行为与规则一致" % CHAPTERS[(ai - 1) % 6]
        if changed and ai == 5:
            text = "按约定完成一次支付流程，并逐项核对金额与优惠，系统行为与规则一致"
        requirement.append("| AC-%02d | %s。 | %s |" % (ai, text, "、".join(ac_rules(ai))))
    requirement.append("")
    requirement.append("## 验收说明")
    requirement.append("")
    for ai in range(1, 21):
        requirement.append(
            "验收 AC-%02d 的准备：提前备好测试账号与样例数据，按关联规则逐项"
            "核对系统行为，记录通过与否及截图编号，评审时统一确认。" % ai)
        requirement.append("")
    requirement.append("## 变更记录")
    requirement.append("")
    requirement.append("| 版本 | 说明 |")
    requirement.append("| --- | --- |")
    requirement.append("| v1 | 初稿，规则与验收标准一次成型。 |")
    requirement.append("| v2 | 评审后补齐边界情况说明。 |")
    requirement.append("")

    design = []
    design.append("# 社区食堂订餐系统 · 设计规格（虚构样例）")
    design.append("")
    design.append("本文件纯属虚构。追溯总表把验收标准、规则与任务对应起来，")
    design.append("任务清单给出实现顺序，验收时逐条核对总表中的验证方式。")
    design.append("")
    design.append("## 追溯总表")
    design.append("")
    design.append("| 验收标准 | 规则 | 方案 | 任务 | 验证 |")
    design.append("| --- | --- | --- | --- | --- |")
    for ai in range(1, 21):
        t = "T%d" % ((ai - 1) % 15 + 1)
        design.append("| AC-%02d | %s | 按规则逐项校验并记录结果 | %s | 手工用例与单元测试 |"
                      % (ai, "、".join(ac_rules(ai)), t))
    design.append("")
    design.append("## 任务清单")
    design.append("")
    for ti, name in enumerate(TASK_NAMES, start=1):
        text = name
        if changed and ti == 15:
            text = "实现灰度开关与回滚预案"
        design.append("- T%d %s。" % (ti, text))
    design.append("")
    design.append("## 实施说明")
    design.append("")
    for ti, name in enumerate(TASK_NAMES, start=1):
        design.append(
            "任务 T%d 的实施要点：先完成%s的接口约定，再编写实现与自测用例，"
            "最后在评审时对照追溯总表确认覆盖范围。" % (ti, name))
        design.append("")
        design.append(
            "评审要点：遇到阻塞时记录原因并在站会上同步；交付前逐项核对"
            "验收标准中的关联规则，避免遗漏边界情况。")
        design.append("")
    design.append("## 评审记录")
    design.append("")
    for ai in range(1, 21):
        design.append(
            "记录 %02d：评审确认 AC-%02d 与 T%d 的对应关系无误，"
            "遗留问题转入下一轮迭代处理。" % (ai, ai, (ai - 1) % 15 + 1))
        design.append("")

    appendix = []
    appendix.append("# 社区食堂订餐系统 · 常见问题（虚构样例）")
    appendix.append("")
    appendix.append("本附录纯属虚构，汇总试运行期间收集的问题与答复，供新人查阅。")
    appendix.append("")
    for qi in range(1, 39):
        r = rule_number((qi - 1) % 60 + 1)
        ac = "AC-%02d" % ((qi - 1) % 20 + 1)
        t = "T%d" % ((qi - 1) % 15 + 1)
        appendix.append("问 %d：规则 %s 与验收 %s 的关系是什么？" % (qi, r, ac))
        appendix.append("答：验收 %s 直接覆盖规则 %s，实现由任务 %s 负责，细节见设计规格的追溯总表。"
                        % (ac, r, t))
        appendix.append("备注：本条在试运行第 %d 周整理，内容如有变化以需求规格为准。" % ((qi - 1) % 8 + 1))
        appendix.append("")
    return {"requirement.md": "\n".join(requirement) + "\n",
            "design.md": "\n".join(design) + "\n",
            "appendix.md": "\n".join(appendix) + "\n"}


def write_spec(base, changed):
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    total_lines = 0
    for name, text in build_spec(changed).items():
        (base / name).write_text(text, encoding="utf-8")
        total_lines += text.count("\n")
    return total_lines


def bench(old, new):
    from specpeek.config import load_config
    from specpeek.diff import compare
    from specpeek.htmlgen import generate
    from specpeek.index import check

    config = load_config()
    start = time.perf_counter()
    result = check(old, config)
    t_check = time.perf_counter() - start
    start = time.perf_counter()
    generate(old, config, result)
    t_html = time.perf_counter() - start
    start = time.perf_counter()
    compare(old, new, config)
    t_diff = time.perf_counter() - start
    print("check: %.3f 秒" % t_check)
    print("html: %.3f 秒" % t_html)
    print("diff: %.3f 秒" % t_diff)


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    out = Path(args[0]) if args else Path("scale-fixture")
    old_lines = write_spec(out / "old", changed=False)
    new_lines = write_spec(out / "new", changed=True)
    print("已生成：%s/old（%d 行）、%s/new（%d 行）" % (out, old_lines, out, new_lines))
    if "--bench" in argv:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        bench(out / "old", out / "new")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
