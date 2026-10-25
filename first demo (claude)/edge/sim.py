"""模拟船况与本船 AIS 输出。所有数据标记 SIMULATED，界面和事件中都能看到来源。

按场景文件的时间轴给出航行状态、航速、船位和 AIS 是否在线；控制台可以手动覆盖。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from common.config import resolve
from common.enums import NavState
from edge.rules.alarms import Telemetry


class TelemetrySim:
    def __init__(self, scenario_path: str | Path) -> None:
        with open(resolve(scenario_path), encoding="utf-8") as f:
            sc = yaml.safe_load(f)
        self.loop_sec = float(sc.get("loop_sec", 0)) or None
        self.segments = sorted(sc["segments"], key=lambda s: s["at"])
        self._lock = threading.Lock()
        self.overrides: dict[str, Any] = {}
        self.last_ais_at: float | None = None
        self.latest: Telemetry | None = None

    def set_override(self, **kw: Any) -> None:
        """可覆盖：speed_kn、nav_state、ais(bool)、telemetry(bool，False 表示船况数据中断)。值为 None 表示取消覆盖。"""
        with self._lock:
            for k, v in kw.items():
                if v is None:
                    self.overrides.pop(k, None)
                else:
                    self.overrides[k] = v

    def clear_overrides(self) -> None:
        with self._lock:
            self.overrides.clear()

    def _segment(self, t: float) -> dict[str, Any]:
        if self.loop_sec:
            t = t % self.loop_sec
        seg = self.segments[0]
        for s in self.segments:
            if s["at"] <= t:
                seg = s
        return seg

    def tick(self, now: float) -> Telemetry | None:
        """每秒调用一次；船况中断时不更新 latest，由规则按过期处理。"""
        seg = self._segment(now)
        with self._lock:
            ov = dict(self.overrides)
        if ov.get("telemetry", True):
            self.latest = Telemetry(
                sampled_at=now,
                nav_state=NavState(ov.get("nav_state", seg["nav_state"])),
                speed_kn=float(ov.get("speed_kn", seg["speed_kn"])),
                position=list(ov.get("position", seg["position"])),
            )
        if ov.get("ais", seg.get("ais", True)):
            self.last_ais_at = now
        return self.latest

    def describe(self) -> dict[str, Any]:
        t = self.latest
        return {
            "source_kind": "SIMULATED",
            "nav_state": t.nav_state.value if t else None,
            "speed_kn": t.speed_kn if t else None,
            "position": t.position if t else None,
            "sampled_at": t.sampled_at if t else None,
            "last_ais_at": self.last_ais_at,
            "overrides": dict(self.overrides),
        }
