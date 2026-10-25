"""初始化岸端数据库：演示船、船端设备、摄像头、账号、初始配置版本。

用法：python scripts/init_shore.py [--config configs/demo.yaml] [--password 统一密码]
不给 --password 时为每个账号随机生成密码，写入 runtime/shore/initial_accounts.txt（不入库）。
重复运行是安全的：已有的配置版本不会被覆盖，账号密码会按本次重置。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import RUNTIME_DIR  # noqa: E402
from shore.bootstrap import init  # noqa: E402
from shore.db import Database  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/demo.yaml")
    ap.add_argument("--db", default=str(RUNTIME_DIR / "shore" / "shore.db"))
    ap.add_argument("--password", default=None, help="所有演示账号使用同一密码；不填则随机生成")
    args = ap.parse_args()
    db = Database(args.db)
    accounts_file = RUNTIME_DIR / "shore" / "initial_accounts.txt"
    init(db, args.config, args.password, accounts_file)
    print(f"岸端已初始化：{args.db}")
    print(f"账号和密码见：{accounts_file}")


if __name__ == "__main__":
    main()
