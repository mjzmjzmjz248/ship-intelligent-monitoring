"""设备接口：船端用 Bearer 设备令牌访问，和用户登录分开。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile

from common.schemas import EventMessage, HeartbeatMessage
from common.timeutil import now_iso
from shore.auth import device_by_token
from shore.services import config as config_svc
from shore.services.events import close_offline_event, ingest_event, store_evidence

router = APIRouter(prefix="/api/device")


def current_device(request: Request, authorization: str | None = Header(None),
                   x_device_id: str | None = Header(None)) -> dict[str, Any]:
    token = authorization.split(" ", 1)[1] if authorization and authorization.startswith("Bearer ") else None
    d = device_by_token(request.app.state.ctx.db, x_device_id, token)
    if d is None:
        raise HTTPException(401, "设备认证失败")
    return d


@router.post("/heartbeat")
def heartbeat(msg: HeartbeatMessage, request: Request, device=Depends(current_device)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    db = ctx.db
    if msg.device_id != device["device_id"]:
        raise HTTPException(403, "设备 ID 与令牌不符")
    cur = config_svc.latest(db)
    reply = {"server_time": now_iso(), "config_version": cur["version"] if cur else 0}
    # 同一次启动内序号必须递增；迟到的旧心跳不能把离线设备刷新成在线
    if msg.boot_id == device["boot_id"] and device["seq"] is not None and msg.seq <= device["seq"]:
        return {**reply, "ignored": "stale_seq"}
    with db.tx() as c:
        c.execute("UPDATE devices SET boot_id=?, seq=?, last_seen_at=?, health=?, applied_config_version=? WHERE device_id=?",
                  (msg.boot_id, msg.seq, now_iso(), json.dumps(msg.health, ensure_ascii=False),
                   msg.applied_config_version, device["device_id"]))
        if device["link_status"] == "NEVER":
            c.execute("UPDATE devices SET link_status='ONLINE' WHERE device_id=?", (device["device_id"],))
            c.execute("INSERT INTO device_link_log(device_id, status, at) VALUES(?,?,?)",
                      (device["device_id"], "ONLINE", now_iso()))
    if device["link_status"] == "OFFLINE":
        close_offline_event(db, device)
    return reply


@router.get("/config")
def get_config(request: Request, device=Depends(current_device)) -> dict[str, Any]:
    cur = config_svc.latest(request.app.state.ctx.db)
    if cur is None:
        raise HTTPException(404, "尚无配置")
    return {"version": cur["version"], "content": cur["content"]}


@router.post("/events")
def post_event(msg: EventMessage, request: Request, device=Depends(current_device)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    sp = ctx.shore_params()
    return ingest_event(ctx.db, device, msg, sp.late_window_sec, sp.captain_deadline_sec)


MAX_EVIDENCE = {"snapshot": 5 * 1024 * 1024, "video": 50 * 1024 * 1024}


@router.put("/evidence/{evidence_id}")
async def put_evidence(evidence_id: str, request: Request, device=Depends(current_device),
                       x_event_id: str = Header(...), x_kind: str = Header(...), x_md5: str = Header(...),
                       x_status: str | None = Header(None), x_missing_reason: str | None = Header(None),
                       x_time_range: str | None = Header(None)) -> dict[str, Any]:
    data = await request.body()
    if len(data) > MAX_EVIDENCE.get(x_kind, 0):
        raise HTTPException(413, "证据文件过大")
    ctx = request.app.state.ctx
    return store_evidence(ctx.db, device, evidence_id, x_event_id, x_kind, x_md5, data, ctx.storage, x_status,
                          json.loads(x_missing_reason) if x_missing_reason else None,
                          json.loads(x_time_range) if x_time_range else None)


@router.post("/preview")
async def post_preview(request: Request, image: UploadFile = File(...), info: str = Form(...),
                       device=Depends(current_device)) -> dict[str, Any]:
    data = await image.read()
    if len(data) > 300 * 1024:
        raise HTTPException(413, "预览图过大")
    meta = json.loads(info)
    request.app.state.ctx.previews[meta["camera_id"]] = (data, meta, now_iso())
    return {"ok": True}
