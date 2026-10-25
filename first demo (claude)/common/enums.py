"""船岸共用的枚举与中文名称。"""
from __future__ import annotations

from enum import Enum


class AlarmType(str, Enum):
    A1_BRIDGE_ABSENCE = "A1_BRIDGE_ABSENCE"
    A2_DUTYROOM_ABSENCE = "A2_DUTYROOM_ABSENCE"
    A3_CAMERA_ABNORMAL = "A3_CAMERA_ABNORMAL"
    A4_TERMINAL_OFFLINE = "A4_TERMINAL_OFFLINE"
    A5_AIS_OFFLINE = "A5_AIS_OFFLINE"
    A6_OVERSPEED = "A6_OVERSPEED"


ALARM_NAMES = {
    AlarmType.A1_BRIDGE_ABSENCE: "驾驶室航行中无人在岗",
    AlarmType.A2_DUTYROOM_ABSENCE: "企业值班室人员离岗",
    AlarmType.A3_CAMERA_ABNORMAL: "视频设备异常",
    AlarmType.A4_TERMINAL_OFFLINE: "终端离线",
    AlarmType.A5_AIS_OFFLINE: "AIS设备异常",
    AlarmType.A6_OVERSPEED: "未按规定航速航行",
}

# 依赖视频观测的告警；视频会话结束时这些规则被中断
VIDEO_ALARMS = {
    AlarmType.A1_BRIDGE_ABSENCE,
    AlarmType.A2_DUTYROOM_ABSENCE,
    AlarmType.A3_CAMERA_ABNORMAL,
}


class Risk(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


RISK_RANK = {Risk.HIGH: 0, Risk.MEDIUM: 1, Risk.LOW: 2}
RISK_NAMES = {Risk.HIGH: "高风险", Risk.MEDIUM: "中风险", Risk.LOW: "低风险"}


class ConditionStatus(str, Enum):
    """监测状态：异常本身是否还在。"""
    ACTIVE = "ACTIVE"
    UNKNOWN = "UNKNOWN"  # 已触发，但后续无法观察
    RECOVERED = "RECOVERED"
    PRECONDITION_ENDED = "PRECONDITION_ENDED"  # 如船舶转为停泊
    INTERRUPTED = "INTERRUPTED"  # 视频结束、换配置、程序重启等


class HandlingStatus(str, Enum):
    """处置状态：人是否处理过。"""
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    PROCESSING = "PROCESSING"
    RESOLVED = "RESOLVED"
    FALSE_POSITIVE = "FALSE_POSITIVE"


class EvidenceStatus(str, Enum):
    PENDING = "PENDING"  # 采集或编码中
    READY = "READY"  # 船端已定稿
    PARTIAL = "PARTIAL"  # 定稿但不完整
    FAILED = "FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    VERIFIED = "VERIFIED"  # 岸端已收到且 MD5 一致


class Quality(str, Enum):
    VALID = "VALID"
    DARK = "DARK"
    NO_FRAME = "NO_FRAME"
    STALE = "STALE"  # 有画面，但识别结果过期
    WARMUP = "WARMUP"
    ENDED = "ENDED"


class Verdict(str, Enum):
    """单次规则判断结果。"""
    ABNORMAL = "ABNORMAL"
    NORMAL = "NORMAL"
    UNKNOWN = "UNKNOWN"  # 无法判断，不能当成异常也不能当成正常
    DISABLED = "DISABLED"  # 前置条件不成立
    HOLD = "HOLD"  # 处于进入/退出阈值之间：维持已触发事件，不新触发


class NavState(str, Enum):
    UNDERWAY = "UNDERWAY"
    MOORED = "MOORED"


class Role(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    CAPTAIN = "captain"
    MANAGER = "manager"
    VIEWER = "viewer"
