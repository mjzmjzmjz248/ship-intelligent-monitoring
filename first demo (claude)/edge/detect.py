"""人员检测。检测器做成接口，YOLO 只是其中一种实现，便于后续替换。

检测线程只处理最新一帧（不排队），按目标频率运行，结果带帧时间，过期由调用方判断。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from common.config import DetectorConfig, resolve

log = logging.getLogger("edge.detect")


class PersonDetector(Protocol):
    name: str

    def detect(self, image: np.ndarray) -> list[list[float]]:
        """返回人框列表 [x1, y1, x2, y2, conf]，像素坐标。"""


class YoloPersonDetector:
    def __init__(self, cfg: DetectorConfig) -> None:
        from ultralytics import YOLO  # 延迟导入：测试和岸端不需要 torch

        path = resolve(cfg.model)
        if not path.exists():
            # 不自动联网下载：模型缺失要明确报错
            raise FileNotFoundError(f"模型文件不存在：{path}，请先运行 scripts/fetch_assets.py")
        self.model = YOLO(str(path))
        self.cfg = cfg
        self.name = f"{path.stem}-person"

    def detect(self, image: np.ndarray) -> list[list[float]]:
        res = self.model.predict(image, imgsz=self.cfg.imgsz, conf=self.cfg.conf, classes=[0],
                                 device="cpu", verbose=False)[0]
        out = []
        for (x1, y1, x2, y2), c in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist()):
            if y2 - y1 >= self.cfg.min_box_h:
                out.append([x1, y1, x2, y2, c])
        return out


@dataclass
class DetectionResult:
    frame_ts: float
    frame_no: int
    boxes: list[list[float]]
    infer_ms: float
    width: int
    height: int


class DetectorThread(threading.Thread):
    def __init__(self, detector: PersonDetector, get_frame, target_fps: float) -> None:
        super().__init__(name="detector", daemon=True)
        self.detector = detector
        self.get_frame = get_frame
        self.period = 1.0 / max(target_fps, 0.1)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.result: DetectionResult | None = None
        self.error: str | None = None
        self.runs = 0
        self._rate_window: list[float] = []

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> DetectionResult | None:
        with self._lock:
            return self.result

    def actual_fps(self) -> float:
        now = time.monotonic()
        recent = [t for t in self._rate_window if now - t <= 5.0]
        return len(recent) / 5.0

    def run(self) -> None:
        last_no = -1
        while not self._stop.is_set():
            t0 = time.monotonic()
            frame = self.get_frame()
            if frame is not None and frame.frame_no != last_no:
                last_no = frame.frame_no
                try:
                    s = time.perf_counter()
                    boxes = self.detector.detect(frame.image)
                    ms = (time.perf_counter() - s) * 1000
                    h, w = frame.image.shape[:2]
                    with self._lock:
                        self.result = DetectionResult(frame.ts, frame.frame_no, boxes, ms, w, h)
                    self.runs += 1
                    self._rate_window.append(time.monotonic())
                    self._rate_window = self._rate_window[-100:]
                    self.error = None
                except Exception as e:  # noqa: BLE001
                    self.error = str(e)
                    log.exception("检测失败")
            self._stop.wait(max(0.0, self.period - (time.monotonic() - t0)))
