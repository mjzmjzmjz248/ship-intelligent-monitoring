"""岸端初始化：演示船、船端设备、摄像头、账号、初始配置版本。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from common.config import load_config
from common.enums import Role
from shore.auth import create_user, hash_token
from shore.db import Database
from shore.services import config as config_svc

ACCOUNTS = [
    ("admin", "系统管理员", Role.ADMIN),
    ("operator", "岸基值班员", Role.OPERATOR),
    ("captain", "船长（演示）", Role.CAPTAIN),
    ("manager", "企业负责人（演示）", Role.MANAGER),
    ("viewer", "只读用户", Role.VIEWER),
]


def init(db: Database, config_path: str, password: str | None, accounts_file: Path | None) -> dict[str, str]:
    cfg = load_config(config_path)
    token = os.environ.get(cfg.device.token_env, cfg.device.default_token)
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO vessels(vessel_id, name, ship_type) VALUES(?,?,?)",
                  (cfg.vessel.vessel_id, cfg.vessel.name, cfg.vessel.ship_type))
        c.execute("INSERT INTO devices(device_id, vessel_id, token_hash) VALUES(?,?,?)"
                  " ON CONFLICT(device_id) DO UPDATE SET vessel_id=excluded.vessel_id, token_hash=excluded.token_hash",
                  (cfg.device.device_id, cfg.vessel.vessel_id, hash_token(token)))
        for cam in cfg.cameras:
            c.execute("INSERT OR REPLACE INTO cameras(camera_id, device_id, name, scene) VALUES(?,?,?,?)",
                      (cam.camera_id, cfg.device.device_id, cam.name, cam.scene))
    if config_svc.latest(db) is None:
        config_svc.publish(db, config_svc.content_from_config(cfg), "init", f"初始化（{cfg.profile}）")
    creds = {}
    for username, name, role in ACCOUNTS:
        pw = password or secrets.token_urlsafe(9)
        create_user(db, username, name, role, pw)
        creds[username] = pw
    if accounts_file:
        accounts_file.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# 演示账号（本地测试用，勿提交）"] + [f"{u}\t{creds[u]}\t{n}" for u, n, _ in ACCOUNTS]
        accounts_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return creds

