"""一键启动：岸端 + 船端（同一台电脑上的两个独立进程）。

  python scripts/run_all.py [--config configs/demo.yaml] [--camera bridge-cam-01]

首次运行会自动初始化岸端数据库（账号密码写入 runtime/shore/initial_accounts.txt）。
按 Ctrl+C 结束；Windows 下通过 CTRL_BREAK 通知船端正常收尾。
打开：岸端平台 http://127.0.0.1:8000    船端控制台 http://127.0.0.1:8100
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IS_WIN = sys.platform.startswith("win")


def spawn(args: list[str]) -> subprocess.Popen:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
    return subprocess.Popen([sys.executable, *args], cwd=ROOT, creationflags=flags)


def stop(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        p.send_signal(signal.CTRL_BREAK_EVENT if IS_WIN else signal.SIGINT)
        p.wait(timeout=8)
    except Exception:  # noqa: BLE001
        p.terminate()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/demo.yaml")
    ap.add_argument("--camera", default=None)
    args = ap.parse_args()

    if not (ROOT / "runtime" / "shore" / "shore.db").exists():
        subprocess.run([sys.executable, "scripts/init_shore.py", "--config", args.config], cwd=ROOT, check=True)
    for need in ("models/yolo11n.pt", "data/synthetic_duty.mp4", "assets/audio/tone_high.wav"):
        if not (ROOT / need).exists():
            print(f"缺少 {need}，请先按 README 运行 fetch_assets.py / make_synthetic_video.py / gen_audio.py")
            sys.exit(1)

    shore = spawn(["-m", "shore"])
    time.sleep(2)
    edge_args = ["-m", "edge", "--config", args.config] + (["--camera", args.camera] if args.camera else [])
    edge = spawn(edge_args)
    print("岸端平台 http://127.0.0.1:8000    船端控制台 http://127.0.0.1:8100    Ctrl+C 结束")
    try:
        while shore.poll() is None and edge.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop(edge)
        stop(shore)


if __name__ == "__main__":
    main()
