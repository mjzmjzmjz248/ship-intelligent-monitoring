"""告警证据：叠加信息的截图 + 触发前后各 5 秒的 MP4，船端定稿后计算 MD5。

- 帧缓冲：按 15 FPS 采样，把最近几秒的帧压成 JPEG 放在内存里。
- 触发时立即生成截图；视频要等触发后 5 秒的帧收齐，再交给后台线程用 FFmpeg 编码，
  不阻塞识别和语音。
- 证据不完整时如实记录原因，不用其他片段冒充。
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from common.config import EvidenceConfig
from common.enums import EvidenceStatus
from common.timeutil import SessionClock, to_iso, utcnow

log = logging.getLogger("edge.evidence")

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]


def load_font(size: int) -> ImageFont.ImageFont:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    log.warning("未找到中文字体，截图文字可能无法显示中文")
    return ImageFont.load_default()


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def draw_annotations(image: np.ndarray, lines: list[str], roi: list[list[float]] | None,
                     boxes: list[list[float]] | None, font: ImageFont.ImageFont,
                     alert: bool = False) -> np.ndarray:
    """在图像上画 ROI、人框和中文信息栏，返回新图像。"""
    img = image.copy()
    h, w = img.shape[:2]
    if roi:
        pts = np.array([[int(x * w), int(y * h)] for x, y in roi], np.int32)
        cv2.polylines(img, [pts], True, (0, 0, 255) if alert else (0, 200, 255), 3)
    for b in boxes or []:
        x1, y1, x2, y2 = (int(v) for v in b[:4])
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        if len(b) > 4:
            cv2.putText(img, f"person {b[4]:.2f}", (x1, max(15, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 255, 0), 2)
    if lines:
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil, "RGBA")
        size = getattr(font, "size", 20)
        pad = 8
        box_h = pad * 2 + len(lines) * (size + 6)
        box_w = max(int(draw.textlength(s, font=font)) for s in lines) + pad * 2
        draw.rectangle([0, 0, box_w, box_h], fill=(0, 0, 0, 150))
        for i, s in enumerate(lines):
            draw.text((pad, pad + i * (size + 6)), s, font=font, fill=(255, 255, 255, 255))
        img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    return img


@dataclass
class _ClipJob:
    event_id: str
    evidence_id: str
    anchor_ts: float
    start_ts: float
    end_ts: float
    frames: list[tuple[float, bytes]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


class EvidenceManager:
    def __init__(self, cfg: EvidenceConfig, store, out_dir: Path, clock: SessionClock) -> None:
        self.cfg = cfg
        self.store = store
        self.out_dir = Path(out_dir)
        self.clock = clock
        self.font = load_font(26)
        self._lock = threading.Lock()
        self._ring: deque[tuple[float, bytes]] = deque()
        self._last_kept = -1e9
        self._session_start = 0.0
        self._jobs: list[_ClipJob] = []
        self._encode_q: queue.Queue[_ClipJob | None] = queue.Queue(maxsize=4)
        self._encoder = threading.Thread(target=self._encode_loop, name="encoder", daemon=True)
        self._encoder.start()
        self.encode_failures = 0

    # ---- 帧输入（采集线程调用）----
    def new_session(self, start_ts: float) -> None:
        with self._lock:
            self._ring.clear()
            self._last_kept = -1e9
            self._session_start = start_ts

    def on_frame(self, ts: float, image: np.ndarray) -> None:
        if ts - self._last_kept < 1.0 / self.cfg.fps - 0.005:
            return
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
        if not ok:
            return
        data = buf.tobytes()
        done: list[_ClipJob] = []
        with self._lock:
            self._last_kept = ts
            self._ring.append((ts, data))
            keep_from = ts - self.cfg.pre_sec - 1.0
            while self._ring and self._ring[0][0] < keep_from:
                self._ring.popleft()
            for job in self._jobs:
                if job.anchor_ts < ts <= job.end_ts:
                    job.frames.append((ts, data))
                if ts >= job.end_ts:
                    done.append(job)
            for job in done:
                self._jobs.remove(job)
        for job in done:
            self._submit(job)

    # ---- 事件触发（主循环调用）----
    def capture(self, event_id: str, anchor_ts: float, image: np.ndarray | None, image_ts: float | None,
                lines: list[str], roi: list[list[float]] | None, boxes: list[list[float]] | None,
                want_video: bool, video_na_reason: str | None = None) -> list[dict[str, Any]]:
        """生成截图并登记视频任务，返回证据引用列表（状态可能仍是 PENDING）。"""
        refs = []
        day = utcnow().strftime("%Y-%m-%d")
        ev_dir = self.out_dir / day / event_id
        ev_dir.mkdir(parents=True, exist_ok=True)

        # 截图
        if image is None:
            sid = self.store.add_evidence(event_id, "snapshot", EvidenceStatus.FAILED.value, "无可用画面")
            self.store.finalize_evidence(sid, status=EvidenceStatus.FAILED.value, path=None, md5=None, size=None,
                                         missing_reason="无可用画面")
        else:
            stale = image_ts is not None and anchor_ts - image_ts > 1.0
            if stale:
                lines = lines + [f"注意：最后有效画面，采集于触发前 {anchor_ts - image_ts:.1f} 秒"]
            annotated = draw_annotations(image, lines, roi, boxes, self.font, alert=True)
            sid = self.store.add_evidence(event_id, "snapshot", EvidenceStatus.PENDING.value)
            path = ev_dir / f"{sid}.jpg"
            cv2.imwrite(str(path), annotated, [cv2.IMWRITE_JPEG_QUALITY, 90])
            shot_at = to_iso(self.clock.wall_of(image_ts if image_ts is not None else anchor_ts))
            self.store.finalize_evidence(sid, status=(EvidenceStatus.PARTIAL if stale else EvidenceStatus.READY).value,
                                         path=str(path), md5=md5_file(path), size=path.stat().st_size,
                                         missing_reason="非触发时刻画面" if stale else None,
                                         time_range=[shot_at, shot_at])
        refs.append(self.store.get_evidence(sid))

        # 视频
        if not want_video:
            vid = self.store.add_evidence(event_id, "video", EvidenceStatus.NOT_APPLICABLE.value, video_na_reason)
            self.store.finalize_evidence(vid, status=EvidenceStatus.NOT_APPLICABLE.value, path=None, md5=None,
                                         size=None, missing_reason=video_na_reason)
        else:
            vid = self.store.add_evidence(event_id, "video", EvidenceStatus.PENDING.value)
            job = _ClipJob(event_id, vid, anchor_ts, anchor_ts - self.cfg.pre_sec, anchor_ts + self.cfg.post_sec)
            with self._lock:
                job.frames = [(t, d) for t, d in self._ring if job.start_ts <= t <= anchor_ts]
                if anchor_ts - self._session_start < self.cfg.pre_sec:
                    job.missing.append("会话启动不足 5 秒，触发前画面不全")
                self._jobs.append(job)
        refs.append(self.store.get_evidence(vid))
        return refs

    def tick(self, now: float) -> None:
        """触发后长时间收不到帧（断流、视频结束）时，用已有帧定稿。"""
        expired: list[_ClipJob] = []
        with self._lock:
            for job in list(self._jobs):
                if now > job.end_ts + 3.0:
                    job.missing.append("触发后画面中断，后续画面不全")
                    self._jobs.remove(job)
                    expired.append(job)
        for job in expired:
            self._submit(job)

    def flush(self, reason: str) -> None:
        """会话结束时把未完成的视频任务按已有帧定稿。"""
        with self._lock:
            jobs, self._jobs = self._jobs, []
        for job in jobs:
            job.missing.append(reason)
            self._submit(job)

    def pending_jobs(self) -> int:
        with self._lock:
            return len(self._jobs) + self._encode_q.qsize()

    def stop(self) -> None:
        self._encode_q.put(None)

    # ---- 编码 ----
    def _submit(self, job: _ClipJob) -> None:
        try:
            self._encode_q.put_nowait(job)
        except queue.Full:
            self._finalize_failed(job, "编码队列已满")

    def _finalize_failed(self, job: _ClipJob, reason: str) -> None:
        self.encode_failures += 1
        self.store.finalize_evidence(job.evidence_id, status=EvidenceStatus.FAILED.value, path=None, md5=None,
                                     size=None, missing_reason=reason)
        self._notify(job)

    def _notify(self, job: _ClipJob) -> None:
        cb = getattr(self, "on_evidence_final", None)
        if cb:
            cb(job.event_id)

    def _encode_loop(self) -> None:
        while True:
            job = self._encode_q.get()
            if job is None:
                return
            try:
                self._encode(job)
            except Exception as e:  # noqa: BLE001
                log.exception("视频编码失败")
                self._finalize_failed(job, f"编码失败：{e}")

    def _encode(self, job: _ClipJob) -> None:
        frames = sorted(job.frames, key=lambda x: x[0])
        if len(frames) < 2:
            self._finalize_failed(job, "；".join(job.missing + ["可用帧不足"]))
            return
        day = utcnow().strftime("%Y-%m-%d")
        out = self.out_dir / day / job.event_id / f"{job.evidence_id}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part.mp4")
        cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(self.cfg.fps),
               "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(tmp)]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        try:
            for _, data in frames:
                proc.stdin.write(data)
            proc.stdin.close()
        except OSError:
            pass
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0 or not tmp.exists():
            self._finalize_failed(job, f"ffmpeg 退出码 {proc.returncode}：{err[:200]}")
            return
        os.replace(tmp, out)
        first, last = frames[0][0], frames[-1][0]
        covered_pre = job.anchor_ts - first
        covered_post = last - job.anchor_ts
        missing = list(job.missing)
        if covered_pre < self.cfg.pre_sec - 0.5 and not any("触发前" in m for m in missing):
            missing.append(f"触发前只有 {covered_pre:.1f} 秒")
        if covered_post < self.cfg.post_sec - 0.5 and not any("后续" in m for m in missing):
            missing.append(f"触发后只有 {covered_post:.1f} 秒")
        status = EvidenceStatus.PARTIAL if missing else EvidenceStatus.READY
        self.store.finalize_evidence(job.evidence_id, status=status.value, path=str(out), md5=md5_file(out),
                                     size=out.stat().st_size, missing_reason="；".join(missing) or None,
                                     time_range=[to_iso(self.clock.wall_of(first)), to_iso(self.clock.wall_of(last))])
        self._notify(job)
