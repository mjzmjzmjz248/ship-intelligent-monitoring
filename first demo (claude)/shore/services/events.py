"""事件接收、证据入库、处置动作、终端离线事件。"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from common.enums import (AlarmType, ConditionStatus, EvidenceStatus, HandlingStatus, Risk)
from common.schemas import EventMessage
from common.timeutil import business_day, now_iso, parse_iso, utcnow
from shore.db import Database
from shore.services import notify


class ServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------- 事件接收 ----------------
def ingest_event(db: Database, device: dict[str, Any], msg: EventMessage, late_window_sec: float,
                 captain_deadline_sec: float) -> dict[str, Any]:
    if msg.device_id != device["device_id"] or msg.vessel_id != device["vessel_id"]:
        raise ServiceError(403, "事件的设备或船舶与令牌不符")
    payload = msg.model_dump(mode="json")
    body = _canonical(payload)
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    old = db.one("SELECT content_hash FROM event_revisions WHERE event_id=? AND revision=?",
                 (msg.event_id, msg.revision))
    if old:
        if old["content_hash"] == h:
            return {"event_id": msg.event_id, "accepted_revision": msg.revision, "duplicate": True}
        raise ServiceError(409, "同一 revision 内容不同，拒绝覆盖")

    received = now_iso()
    created = False
    with db.tx() as c:
        c.execute("INSERT INTO event_revisions(event_id, revision, content_hash, payload, received_at) VALUES(?,?,?,?,?)",
                  (msg.event_id, msg.revision, h, body, received))
        ev = c.execute("SELECT latest_revision FROM events WHERE event_id=?", (msg.event_id,)).fetchone()
        if ev is None:
            triggered = parse_iso(msg.triggered_at)
            late = (utcnow() - triggered).total_seconds() > late_window_sec
            c.execute(
                "INSERT INTO events(event_id, origin, device_id, vessel_id, camera_id, scene, alarm_type, subtype, risk,"
                " condition_status, latest_revision, started_at, triggered_at, sound_started_at, recovered_at,"
                " ended_reason, received_at, is_backfill, business_day, profile, rule_version, payload)"
                " VALUES(?, 'EDGE', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg.event_id, msg.device_id, msg.vessel_id, msg.camera_id, msg.scene, msg.alarm_type.value,
                 msg.subtype, msg.risk.value, msg.condition_status.value, msg.revision, msg.started_at,
                 msg.triggered_at, msg.sound_started_at, msg.recovered_at, msg.ended_reason, received, int(late),
                 business_day(triggered).isoformat(), msg.profile, msg.rule_version, body))
            created = True
        elif msg.revision > ev["latest_revision"]:
            # 只更新监测相关字段；处置状态只能由用户操作修改
            c.execute("UPDATE events SET condition_status=?, latest_revision=?, sound_started_at=?, recovered_at=?,"
                      " ended_reason=?, subtype=?, payload=? WHERE event_id=?",
                      (msg.condition_status.value, msg.revision, msg.sound_started_at, msg.recovered_at,
                       msg.ended_reason, msg.subtype, body, msg.event_id))
        # 旧 revision 迟到：只留在历史里，不回滚状态
        if ev is None or msg.revision >= ev["latest_revision"]:
            for e in msg.evidence:
                c.execute(
                    "INSERT INTO evidence(evidence_id, event_id, kind, status, edge_status, md5, size, missing_reason, time_range)"
                    " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(evidence_id) DO UPDATE SET"
                    " edge_status=excluded.edge_status, missing_reason=excluded.missing_reason,"
                    " time_range=excluded.time_range,"
                    " md5=CASE WHEN evidence.status='VERIFIED' THEN evidence.md5 ELSE excluded.md5 END,"
                    " size=CASE WHEN evidence.status='VERIFIED' THEN evidence.size ELSE excluded.size END,"
                    " status=CASE WHEN evidence.status='VERIFIED' THEN 'VERIFIED' ELSE excluded.status END",
                    (e.evidence_id, msg.event_id, e.kind, e.status.value, e.status.value, e.md5, e.size,
                     e.missing_reason, json.dumps(e.time_range) if e.time_range else None))
    if created:
        notify.on_new_event(db, get_event_row(db, msg.event_id), captain_deadline_sec)
    return {"event_id": msg.event_id, "accepted_revision": msg.revision, "duplicate": False, "created": created}


def get_event_row(db: Database, event_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM events WHERE event_id=?", (event_id,))


# ---------------- 证据 ----------------
def store_evidence(db: Database, device: dict[str, Any], evidence_id: str, event_id: str, kind: str,
                   claimed_md5: str, data: bytes, storage: Path, edge_status: str | None,
                   missing_reason: str | None, time_range: list | None) -> dict[str, Any]:
    ev = get_event_row(db, event_id)
    if ev is None:
        raise ServiceError(409, "事件尚未到达，请稍后重传")
    if ev["vessel_id"] != device["vessel_id"]:
        raise ServiceError(403, "证据不属于本设备的船舶")
    if kind not in ("snapshot", "video"):
        raise ServiceError(422, "kind 只能是 snapshot 或 video")
    actual = hashlib.md5(data).hexdigest()
    if actual != claimed_md5:
        raise ServiceError(422, f"MD5 校验失败：声明 {claimed_md5}，实际 {actual}")
    old = db.one("SELECT status, md5 FROM evidence WHERE evidence_id=?", (evidence_id,))
    if old and old["status"] == EvidenceStatus.VERIFIED.value:
        if old["md5"] == actual:
            return {"evidence_id": evidence_id, "md5": actual, "duplicate": True}
        raise ServiceError(409, "该证据已存在且内容不同")
    ext = ".jpg" if kind == "snapshot" else ".mp4"
    rel = Path(ev["business_day"]) / event_id / f"{evidence_id}{ext}"
    path = storage / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    with db.tx() as c:
        c.execute(
            "INSERT INTO evidence(evidence_id, event_id, kind, status, edge_status, md5, size, path, missing_reason,"
            " time_range, received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(evidence_id) DO UPDATE SET"
            " status=excluded.status, edge_status=excluded.edge_status, md5=excluded.md5, size=excluded.size,"
            " path=excluded.path, missing_reason=excluded.missing_reason, time_range=excluded.time_range,"
            " received_at=excluded.received_at",
            (evidence_id, event_id, kind, EvidenceStatus.VERIFIED.value, edge_status, actual, len(data),
             str(rel), missing_reason, json.dumps(time_range) if time_range else None, now_iso()))
    return {"evidence_id": evidence_id, "md5": actual, "duplicate": False}


# ---------------- 处置 ----------------
ACTIONS = {
    # 动作: (允许的当前状态, 目标状态)
    "confirm": ({HandlingStatus.PENDING}, HandlingStatus.CONFIRMED),
    "start": ({HandlingStatus.CONFIRMED}, HandlingStatus.PROCESSING),
    "resolve": ({HandlingStatus.CONFIRMED, HandlingStatus.PROCESSING}, HandlingStatus.RESOLVED),
    "false_positive": ({HandlingStatus.PENDING, HandlingStatus.CONFIRMED, HandlingStatus.PROCESSING},
                       HandlingStatus.FALSE_POSITIVE),
}


def apply_action(db: Database, event_id: str, user: dict[str, Any], action: str, note: str | None,
                 action_id: str) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ServiceError(422, f"未知操作 {action}")
    done = db.one("SELECT * FROM event_actions WHERE action_id=?", (action_id,))
    if done:  # 重复点击：返回已有结果
        return {"duplicate": True, "handling_status": done["to_status"]}
    allowed, target = ACTIONS[action]
    with db.tx() as c:
        ev = c.execute("SELECT handling_status FROM events WHERE event_id=?", (event_id,)).fetchone()
        if ev is None:
            raise ServiceError(404, "事件不存在")
        cur = HandlingStatus(ev["handling_status"])
        if cur not in allowed:
            raise ServiceError(409, f"当前状态 {cur.value} 不能执行 {action}")
        c.execute("UPDATE events SET handling_status=? WHERE event_id=?", (target.value, event_id))
        c.execute("INSERT INTO event_actions(action_id, event_id, username, role, action, from_status, to_status, note, at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  (action_id, event_id, user["username"], user["role"], action, cur.value, target.value, note,
                   now_iso()))
    return {"duplicate": False, "handling_status": target.value}


# ---------------- 终端离线（岸端判断） ----------------
def open_offline_event(db: Database, device: dict[str, Any], risk: Risk, profile: str, basis: str,
                       captain_deadline_sec: float) -> str:
    event_id = str(uuid.uuid4())
    now = now_iso()
    started = device["last_seen_at"] or now
    payload = {"event_id": event_id, "origin": "SHORE", "alarm_type": AlarmType.A4_TERMINAL_OFFLINE.value,
               "reason": "超过时限未收到心跳；原因可能是断电、程序退出或网络中断，无法仅凭心跳区分",
               "last_seen_at": device["last_seen_at"], "profile": profile, "basis": basis}
    body = _canonical(payload)
    with db.tx() as c:
        c.execute(
            "INSERT INTO events(event_id, origin, device_id, vessel_id, alarm_type, risk, condition_status,"
            " latest_revision, started_at, triggered_at, received_at, business_day, profile, payload)"
            " VALUES(?, 'SHORE', ?,?,?,?,?, 1, ?,?,?,?,?,?)",
            (event_id, device["device_id"], device["vessel_id"], AlarmType.A4_TERMINAL_OFFLINE.value, risk.value,
             ConditionStatus.ACTIVE.value, started, now, now, business_day(utcnow()).isoformat(), profile, body))
        c.execute("INSERT INTO event_revisions(event_id, revision, content_hash, payload, received_at) VALUES(?,?,?,?,?)",
                  (event_id, 1, hashlib.sha256(body.encode()).hexdigest(), body, now))
        for kind in ("snapshot", "video"):
            c.execute("INSERT INTO evidence(evidence_id, event_id, kind, status, missing_reason) VALUES(?,?,?,?,?)",
                      (str(uuid.uuid4()), event_id, kind, EvidenceStatus.NOT_APPLICABLE.value, "终端离线，无船端画面"))
        c.execute("UPDATE devices SET link_status='OFFLINE', offline_event_id=? WHERE device_id=?",
                  (event_id, device["device_id"]))
        c.execute("INSERT INTO device_link_log(device_id, status, at) VALUES(?,?,?)", (device["device_id"], "OFFLINE", now))
    notify.on_new_event(db, get_event_row(db, event_id), captain_deadline_sec)
    return event_id


def close_offline_event(db: Database, device: dict[str, Any]) -> None:
    event_id = device["offline_event_id"]
    now = now_iso()
    with db.tx() as c:
        if event_id:
            row = c.execute("SELECT latest_revision, payload FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row:
                rev = row["latest_revision"] + 1
                payload = {**json.loads(row["payload"]), "recovered_at": now, "condition_status": "RECOVERED"}
                body = _canonical(payload)
                c.execute("UPDATE events SET condition_status=?, recovered_at=?, latest_revision=?, payload=? WHERE event_id=?",
                          (ConditionStatus.RECOVERED.value, now, rev, body, event_id))
                c.execute("INSERT INTO event_revisions(event_id, revision, content_hash, payload, received_at)"
                          " VALUES(?,?,?,?,?)", (event_id, rev, hashlib.sha256(body.encode()).hexdigest(), body, now))
        c.execute("UPDATE devices SET link_status='ONLINE', offline_event_id=NULL WHERE device_id=?",
                  (device["device_id"],))
        c.execute("INSERT INTO device_link_log(device_id, status, at) VALUES(?,?,?)", (device["device_id"], "ONLINE", now))
