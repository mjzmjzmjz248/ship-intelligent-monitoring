"""6 类告警中由船端判断的 5 类（A4 终端离线由岸端判断）的判断条件。

每个函数只回答"此刻是异常、正常、无法判断、前置条件不成立还是处于防抖区间"，
持续多久才报警由 engine.DurationRule 负责。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.enums import AlarmType, NavState, Quality, Verdict
from edge.roi import point_in_polygon


@dataclass
class Observation:
    ts: float  # 会话秒
    quality: Quality
    roi_count: int | None = None
    persons: list[list[float]] = field(default_factory=list)  # [x1, y1, x2, y2, conf]
    infer_ms: float | None = None


@dataclass
class Telemetry:
    sampled_at: float  # 会话秒
    nav_state: NavState
    speed_kn: float | None
    position: list[float] | None  # [经度, 纬度]
    source_kind: str = "SIMULATED"


@dataclass
class SpeedZoneLite:
    name: str
    limit_kn: float
    polygon: list[list[float]]


@dataclass
class EvalContext:
    now: float
    obs: Observation | None
    telemetry: Telemetry | None
    last_ais_at: float | None
    telemetry_ttl: float = 3.0
    ais_gap: float = 3.0
    speed_zones: list[SpeedZoneLite] = field(default_factory=list)

    def telemetry_fresh(self) -> bool:
        return self.telemetry is not None and self.now - self.telemetry.sampled_at <= self.telemetry_ttl


Result = tuple[Verdict, dict[str, Any]]


def eval_absence(ctx: EvalContext, require_underway: bool) -> Result:
    """A1（require_underway=True）/ A2：ROI 内无人。看不清时不能当作无人。"""
    if require_underway:
        if not ctx.telemetry_fresh():
            return Verdict.UNKNOWN, {"why": "航行状态数据过期"}
        if ctx.telemetry.nav_state != NavState.UNDERWAY:
            return Verdict.DISABLED, {"why": "非航行状态"}
    obs = ctx.obs
    if obs is None or obs.quality != Quality.VALID or obs.roi_count is None:
        return Verdict.UNKNOWN, {"why": f"观测无效：{obs.quality.value if obs else 'NONE'}"}
    detail = {"roi_count": obs.roi_count}
    return (Verdict.ABNORMAL if obs.roi_count == 0 else Verdict.NORMAL), detail


def eval_camera(ctx: EvalContext) -> Result:
    """A3：断流（NO_FRAME）或明显黑屏（DARK）。识别结果过期不是摄像头问题。"""
    obs = ctx.obs
    if obs is None or obs.quality in (Quality.WARMUP, Quality.ENDED):
        return Verdict.UNKNOWN, {}
    if obs.quality == Quality.NO_FRAME:
        return Verdict.ABNORMAL, {"subtype": "STREAM_LOST"}
    if obs.quality == Quality.DARK:
        return Verdict.ABNORMAL, {"subtype": "BLACK_SCREEN"}
    return Verdict.NORMAL, {}


def eval_ais(ctx: EvalContext) -> Result:
    """A5：本船 AIS 设备持续无报文输出。"""
    if ctx.last_ais_at is None or ctx.now - ctx.last_ais_at > ctx.ais_gap:
        return Verdict.ABNORMAL, {"last_ais_at": ctx.last_ais_at}
    return Verdict.NORMAL, {}


def eval_overspeed(ctx: EvalContext, enter_margin: float, exit_margin: float) -> Result:
    """A6：航行中、在限速区内超速。进入和退出用不同阈值，避免在限速附近反复触发。"""
    if not ctx.telemetry_fresh():
        return Verdict.UNKNOWN, {"why": "船况数据过期"}
    t = ctx.telemetry
    if t.nav_state != NavState.UNDERWAY:
        return Verdict.DISABLED, {"why": "非航行状态"}
    if t.speed_kn is None or t.position is None:
        return Verdict.UNKNOWN, {"why": "航速或位置缺失"}
    zone = next((z for z in ctx.speed_zones if point_in_polygon(t.position, z.polygon)), None)
    if zone is None:
        return Verdict.NORMAL, {"zone": None, "speed_kn": t.speed_kn}
    detail = {"zone": zone.name, "limit_kn": zone.limit_kn, "speed_kn": t.speed_kn}
    if t.speed_kn > zone.limit_kn + enter_margin:
        return Verdict.ABNORMAL, detail
    if t.speed_kn < zone.limit_kn - exit_margin:
        return Verdict.NORMAL, detail
    return Verdict.HOLD, detail


def evaluate(alarm: AlarmType, ctx: EvalContext, extra: dict[str, Any]) -> Result:
    if alarm == AlarmType.A1_BRIDGE_ABSENCE:
        return eval_absence(ctx, require_underway=True)
    if alarm == AlarmType.A2_DUTYROOM_ABSENCE:
        return eval_absence(ctx, require_underway=False)
    if alarm == AlarmType.A3_CAMERA_ABNORMAL:
        return eval_camera(ctx)
    if alarm == AlarmType.A5_AIS_OFFLINE:
        return eval_ais(ctx)
    if alarm == AlarmType.A6_OVERSPEED:
        return eval_overspeed(ctx, float(extra.get("enter_margin_kn", 0.5)),
                              float(extra.get("exit_margin_kn", 0.2)))
    raise ValueError(f"船端不判断 {alarm}")
