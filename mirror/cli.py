from __future__ import annotations

import argparse
import json
from pathlib import Path

from .store import MirrorStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="管理 Phase 1 虚拟镜像账本的源仓位基准（不会连接币安交易接口）"
    )
    parser.add_argument(
        "--database",
        default="./data/monitor.db",
        help="与服务端相同的 SQLite 数据库路径",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    flat = commands.add_parser(
        "assume-source-flat",
        help="仅在你能独立确认熬鹰当前全部仓位为空时使用",
    )
    flat.add_argument(
        "--confirm-source-is-flat",
        action="store_true",
        help="明确确认源带单员当前没有任何未平仓仓位",
    )

    seed = commands.add_parser(
        "seed-source-positions",
        help="从 JSON 文件导入一份完整、可信的熬鹰当前仓位快照",
    )
    seed.add_argument("--file", required=True)
    seed.add_argument(
        "--confirm-complete-snapshot",
        action="store_true",
        help="明确确认文件包含源账户所有未平仓仓位",
    )

    args = parser.parse_args()
    store = MirrorStore(args.database)

    if args.command == "assume-source-flat":
        if not args.confirm_source_is_flat:
            parser.error("必须显式添加 --confirm-source-is-flat")
        store.assume_source_flat(
            "操作者明确确认源账户当前所有仓位为空；该确认不是由隐藏仓位接口自动验证"
        )
        print("源仓位基准已设为 SYNCED / FLAT。仅影响模拟账本，不会下单。")
        return 0

    if args.command == "seed-source-positions":
        if not args.confirm_complete_snapshot:
            parser.error("必须显式添加 --confirm-complete-snapshot")
        positions = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if not isinstance(positions, list):
            parser.error("JSON 根节点必须是数组")
        store.set_source_baseline(
            positions,
            f"操作者从文件 {args.file} 导入完整源仓位快照；未由程序独立核实",
        )
        print(f"源仓位基准已导入 {len(positions)} 条。仅影响模拟账本，不会下单。")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
