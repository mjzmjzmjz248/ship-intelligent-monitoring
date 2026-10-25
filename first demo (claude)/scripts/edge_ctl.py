"""船端控制台命令行，演示时用：

  python scripts/edge_ctl.py status
  python scripts/edge_ctl.py sync off | sync on
  python scripts/edge_ctl.py fault stream_cut | fault black | fault clear
  python scripts/edge_ctl.py speed 10        # 模拟航速（节）
  python scripts/edge_ctl.py nav UNDERWAY | nav MOORED
  python scripts/edge_ctl.py ais off | ais on
  python scripts/edge_ctl.py sim clear       # 取消手动覆盖，回到场景时间轴
  python scripts/edge_ctl.py camera dutyroom-cam-01
  python scripts/edge_ctl.py source restart | pause | resume
"""
from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8100"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, *rest = argv
    arg = rest[0] if rest else None
    if cmd == "status":
        r = httpx.get(f"{BASE}/status")
        h = r.json()["health"]
        print(json.dumps({k: h[k] for k in ("camera_id", "quality", "roi_count", "detect_fps", "sync_enabled",
                                             "fault", "outbox_pending", "profile")}, ensure_ascii=False, indent=1))
        return 0
    if cmd == "sync":
        r = httpx.post(f"{BASE}/sync/{arg}")
    elif cmd == "fault":
        r = httpx.post(f"{BASE}/fault/{arg}")
    elif cmd == "speed":
        r = httpx.post(f"{BASE}/sim", json={"speed_kn": float(arg)})
    elif cmd == "nav":
        r = httpx.post(f"{BASE}/sim", json={"nav_state": arg})
    elif cmd == "ais":
        r = httpx.post(f"{BASE}/sim", json={"ais": arg == "on"})
    elif cmd == "sim" and arg == "clear":
        r = httpx.post(f"{BASE}/sim", json={"clear": True})
    elif cmd == "camera":
        r = httpx.post(f"{BASE}/camera/{arg}")
    elif cmd == "source":
        r = httpx.post(f"{BASE}/source/{arg}")
    else:
        print(__doc__)
        return 1
    print(r.status_code, r.text)
    return 0 if r.status_code == 200 else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
