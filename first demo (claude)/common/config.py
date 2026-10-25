"""配置加载与校验。路径一律相对项目根目录解析，便于在 Windows 上直接运行。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.enums import AlarmType, Risk

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"
RUNTIME_DIR = ROOT / "runtime"


def resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


class RuleParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    trigger_sec: float = Field(ge=0)
    recover_sec: float = Field(ge=0)
    repeat_sec: float = Field(gt=0)
    risk: Risk
    basis: str = ""  # 参数依据，页面上显示
    pending: list[str] = []  # 尚待老师确认的字段名


class RoiConfig(BaseModel):
    zone_id: str
    anchor: str = "center"  # center: 人框中心点；foot: 人框底边中点
    points: list[list[float]]

    @field_validator("points")
    @classmethod
    def _check_points(cls, v: list[list[float]]) -> list[list[float]]:
        if len(v) < 3:
            raise ValueError("ROI 至少需要 3 个点")
        for p in v:
            if len(p) != 2 or not all(0.0 <= c <= 1.0 for c in p):
                raise ValueError(f"ROI 坐标必须是 0~1 之间的 [x, y]：{p}")
        return v

    @field_validator("anchor")
    @classmethod
    def _check_anchor(cls, v: str) -> str:
        if v not in ("center", "foot"):
            raise ValueError("anchor 只能是 center 或 foot")
        return v


class SourceConfig(BaseModel):
    kind: str  # file / usb / rtsp
    path: str | None = None  # file
    index: int = 0  # usb
    url: str | None = None  # rtsp
    loop: bool = False
    tag: str = "LAB_REPLAY"  # 来源标签：LAB_REPLAY / SYNTHETIC / LIVE_USB / LIVE_RTSP


class CameraConfig(BaseModel):
    camera_id: str
    scene: str
    name: str
    source: SourceConfig
    roi: RoiConfig
    alarms: list[AlarmType]


class DetectorConfig(BaseModel):
    model: str = "models/yolo11n.pt"
    imgsz: int = 640
    conf: float = 0.35
    min_box_h: int = 60  # 以 720P 画面计
    target_fps: float = 5.0
    max_result_age_sec: float = 1.0


class QualityConfig(BaseModel):
    no_frame_sec: float = 2.0
    dark_gray: int = 15
    dark_ratio: float = 0.95
    warmup_sec: float = 6.0


class SpeedZone(BaseModel):
    name: str
    limit_kn: float
    polygon: list[list[float]]  # GeoJSON 顺序 [lon, lat]


class TelemetryConfig(BaseModel):
    scenario: str
    ttl_sec: float = 3.0
    ais_gap_sec: float = 3.0
    speed_zones: list[SpeedZone] = []


class EvidenceConfig(BaseModel):
    pre_sec: float = 5.0
    post_sec: float = 5.0
    fps: int = 15
    width: int = 1280
    height: int = 720
    jpeg_quality: int = 85
    retain_days: int = 7
    quota_gb: float = 20.0


class SyncConfig(BaseModel):
    shore_url: str = "http://127.0.0.1:8000"
    heartbeat_sec: float = 5.0
    config_poll_sec: float = 5.0
    preview_fps: float = 2.0
    timeout_sec: float = 5.0


class DeviceConfig(BaseModel):
    device_id: str
    token_env: str = "DEVICE_TOKEN"
    default_token: str = "demo-device-token"  # 仅本机演示；正式部署必须通过环境变量提供


class VesselConfig(BaseModel):
    vessel_id: str
    name: str
    ship_type: str


class ShoreConfig(BaseModel):
    offline_after_sec: float
    offline_repeat_sec: float = 60.0
    offline_basis: str = ""
    offline_pending: bool = False
    captain_deadline_sec: float
    late_window_sec: float = 30.0
    offline_risk: Risk = Risk.MEDIUM


class AppConfig(BaseModel):
    profile: str
    timezone: str = "Asia/Shanghai"
    console_port: int = 8100
    device: DeviceConfig
    vessel: VesselConfig
    active_camera: str
    cameras: list[CameraConfig]
    detector: DetectorConfig = DetectorConfig()
    quality: QualityConfig = QualityConfig()
    telemetry: TelemetryConfig
    evidence: EvidenceConfig = EvidenceConfig()
    sync: SyncConfig = SyncConfig()
    rules: dict[AlarmType, RuleParams]
    shore: ShoreConfig

    def camera(self, camera_id: str | None = None) -> CameraConfig:
        cid = camera_id or self.active_camera
        for c in self.cameras:
            if c.camera_id == cid:
                return c
        raise KeyError(f"配置中没有摄像头 {cid}")


def load_config(path: str | Path) -> AppConfig:
    p = resolve(path)
    with open(p, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    cfg = AppConfig.model_validate(data)
    cfg.camera()  # 校验 active_camera 存在
    return cfg
