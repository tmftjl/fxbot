"""把历史会话表名迁移为当前名称；默认处理 data/db/fxbot.db。"""

from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path

from fxbot.plugins.memes.migrate import migrate_session_tables


def main() -> None:
    """运行迁移并输出 JSON 结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/db/fxbot.db"),
        help="SQLite 数据库路径",
    )
    parser.add_argument("--no-backup", action="store_true", help="不生成备份文件")
    args = parser.parse_args()
    result = migrate_session_tables(args.database, backup=not args.no_backup)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
