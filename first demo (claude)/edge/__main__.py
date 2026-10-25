"""船端入口：python -m edge --config configs/demo.yaml"""
from __future__ import annotations

import argparse
import logging
import signal

from edge.app import EdgeApp


def main() -> None:
    ap = argparse.ArgumentParser(description="船舶智能监控 船端程序")
    ap.add_argument("--config", default="configs/demo.yaml")
    ap.add_argument("--camera", default=None, help="启动时使用的摄像头 ID，默认取配置中的 active_camera")
    ap.add_argument("--shore", default=None, help="岸端地址，默认取配置中的 sync.shore_url")
    ap.add_argument("--run-dir", default=None, help="船端数据目录，默认 runtime/edge")
    ap.add_argument("--console-port", type=int, default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 心跳和预览请求太频繁，不逐条打印
    app = EdgeApp(args.config, camera_id=args.camera, shore_url=args.shore, run_dir=args.run_dir)
    if args.console_port:
        app.cfg.console_port = args.console_port
    # Ctrl+C 与 Windows 下的 Ctrl+Break 都能正常退出
    signal.signal(signal.SIGINT, lambda *_: app.stop())
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: app.stop())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: app.stop())
    app.run_forever()


if __name__ == "__main__":
    main()
