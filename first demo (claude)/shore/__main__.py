"""岸端入口：python -m shore [--port 8000]"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from shore.main import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="船舶智能监控 岸端服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=None, help="数据库路径，默认 runtime/shore/shore.db")
    ap.add_argument("--storage", default=None, help="证据目录，默认 runtime/shore/evidence")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # 单进程运行：调度器只能有一份，不使用 --workers 或自动重载
    uvicorn.run(create_app(args.db, args.storage), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
