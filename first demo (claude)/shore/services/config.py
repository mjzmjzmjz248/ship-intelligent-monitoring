"""规则参数与 ROI 的版本管理。每次修改生成新版本，船端在心跳里回报已应用的版本。"""
from __future__ import annotations

import json
from typing import Any

from common.config import AppConfig, RoiConfig, RuleParams, ShoreConfig, load_config
from common.enums import AlarmType
from common.timeutil import now_iso
from shore.db import Database
from shore.services.events import ServiceError

PROFILES = {"demo": "configs/demo.yaml", "standard": "configs/standard.yaml"}


def content_from_config(cfg: AppConfig) -> dict[str, Any]:
    return {"profile": cfg.profile,
            "rules": {a.value: p.model_dump(mode="json") for a, p in cfg.rules.items()},
            "rois": {c.camera_id: c.roi.model_dump(mode="json") for c in cfg.cameras},
            "shore": cfg.shore.model_dump(mode="json")}


def validate_content(content: dict[str, Any]) -> dict[str, Any]:
    try:
        rules = {AlarmType(k).value: RuleParams.model_validate(v).model_dump(mode="json")
                 for k, v in content["rules"].items()}
        rois = {cid: RoiConfig.model_validate(v).model_dump(mode="json") for cid, v in content["rois"].items()}
        shore = ShoreConfig.model_validate(content["shore"]).model_dump(mode="json")
    except Exception as e:  # noqa: BLE001
        raise ServiceError(422, f"配置无效：{e}") from e
    return {"profile": content.get("profile", "custom"), "rules": rules, "rois": rois, "shore": shore}


def latest(db: Database) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM config_versions ORDER BY version DESC LIMIT 1")
    if row:
        row["content"] = json.loads(row["content"])
    return row


def publish(db: Database, content: dict[str, Any], user: str, note: str | None = None) -> int:
    content = validate_content(content)
    with db.tx() as c:
        cur = c.execute("INSERT INTO config_versions(profile, content, created_at, created_by, note) VALUES(?,?,?,?,?)",
                        (content["profile"], json.dumps(content, ensure_ascii=False), now_iso(), user, note))
        return int(cur.lastrowid)


def switch_profile(db: Database, profile: str, user: str) -> int:
    """切换参数套：规则取对应配置文件，ROI 保留当前已发布的。"""
    if profile not in PROFILES:
        raise ServiceError(422, f"未知参数套 {profile}")
    cfg = load_config(PROFILES[profile])
    new = content_from_config(cfg)
    cur = latest(db)
    if cur:
        new["rois"] = cur["content"]["rois"]
    return publish(db, new, user, f"切换为 {profile} 参数")


def set_roi(db: Database, camera_id: str, roi: dict[str, Any], user: str) -> int:
    cur = latest(db)
    if not cur or camera_id not in cur["content"]["rois"]:
        raise ServiceError(404, f"未知摄像头 {camera_id}")
    content = json.loads(json.dumps(cur["content"]))
    content["rois"][camera_id] = {**content["rois"][camera_id], **roi}
    return publish(db, content, user, f"修改 {camera_id} 的 ROI")
