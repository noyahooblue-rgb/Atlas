"""命令行入口：python -m precheck <子命令> ...

退出码约定（三个子命令一致）：
  0  正常（verify 全部一致 / tally 对账通过 / diffstat 统计完成）
  1  检查不通过（verify 有差异 / tally 对不上或零用例）
  2  参数错误、读文件错误等预期内失败（不打印堆栈）
"""

import argparse
import sys

from . import __version__
from .errors import PrecheckError
from . import diffstat as diffstat_mod
from . import fingerprint
from . import junit_tally


def _configure_output(encoding):
    """把 stdout/stderr 切到指定编码且永不因编码抛错（Windows 控制台）。"""
    if not encoding:
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding=encoding, errors="replace")
            except (ValueError, OSError):
                pass


def _out(text=""):
    sys.stdout.write(text + "\n")


def _err(text=""):
    sys.stderr.write(text + "\n")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        help="以 JSON 输出结果（给其他脚本用）")
    common.add_argument("--encoding", default="utf-8",
                        help="输出编码，默认 utf-8")

    parser = argparse.ArgumentParser(
        prog="precheck",
        description="提交前检查工具包：文件指纹、用例对账、改动量统计")
    parser.add_argument("--version", action="version",
                        version="precheck %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_snap = sub.add_parser("snapshot", parents=[common],
                            help="对一组文件/目录生成 sha256 指纹清单")
    p_snap.add_argument("paths", nargs="+",
                        help="文件、目录或通配模式（可多个）")
    p_snap.add_argument("-o", "--output", default=None,
                        help="清单输出路径（默认 precheck-snapshot.json）")
    p_snap.add_argument("--label", default=None, help="清单说明文字")
    p_snap.add_argument("--exclude", action="append", default=[],
                        help="排除模式，可多次使用，如 --exclude target/")

    p_verify = sub.add_parser("verify", parents=[common],
                              help="按清单复核当前文件")
    p_verify.add_argument("manifest", help="snapshot 生成的清单文件")
    p_verify.add_argument("--base", default=None,
                          help="把清单里的文件按相对路径在此目录下复核")

    p_tally = sub.add_parser("junit-tally", parents=[common],
                             help="JUnit XML 报告用例对账")
    p_tally.add_argument("paths", nargs="+",
                         help="报告文件或目录（可多个）")
    p_tally.add_argument("--expect-tests", type=int, default=None)
    p_tally.add_argument("--expect-failures", type=int, default=None)
    p_tally.add_argument("--expect-errors", type=int, default=None)
    p_tally.add_argument("--expect-skipped", type=int, default=None)

    p_diff = sub.add_parser("diffstat", parents=[common],
                            help="git 工作区改动量统计（只读）")
    p_diff.add_argument("paths", nargs="+", help="git 工作区路径（可多个）")
    p_diff.add_argument("--base", default="HEAD",
                        help="行数统计基线，默认 HEAD")
    p_diff.add_argument("--include", action="append", default=[],
                        help="只统计命中此模式的文件，可多次使用")
    p_diff.add_argument("--exclude", action="append", default=[],
                        help="排除命中此模式的文件，可多次使用")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_output(getattr(args, "encoding", "utf-8"))
    try:
        if args.command == "snapshot":
            return fingerprint.run_snapshot(
                args.paths, args.output, args.label, args.exclude,
                args.json, _out)
        if args.command == "verify":
            return fingerprint.run_verify(
                args.manifest, args.base, args.json, _out)
        if args.command == "junit-tally":
            expects = {"tests": args.expect_tests,
                       "failures": args.expect_failures,
                       "errors": args.expect_errors,
                       "skipped": args.expect_skipped}
            return junit_tally.run_tally(args.paths, expects,
                                         args.json, _out)
        if args.command == "diffstat":
            return diffstat_mod.run_diffstat(
                args.paths, args.base, args.include, args.exclude,
                args.json, _out, _err)
    except PrecheckError as exc:
        _err("错误：%s" % exc)
        return 2
    except BrokenPipeError:
        return 2
    except Exception as exc:  # 兜底：命令行工具不向用户打印堆栈
        _err("错误：未预期异常：%s" % exc)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
