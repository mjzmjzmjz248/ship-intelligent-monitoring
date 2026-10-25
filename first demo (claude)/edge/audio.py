"""本地语音告警：按风险从高到低串行播报，正在播放的不打断。

播放方式：Windows 用标准库 winsound；其他平台尝试 simpleaudio；都不可用（如虚拟机无声卡）时
按音频时长等待并在日志里标注"未实际播放"，保证播报队列的时序仍可验证。
"""
from __future__ import annotations

import itertools
import logging
import queue
import sys
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from common.config import ROOT
from common.enums import RISK_RANK, AlarmType, Risk

log = logging.getLogger("edge.audio")
AUDIO_DIR = ROOT / "assets" / "audio"


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _detect_backend() -> str:
    if sys.platform.startswith("win"):
        return "winsound"
    try:
        import simpleaudio  # noqa: F401

        return "simpleaudio"
    except Exception:  # noqa: BLE001
        return "none"


def _play(path: Path, backend: str) -> None:
    if backend == "winsound":
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME)
    elif backend == "simpleaudio":
        import simpleaudio

        simpleaudio.WaveObject.from_wave_file(str(path)).play().wait_done()
    else:
        time.sleep(_wav_duration(path))


@dataclass(order=True)
class _Item:
    rank: int
    seq: int
    alarm: AlarmType = field(compare=False)
    risk: Risk = field(compare=False)
    event_id: str = field(compare=False)
    kind: str = field(compare=False)  # created / reminded


class AudioAnnouncer(threading.Thread):
    def __init__(self, on_started: Callable[[str, str, float], None] | None = None) -> None:
        super().__init__(name="audio", daemon=True)
        self.backend = _detect_backend()
        self.on_started = on_started  # (event_id, kind, wall_ts)
        self._q: queue.PriorityQueue[_Item] = queue.PriorityQueue()
        self._seq = itertools.count()
        self._stop = threading.Event()
        self.played: list[dict] = []  # 最近播报记录，供控制台显示
        if self.backend == "none":
            log.warning("没有可用的音频输出，播报只记录日志，不实际发声")

    def announce(self, alarm: AlarmType, risk: Risk, event_id: str, kind: str = "created") -> None:
        self._q.put(_Item(RISK_RANK[risk], next(self._seq), alarm, risk, event_id, kind))

    def queued(self) -> int:
        return self._q.qsize()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            files = []
            tone = AUDIO_DIR / ("tone_high.wav" if item.risk == Risk.HIGH else "tone_normal.wav")
            if tone.exists():
                files.append(tone)
            voice = AUDIO_DIR / f"{item.alarm.value}.wav"
            if voice.exists():
                files.append(voice)
            started = time.time()
            if self.on_started:
                self.on_started(item.event_id, item.kind, started)
            self.played.append({"event_id": item.event_id, "alarm": item.alarm.value, "kind": item.kind,
                                "started": started, "backend": self.backend,
                                "files": [f.name for f in files]})
            self.played = self.played[-50:]
            note = "" if self.backend != "none" else "（未实际播放：无音频设备）"
            log.info("播报 %s %s %s%s", item.risk.value, item.alarm.value, item.kind, note)
            for f in files:
                try:
                    _play(f, self.backend)
                except Exception:  # noqa: BLE001
                    log.exception("播放失败 %s", f)
