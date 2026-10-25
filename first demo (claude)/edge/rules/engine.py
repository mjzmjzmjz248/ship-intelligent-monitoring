"""通用持续规则状态机。

不依赖视频、网络和系统时间：调用方每次传入当前时刻（会话秒数）和本次判断结果，
规则返回需要处理的输出（新事件、重复提醒、恢复等）。6 类告警都用这一个状态机，
差别只在判断条件（见 alarms.py）和参数。

状态：NORMAL → PENDING → ACTIVE ⇄ RECOVERING → NORMAL
一次"异常开始 → 恢复"对应一个事件；持续异常期间只发重复提醒，不新建事件。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from common.enums import AlarmType, Risk, Verdict


class RuleState(str, Enum):
    NORMAL = "NORMAL"
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    RECOVERING = "RECOVERING"


class OutputKind(str, Enum):
    CREATED = "created"  # 持续满 trigger_sec，新建事件
    REMINDED = "reminded"  # 同一事件持续中，按 repeat_sec 再提醒
    RECOVERED = "recovered"  # 正常持续满 recover_sec
    UNKNOWN = "unknown"  # 已触发事件后续无法观察
    OBSERVABLE = "observable"  # 由无法观察恢复为可观察且仍异常
    PRECONDITION_ENDED = "precondition_ended"  # 如转为停泊
    INTERRUPTED = "interrupted"  # 会话结束、换配置等外部原因


@dataclass
class RuleParamsLite:
    trigger_sec: float
    recover_sec: float
    repeat_sec: float
    risk: Risk


@dataclass
class RuleOutput:
    kind: OutputKind
    alarm_type: AlarmType
    event_id: str
    at: float  # 发生时刻（会话秒）
    started_at: float  # 异常开始时刻
    condition_satisfied_at: float | None = None  # 持续阈值满足时刻
    detail: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


class DurationRule:
    def __init__(self, alarm_type: AlarmType, params: RuleParamsLite) -> None:
        self.alarm_type = alarm_type
        self.params = params
        self.state = RuleState.NORMAL
        self.unknown = False  # ACTIVE 期间是否处于无法观察
        self.event_id: str | None = None
        self._pending_since = 0.0
        self._recover_since = 0.0
        self._last_notify = 0.0
        self._started_at = 0.0
        self._pending_detail: dict[str, Any] = {}

    # ---- 对外 ----
    def update(self, now: float, verdict: Verdict, detail: dict[str, Any] | None = None) -> list[RuleOutput]:
        detail = detail or {}
        handler = {
            RuleState.NORMAL: self._on_normal,
            RuleState.PENDING: self._on_pending,
            RuleState.ACTIVE: self._on_active,
            RuleState.RECOVERING: self._on_recovering,
        }[self.state]
        return handler(now, verdict, detail)

    def interrupt(self, now: float, reason: str) -> list[RuleOutput]:
        """外部原因结束当前判断（视频结束、换配置、程序退出）。"""
        out: list[RuleOutput] = []
        if self.state in (RuleState.ACTIVE, RuleState.RECOVERING):
            out.append(self._out(OutputKind.INTERRUPTED, now, reason=reason))
        self._reset()
        return out

    @property
    def active(self) -> bool:
        return self.state in (RuleState.ACTIVE, RuleState.RECOVERING)

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state.value, "unknown": self.unknown, "event_id": self.event_id}

    # ---- 各状态处理 ----
    def _on_normal(self, now: float, v: Verdict, d: dict[str, Any]) -> list[RuleOutput]:
        if v == Verdict.ABNORMAL:
            self.state = RuleState.PENDING
            self._pending_since = now
            self._pending_detail = d
            return self._on_pending(now, v, d)  # trigger_sec 为 0 时立即触发
        return []

    def _on_pending(self, now: float, v: Verdict, d: dict[str, Any]) -> list[RuleOutput]:
        if v != Verdict.ABNORMAL:
            # 正常、无法判断、前置条件不成立、处于防抖区间：候选计时全部清零
            self._reset()
            return []
        if now - self._pending_since >= self.params.trigger_sec:
            self.state = RuleState.ACTIVE
            self.unknown = False
            self.event_id = str(uuid.uuid4())
            self._started_at = self._pending_since
            self._last_notify = now
            merged = {**self._pending_detail, **d}
            return [self._out(OutputKind.CREATED, now,
                              condition_satisfied_at=self._pending_since + self.params.trigger_sec,
                              detail=merged)]
        return []

    def _on_active(self, now: float, v: Verdict, d: dict[str, Any]) -> list[RuleOutput]:
        if v == Verdict.DISABLED:
            out = [self._out(OutputKind.PRECONDITION_ENDED, now, reason="前置条件不再成立")]
            self._reset()
            return out
        if v == Verdict.UNKNOWN:
            if not self.unknown:
                self.unknown = True
                return [self._out(OutputKind.UNKNOWN, now, detail=d)]
            return []
        out: list[RuleOutput] = []
        if self.unknown:
            if v in (Verdict.ABNORMAL, Verdict.HOLD):
                self.unknown = False
                out.append(self._out(OutputKind.OBSERVABLE, now, detail=d))
            else:
                self.unknown = False
        if v == Verdict.NORMAL:
            self.state = RuleState.RECOVERING
            self._recover_since = now
            return out + self._on_recovering(now, v, d)
        # ABNORMAL 或 HOLD：事件继续
        if now - self._last_notify >= self.params.repeat_sec:
            self._last_notify = now
            out.append(self._out(OutputKind.REMINDED, now, detail=d))
        return out

    def _on_recovering(self, now: float, v: Verdict, d: dict[str, Any]) -> list[RuleOutput]:
        if v == Verdict.NORMAL:
            if now - self._recover_since >= self.params.recover_sec:
                out = [self._out(OutputKind.RECOVERED, now, detail=d)]
                self._reset()
                return out
            return []
        # 恢复未持续满就又异常/不可判断/条件结束：回到 ACTIVE 再处理
        self.state = RuleState.ACTIVE
        return self._on_active(now, v, d)

    # ---- 内部 ----
    def _out(self, kind: OutputKind, now: float, *, condition_satisfied_at: float | None = None,
             detail: dict[str, Any] | None = None, reason: str | None = None) -> RuleOutput:
        assert self.event_id is not None
        return RuleOutput(kind=kind, alarm_type=self.alarm_type, event_id=self.event_id, at=now,
                          started_at=self._started_at, condition_satisfied_at=condition_satisfied_at,
                          detail=detail or {}, reason=reason)

    def _reset(self) -> None:
        self.state = RuleState.NORMAL
        self.unknown = False
        self.event_id = None
        self._pending_detail = {}
