"""船岸之间传输的消息结构。船端按此序列化，岸端按此校验。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from common.enums import AlarmType, ConditionStatus, EvidenceStatus, Risk

SCHEMA_VERSION = "1.0"


class EvidenceRef(BaseModel):
    evidence_id: str
    kind: str  # snapshot / video
    status: EvidenceStatus
    md5: str | None = None
    size: int | None = None
    missing_reason: str | None = None
    time_range: list[str | None] | None = None  # [开始, 结束] UTC ISO


class EventMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    event_id: str
    revision: int
    device_id: str
    vessel_id: str
    camera_id: str | None = None
    scene: str | None = None
    session_id: str | None = None
    alarm_type: AlarmType
    subtype: str | None = None
    risk: Risk
    condition_status: ConditionStatus
    started_at: str
    condition_satisfied_at: str
    triggered_at: str
    sound_started_at: str | None = None
    recovered_at: str | None = None
    ended_reason: str | None = None
    reminder_count: int = 0
    profile: str
    rule_version: int
    rule_params: dict[str, Any]
    source_kind: dict[str, str]
    telemetry: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    model: str | None = None
    evidence: list[EvidenceRef] = []


class HeartbeatMessage(BaseModel):
    device_id: str
    boot_id: str
    seq: int
    sent_at: str
    applied_config_version: int = 0
    health: dict[str, Any] = {}


class HeartbeatReply(BaseModel):
    server_time: str
    config_version: int
