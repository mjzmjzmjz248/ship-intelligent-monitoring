"""下载运行所需的模型权重（只需联网执行一次）。

用法：python scripts/fetch_assets.py
运行时不会自动下载，模型缺失会直接报错。
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    "yolo11n.pt": "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt",
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    out_dir = ROOT / "models"
    out_dir.mkdir(exist_ok=True)
    lines = ["# 模型来源与校验值", "", "| 文件 | 来源 | SHA-256 |", "| --- | --- | --- |"]
    for name, url in MODELS.items():
        dst = out_dir / name
        if not dst.exists():
            print(f"下载 {url}")
            tmp = dst.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.replace(dst)
        digest = sha256(dst)
        print(f"{name}  {digest}")
        lines.append(f"| {name} | {url} | `{digest}` |")
    lines += ["", "YOLO11 权重由 Ultralytics 发布，许可为 AGPL-3.0；本项目仅作课程演示，对外部署前需另行确认许可。"]
    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
