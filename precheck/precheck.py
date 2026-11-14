#!/usr/bin/env python3
"""脚本式入口：在 precheck 目录内执行 `python precheck.py ...` 等价于
在仓库根目录执行 `python -m precheck ...`。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from precheck.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
