"""网页使用的用户接口：登录、告警查询与处置、设备、通知、配置、统计。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from common.enums import ALARM_NAMES, AlarmType, Role
from common.timeutil import business_day, now_iso, parse_iso, utcnow
from shore.auth import CAN_CONFIG, CAN_HANDLE, end_session, new_session, session_user, verify_login
from shore.db import loads
from shore.services import config as config_svc
from shore.services.events import apply_action

router = APIRouter(prefix="/api")
COOKIE = "sid"


def current_user(request: Request, sid: str | None = Cookie(None)) -> dict[str, Any]:
    u = session_user(request.app.state.ctx.db, sid)
    if u is None:
        raise HTTPException(401, "未登录或登录已过期")
    return u


def require(roles: set[Role]):
    def dep(user=Depends(current_user)) -> dict[str, Any]:
        if Role(user["role"]) not in roles:
            raise HTTPException(403, "当前角色无此权限")
        return user
    return dep


# ---------------- 登录 ----------------
class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    db = request.app.state.ctx.db
    u = verify_login(db, body.username, body.password)
    if u is None:
        raise HTTPException(401, "用户名或密码错误")
    token = new_session(db, u["username"])
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax")
    return {"username": u["username"], "display_name": u["display_name"], "role": u["role"]}


@router.post("/auth/logout")
def logout(request: Request, response: Response, sid: str | None = Cookie(None)) -> dict[str, Any]:
    end_session(request.app.state.ctx.db, sid)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/me")
def me(user=Depends(current_user)) -> dict[str, Any]:
    return user


# ---------------- 告警 ----------------
def _event_summary(e: dict[str, Any]) -> dict[str, Any]:
    p = json.loads(e["payload"])
    return {k: e[k] for k in ("event_id", "origin", "device_id", "vessel_id", "camera_id", "scene", "alarm_type",
                              "subtype", "risk", "condition_status", "handling_status", "latest_revision",
                              "started_at", "triggered_at", "sound_started_at", "recovered_at", "ended_reason",
                              "received_at", "is_backfill", "profile", "rule_version")} | {
        "alarm_name": ALARM_NAMES.get(AlarmType(e["alarm_type"]), e["alarm_type"]),
        "source_kind": p.get("source_kind"),
    }


@router.get("/events")
def list_events(request: Request, alarm_type: str | None = None, risk: str | None = None,
                condition_status: str | None = None, handling_status: str | None = None,
                backfill: int | None = None, limit: int = 20, offset: int = 0,
                user=Depends(current_user)) -> dict[str, Any]:
    db = request.app.state.ctx.db
    where, params = [], []
    for col, val in (("alarm_type", alarm_type), ("risk", risk), ("condition_status", condition_status),
                     ("handling_status", handling_status)):
        if val:
            where.append(f"{col}=?")
            params.append(val)
    if backfill is not None:
        where.append("is_backfill=?")
        params.append(int(backfill))
    w = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(limit, 100))
    total = db.scalar(f"SELECT COUNT(*) FROM events{w}", tuple(params))
    rows = db.all(f"SELECT * FROM events{w} ORDER BY triggered_at DESC LIMIT ? OFFSET ?", (*params, limit, offset))
    items = []
    for r in rows:
        s = _event_summary(r)
        ev = db.all("SELECT kind, status FROM evidence WHERE event_id=?", (r["event_id"],))
        s["evidence"] = {e["kind"]: e["status"] for e in ev}
        items.append(s)
    return {"total": total, "items": items}


@router.get("/events/{event_id}")
def event_detail(event_id: str, request: Request, user=Depends(current_user)) -> dict[str, Any]:
    db = request.app.state.ctx.db
    e = db.one("SELECT * FROM events WHERE event_id=?", (event_id,))
    if e is None:
        raise HTTPException(404, "事件不存在")
    out = _event_summary(e)
    out["payload"] = json.loads(e["payload"])
    out["revisions"] = [
        {"revision": r["revision"], "received_at": r["received_at"],
         "condition_status": loads(r["payload"]).get("condition_status"),
         "reminder_count": loads(r["payload"]).get("reminder_count")}
        for r in db.all("SELECT * FROM event_revisions WHERE event_id=? ORDER BY revision", (event_id,))]
    out["evidence"] = [{**ev, "time_range": loads(ev["time_range"]), "path": None}
                       for ev in db.all("SELECT * FROM evidence WHERE event_id=? ORDER BY kind DESC", (event_id,))]
    out["actions"] = db.all("SELECT * FROM event_actions WHERE event_id=? ORDER BY id", (event_id,))
    out["notifications"] = db.all("SELECT * FROM notifications WHERE event_id=? ORDER BY id", (event_id,))
    return out


class ActionBody(BaseModel):
    action: str
    note: str | None = None
    action_id: str


@router.post("/events/{event_id}/actions")
def event_action(event_id: str, body: ActionBody, request: Request,
                 user=Depends(require(CAN_HANDLE))) -> dict[str, Any]:
    return apply_action(request.app.state.ctx.db, event_id, user, body.action, body.note, body.action_id)


@router.get("/evidence/{evidence_id}/file")
def evidence_file(evidence_id: str, request: Request, user=Depends(current_user)):
    ctx = request.app.state.ctx
    ev = ctx.db.one("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,))
    if not ev or not ev["path"]:
        raise HTTPException(404, "证据文件不存在或尚未上传")
    path = (ctx.storage / ev["path"]).resolve()
    if ctx.storage.resolve() not in path.parents or not path.exists():
        raise HTTPException(404, "证据文件不存在")
    media = "image/jpeg" if ev["kind"] == "snapshot" else "video/mp4"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=3600"})


# ---------------- 设备与预览 ----------------
@router.get("/devices")
def devices(request: Request, user=Depends(current_user)) -> list[dict[str, Any]]:
    ctx = request.app.state.ctx
    cur = config_svc.latest(ctx.db)
    out = []
    for d in ctx.db.all("SELECT * FROM devices"):
        d.pop("token_hash", None)
        d["health"] = loads(d["health"])
        last = parse_iso(d["last_seen_at"])
        d["last_seen_age_sec"] = (utcnow() - last).total_seconds() if last else None
        d["latest_config_version"] = cur["version"] if cur else 0
        out.append(d)
    return out


@router.get("/cameras")
def cameras(request: Request, user=Depends(current_user)) -> list[dict[str, Any]]:
    ctx = request.app.state.ctx
    cams = ctx.db.all("SELECT * FROM cameras")
    for c in cams:
        p = ctx.previews.get(c["camera_id"])
        c["preview"] = None
        if p:
            age = (utcnow() - parse_iso(p[1]["captured_at"])).total_seconds()
            c["preview"] = {**p[1], "received_at": p[2], "age_sec": age}
    return cams


@router.get("/cameras/{camera_id}/preview.jpg")
def preview(camera_id: str, request: Request, user=Depends(current_user)) -> Response:
    p = request.app.state.ctx.previews.get(camera_id)
    if p is None:
        raise HTTPException(404, "暂无画面")
    return Response(p[0], media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------------- 通知 ----------------
@router.get("/notifications")
def notifications(request: Request, unread: int = 0, user=Depends(current_user)) -> list[dict[str, Any]]:
    db = request.app.state.ctx.db
    role = user["role"]
    where = "" if role in ("admin", "operator", "viewer") else " WHERE n.role=?"
    params: tuple = () if not where else (role,)
    if unread:
        where += (" AND" if where else " WHERE") + " n.read_at IS NULL"
    return db.all("SELECT n.*, e.alarm_type, e.risk, e.triggered_at, e.handling_status, e.is_backfill"
                  f" FROM notifications n JOIN events e USING(event_id){where} ORDER BY n.id DESC LIMIT 100", params)


@router.post("/notifications/{nid}/read")
def read_notification(nid: int, request: Request, user=Depends(current_user)) -> dict[str, Any]:
    with request.app.state.ctx.db.tx() as c:
        c.execute("UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL", (now_iso(), nid))
    return {"ok": True}


# ---------------- 配置 ----------------
@router.get("/config")
def get_config(request: Request, user=Depends(current_user)) -> dict[str, Any]:
    db = request.app.state.ctx.db
    cur = config_svc.latest(db)
    history = db.all("SELECT version, profile, created_at, created_by, note FROM config_versions ORDER BY version DESC LIMIT 20")
    applied = db.all("SELECT device_id, applied_config_version FROM devices")
    return {"current": cur, "history": history, "applied": applied,
            "alarm_names": {a.value: n for a, n in ALARM_NAMES.items()}}


class RulesBody(BaseModel):
    rules: dict[str, dict[str, Any]]
    note: str | None = None


@router.put("/config/rules")
def put_rules(body: RulesBody, request: Request, user=Depends(require(CAN_CONFIG))) -> dict[str, Any]:
    db = request.app.state.ctx.db
    cur = config_svc.latest(db)
    content = {**cur["content"], "rules": body.rules, "profile": "custom"}
    return {"version": config_svc.publish(db, content, user["username"], body.note or "修改规则参数")}


@router.post("/config/profile/{name}")
def switch_profile(name: str, request: Request, user=Depends(require(CAN_CONFIG))) -> dict[str, Any]:
    return {"version": config_svc.switch_profile(request.app.state.ctx.db, name, user["username"])}


class RoiBody(BaseModel):
    points: list[list[float]]
    anchor: str | None = None


@router.put("/config/roi/{camera_id}")
def put_roi(camera_id: str, body: RoiBody, request: Request, user=Depends(require(CAN_CONFIG))) -> dict[str, Any]:
    roi = {"points": body.points} | ({"anchor": body.anchor} if body.anchor else {})
    return {"version": config_svc.set_roi(request.app.state.ctx.db, camera_id, roi, user["username"])}


# ---------------- 统计 ----------------
@router.get("/stats/summary")
def stats(request: Request, user=Depends(current_user)) -> dict[str, Any]:
    db = request.app.state.ctx.db
    today = business_day(utcnow()).isoformat()
    by_type = db.all("SELECT alarm_type, COUNT(*) n FROM events WHERE business_day=? GROUP BY alarm_type", (today,))
    total = db.scalar("SELECT COUNT(*) FROM events WHERE business_day=?", (today,)) or 0
    pending = db.scalar("SELECT COUNT(*) FROM events WHERE handling_status='PENDING'") or 0
    handled = db.scalar("SELECT COUNT(*) FROM events WHERE business_day=? AND handling_status IN ('RESOLVED','FALSE_POSITIVE')",
                        (today,)) or 0
    fp = db.scalar("SELECT COUNT(*) FROM events WHERE business_day=? AND handling_status='FALSE_POSITIVE'", (today,)) or 0
    backfill = db.scalar("SELECT COUNT(*) FROM events WHERE business_day=? AND is_backfill=1", (today,)) or 0
    return {"business_day": today, "total": total, "pending": pending, "handled": handled, "false_positive": fp,
            "backfill": backfill, "handle_rate": (handled / total) if total else None,
            "false_positive_rate": (fp / handled) if handled else None,
            "by_type": {r["alarm_type"]: r["n"] for r in by_type}, "profile": request.app.state.ctx.profile()}
