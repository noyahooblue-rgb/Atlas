"""命令行入口：python -m specpeek check|html <目录>"""

import argparse
import sys
from pathlib import Path

from .config import load_config
from .htmlgen import generate
from .index import check, format_check


def main(argv=None):
    parser = argparse.ArgumentParser(prog="specpeek", description="Markdown 规格人工审查辅助工具")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "html"):
        p = sub.add_parser(name)
        p.add_argument("directory")
        p.add_argument("--config", default=None)
    sub.choices["html"].add_argument("-o", "--output", required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    directory = Path(args.directory)
    if not directory.is_dir():
        print("目录不存在：%s" % directory, file=sys.stderr)
        return 2
    result = check(directory, config)
    if args.command == "check":
        print(format_check(result, config))
        has_problem = bool(result["undefined"] or result["unused"] or result["coverage_gaps"])
        return 1 if has_problem else 0
    html_text = generate(directory, config, result)
    Path(args.output).write_text(html_text, encoding="utf-8")
    print("已生成：%s" % args.output)
    print(format_check(result, config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
