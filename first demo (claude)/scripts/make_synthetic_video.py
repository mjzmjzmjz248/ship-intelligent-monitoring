"""生成联调用的合成测试视频（不是实拍数据，不能用来评价真实识别效果）。

做法：画一个简化的"驾驶室/值班室"背景，把 Ultralytics 自带样例图 zidane.jpg 中的一个人
抠出来贴到画面里，按时间轴控制"有人 / 无人 / 黑屏"，输出 1280×720、15 FPS 的 H.264 MP4，
同时输出真值 CSV（媒体时间，单位秒）。

用法：python scripts/make_synthetic_video.py
"""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import ROOT  # noqa: E402

W, H, FPS = 1280, 720, 15
# (开始秒, 结束秒, 状态)  present=有人  absent=无人  dark=黑屏
TIMELINE = [
    (0, 20, "present"),
    (20, 40, "absent"),
    (40, 55, "present"),
    (55, 62, "absent"),   # 短暂离开 7 秒，不应触发
    (62, 72, "present"),
    (72, 82, "dark"),
    (82, 100, "present"),
]


def background() -> np.ndarray:
    img = np.zeros((H, W, 3), np.uint8)
    for y in range(H):  # 竖向渐变：上方窗户偏亮
        v = int(150 - 70 * y / H)
        img[y, :] = (v + 20, v + 10, v)
    cv2.rectangle(img, (80, 60), (1200, 300), (200, 185, 160), -1)  # 窗户
    for x in range(80, 1200, 280):
        cv2.line(img, (x, 60), (x, 300), (90, 90, 90), 6)
    cv2.rectangle(img, (0, 520), (W, H), (60, 65, 70), -1)  # 控制台
    for i, x in enumerate(range(120, 1200, 220)):
        cv2.rectangle(img, (x, 545), (x + 150, 640), (30, 40, 30) if i % 2 else (40, 40, 60), -1)
        cv2.rectangle(img, (x + 10, 555), (x + 140, 600), (40, 120, 60), -1)
    cv2.putText(img, "SYNTHETIC TEST VIDEO", (20, 700), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
    return img


def person_cutout() -> tuple[np.ndarray, np.ndarray]:
    import ultralytics

    src = Path(ultralytics.__file__).parent / "assets" / "zidane.jpg"
    img = cv2.imread(str(src))
    if img is None:
        raise FileNotFoundError(src)
    # zidane.jpg 中右侧人物的大致范围；用 GrabCut 抠出前景
    x1, y1, x2, y2 = 740, 40, 1150, 715
    crop = img[y1:y2, x1:x2].copy()
    mask = np.zeros(crop.shape[:2], np.uint8)
    rect = (20, 10, crop.shape[1] - 40, crop.shape[0] - 12)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(crop, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
    alpha = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    alpha = cv2.GaussianBlur(alpha, (7, 7), 0)
    return crop, alpha


def paste(bg: np.ndarray, fg: np.ndarray, alpha: np.ndarray, cx: int, bottom: int, height: int) -> np.ndarray:
    scale = height / fg.shape[0]
    fg = cv2.resize(fg, None, fx=scale, fy=scale)
    a = cv2.resize(alpha, (fg.shape[1], fg.shape[0])).astype(np.float32)[..., None] / 255
    h, w = fg.shape[:2]
    x0, y0 = cx - w // 2, bottom - h
    out = bg.copy()
    roi = out[y0:y0 + h, x0:x0 + w]
    out[y0:y0 + h, x0:x0 + w] = (fg * a + roi * (1 - a)).astype(np.uint8)
    return out


def main() -> None:
    import imageio_ffmpeg

    out = ROOT / "data" / "synthetic_duty.mp4"
    out.parent.mkdir(exist_ok=True)
    bg = background()
    fg, alpha = person_cutout()
    rng = np.random.default_rng(0)
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-g", str(FPS * 2), str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    total = TIMELINE[-1][1] * FPS
    for i in range(total):
        t = i / FPS
        st = next(s for a, b, s in TIMELINE if a <= t < b)
        if st == "dark":
            frame = np.full((H, W, 3), 4, np.uint8)
        else:
            frame = bg.copy()
            if st == "present":
                sway = int(25 * np.sin(t * 0.7))
                frame = paste(frame, fg, alpha, W // 2 + sway, 690, 520)
            noise = rng.integers(-4, 5, frame.shape, dtype=np.int16)  # 轻微噪声，接近真实摄像头
            frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    proc.wait()

    truth = ROOT / "data" / "synthetic_duty_truth.csv"
    with open(truth, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["video_id", "start_s", "end_s", "state", "note"])
        for a, b, s in TIMELINE:
            w.writerow(["synthetic_duty", a, b, s, "合成视频真值"])
    print(f"已生成 {out}（{TIMELINE[-1][1]} 秒）和 {truth}")


if __name__ == "__main__":
    main()
