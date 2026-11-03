"""命令行入口：python -m specpeek check|html|diff ..."""

import argparse
import sys
from pathlib import Path

from .config import load_config
from .diff import compare, format_diff, generate_diff
from .htmlgen import generate
from .index import check, format_check


def _require_dir(path):
    if not Path(path).is_dir():
        print("目录不存在：%s" % path, file=sys.stderr)
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(prog="specpeek", description="Markdown 规格人工审查辅助工具")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "html"):
        p = sub.add_parser(name)
        p.add_argument("directory")
        p.add_argument("--config", default=None)
    sub.choices["html"].add_argument("-o", "--output", required=True)
    p_diff = sub.add_parser("diff")
    p_diff.add_argument("old_dir")
    p_diff.add_argument("new_dir")
    p_diff.add_argument("--config", default=None)
    p_diff.add_argument("-o", "--output", default=None)
    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "diff":
        if not (_require_dir(args.old_dir) and _require_dir(args.new_dir)):
            return 2
        result = compare(args.old_dir, args.new_dir, config)
        print(format_diff(result))
        if args.output:
            html_text = generate_diff(args.old_dir, args.new_dir, config, result)
            Path(args.output).write_text(html_text, encoding="utf-8")
            print("已生成：%s" % args.output)
        return 0

    directory = Path(args.directory)
    if not directory.is_dir():
        print("目录不存在：%s" % directory, file=sys.stderr)
        return 2
    result = check(directory, config)
    if args.command == "check":
        print(format_check(result, config))
        has_problem = bool(
            result["undefined"] or result["unused"]
            or result["coverage_gaps"] or result["duplicates"]
        )
        return 1 if has_problem else 0
    html_text = generate(directory, config, result)
    Path(args.output).write_text(html_text, encoding="utf-8")
    print("已生成：%s" % args.output)
    print(format_check(result, config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
