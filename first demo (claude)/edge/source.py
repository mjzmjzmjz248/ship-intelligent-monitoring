"""视频接入。采集线程只负责取帧并放到共享槽位，不等待识别和网络。

- file：按视频自身时间 1 倍速回放；播完标记 ENDED（不算断流）。
- usb：Windows 上用 DirectShow 打开，避免 MSMF 启动慢。
- rtsp：断线后每 2 秒重连。
故障注入（断流、黑屏）只用于演示和测试，状态会在控制台和事件中标明。
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from common.config import SourceConfig, resolve
from common.timeutil import SessionClock

log = logging.getLogger("edge.source")


@dataclass
class Frame:
    image: np.ndarray  # BGR
    ts: float  # 会话秒
    frame_no: int
    dark: bool


class FaultInjector:
    """演示用故障：stream_cut 丢弃所有帧；black 把画面换成全黑。"""

    def __init__(self) -> None:
        self.mode: str | None = None

    def set(self, mode: str | None) -> None:
        if mode not in (None, "stream_cut", "black"):
            raise ValueError(mode)
        self.mode = mode


def is_dark(image: np.ndarray, gray_thr: int, ratio_thr: float) -> bool:
    small = cv2.resize(image, (160, 90), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return float((gray < gray_thr).mean()) >= ratio_thr


def _open(cfg: SourceConfig) -> cv2.VideoCapture:
    if cfg.kind == "file":
        path = resolve(cfg.path or "")
        if not path.exists():
            raise FileNotFoundError(f"视频文件不存在：{path}")
        return cv2.VideoCapture(str(path))
    if cfg.kind == "usb":
        api = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY
        return cv2.VideoCapture(cfg.index, api)
    if cfg.kind == "rtsp":
        return cv2.VideoCapture(cfg.url or "", cv2.CAP_FFMPEG)
    raise ValueError(f"未知视频源类型 {cfg.kind}")


class CaptureThread(threading.Thread):
    """持续取帧。最新帧放在 latest；每一帧回调 on_frame（用于证据缓冲）。"""

    def __init__(self, cfg: SourceConfig, clock: SessionClock, faults: FaultInjector,
                 on_frame: Callable[[Frame], None], dark_gray: int, dark_ratio: float,
                 target_size: tuple[int, int] = (1280, 720)) -> None:
        super().__init__(name="capture", daemon=True)
        self.cfg = cfg
        self.clock = clock
        self.faults = faults
        self.on_frame = on_frame
        self.dark_gray = dark_gray
        self.dark_ratio = dark_ratio
        self.target_size = target_size
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.latest: Frame | None = None
        self.last_frame_ts: float | None = None
        self.ended = False
        self.error: str | None = None
        self.source_fps: float | None = None
        self.frames_read = 0

    def stop(self) -> None:
        self._stop.set()

    def get_latest(self) -> Frame | None:
        with self._lock:
            return self.latest

    def run(self) -> None:
        try:
            if self.cfg.kind == "file":
                self._run_file()
            else:
                self._run_live()
        except Exception as e:  # noqa: BLE001 —— 采集失败要可见，不能静默退出
            self.error = str(e)
            log.exception("采集线程异常")

    def _deliver(self, image: np.ndarray) -> None:
        if self.faults.mode == "stream_cut":
            return
        if image.shape[1] != self.target_size[0] or image.shape[0] != self.target_size[1]:
            image = cv2.resize(image, self.target_size, interpolation=cv2.INTER_AREA)
        if self.faults.mode == "black":
            image = np.zeros_like(image)
        ts = self.clock.now()
        self.frames_read += 1
        frame = Frame(image=image, ts=ts, frame_no=self.frames_read,
                      dark=is_dark(image, self.dark_gray, self.dark_ratio))
        with self._lock:
            self.latest = frame
            self.last_frame_ts = ts
        self.on_frame(frame)

    def _run_file(self) -> None:
        cap = _open(self.cfg)
        if not cap.isOpened():
            raise RuntimeError(f"无法打开视频 {self.cfg.path}")
        self.source_fps = cap.get(cv2.CAP_PROP_FPS) or None
        start_clock = self.clock.now()
        while not self._stop.is_set():
            if self.clock.paused:
                time.sleep(0.05)
                continue
            ok, img = cap.read()
            if not ok:
                self.ended = True
                break
            media_t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            # 按视频时间 1 倍速回放：读得太快就等
            wait = start_clock + media_t - self.clock.now()
            if wait > 0:
                self._stop.wait(wait)
            self._deliver(img)
        cap.release()

    def _run_live(self) -> None:
        while not self._stop.is_set():
            cap = _open(self.cfg)
            if not cap.isOpened():
                log.warning("视频源打开失败，2 秒后重试")
                self._stop.wait(2)
                continue
            self.source_fps = cap.get(cv2.CAP_PROP_FPS) or None
            while not self._stop.is_set():
                ok, img = cap.read()
                if not ok:
                    log.warning("视频读取失败，准备重连")
                    break
                self._deliver(img)
            cap.release()
            self._stop.wait(2)
