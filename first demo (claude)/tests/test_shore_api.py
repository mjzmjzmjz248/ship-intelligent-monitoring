"""岸端接口：设备认证、事件幂等、证据 MD5、处置权限、配置版本。"""
import uuid

from tests.conftest import DEVICE, login, make_event, md5


def test_device_auth_required(shore):
    assert shore.post("/api/device/events", json=make_event()).status_code == 401
    bad = {**DEVICE, "Authorization": "Bearer wrong"}
    assert shore.post("/api/device/events", json=make_event(), headers=bad).status_code == 401


def test_event_idempotent_and_conflict(shore):
    ev = make_event()
    r1 = shore.post("/api/device/events", json=ev, headers=DEVICE).json()
    r2 = shore.post("/api/device/events", json=ev, headers=DEVICE).json()
    assert r1["duplicate"] is False and r2["duplicate"] is True
    changed = {**ev, "risk": "LOW"}
    assert shore.post("/api/device/events", json=changed, headers=DEVICE).status_code == 409
    login(shore)
    assert shore.get("/api/events").json()["total"] == 1


def test_old_revision_does_not_roll_back(shore):
    ev = make_event()
    shore.post("/api/device/events", json=ev, headers=DEVICE)
    rec = {**ev, "revision": 3, "condition_status": "RECOVERED", "recovered_at": ev["triggered_at"]}
    shore.post("/api/device/events", json=rec, headers=DEVICE)
    late = {**ev, "revision": 2, "condition_status": "UNKNOWN"}
    assert shore.post("/api/device/events", json=late, headers=DEVICE).status_code == 200
    login(shore)
    d = shore.get(f"/api/events/{ev['event_id']}").json()
    assert d["condition_status"] == "RECOVERED"
    assert [r["revision"] for r in d["revisions"]] == [1, 2, 3]


def test_vessel_mismatch_rejected(shore):
    ev = {**make_event(), "vessel_id": "other"}
    assert shore.post("/api/device/events", json=ev, headers=DEVICE).status_code == 403


def put_evidence(shore, ev, data, claimed=None, kind="snapshot", eid=None):
    eid = eid or str(uuid.uuid4())
    h = {**DEVICE, "X-Event-Id": ev["event_id"], "X-Kind": kind, "X-MD5": claimed or md5(data),
         "X-Status": "READY", "Content-Type": "image/jpeg"}
    return eid, shore.put(f"/api/device/evidence/{eid}", content=data, headers=h)


def test_evidence_md5_verified_and_tamper_rejected(shore):
    ev = make_event()
    data = b"\xff\xd8fake-jpeg" * 100
    # 事件未到时证据请求返回 409，船端会稍后重传
    _, r = put_evidence(shore, ev, data)
    assert r.status_code == 409
    shore.post("/api/device/events", json=ev, headers=DEVICE)
    eid, r = put_evidence(shore, ev, data)
    assert r.status_code == 200 and r.json()["md5"] == md5(data)
    _, r = put_evidence(shore, ev, data, eid=eid)
    assert r.json()["duplicate"] is True
    _, r = put_evidence(shore, ev, data + b"x", claimed=md5(data))
    assert r.status_code == 422
    login(shore)
    d = shore.get(f"/api/events/{ev['event_id']}").json()
    assert [e["status"] for e in d["evidence"]] == ["VERIFIED"]
    f = shore.get(f"/api/evidence/{eid}/file")
    assert f.status_code == 200 and md5(f.content) == md5(data)


def test_handling_flow_and_permissions(shore):
    ev = make_event()
    shore.post("/api/device/events", json=ev, headers=DEVICE)
    url = f"/api/events/{ev['event_id']}/actions"
    login(shore, "viewer")
    assert shore.post(url, json={"action": "confirm", "action_id": "a1"}).status_code == 403
    login(shore, "operator")
    assert shore.post(url, json={"action": "resolve", "action_id": "a0"}).status_code == 409
    r = shore.post(url, json={"action": "confirm", "note": "已核实", "action_id": "a1"}).json()
    assert r["handling_status"] == "CONFIRMED"
    again = shore.post(url, json={"action": "confirm", "action_id": "a1"}).json()
    assert again["duplicate"] is True
    assert shore.post(url, json={"action": "resolve", "action_id": "a2"}).json()["handling_status"] == "RESOLVED"
    d = shore.get(f"/api/events/{ev['event_id']}").json()
    assert [a["action"] for a in d["actions"]] == ["confirm", "resolve"]
    # 船端后续 revision 不能覆盖处置状态
    shore.post("/api/device/events", json={**ev, "revision": 2, "condition_status": "RECOVERED"}, headers=DEVICE)
    assert shore.get(f"/api/events/{ev['event_id']}").json()["handling_status"] == "RESOLVED"


def test_config_versions_and_roles(shore):
    login(shore, "operator")
    assert shore.post("/api/config/profile/standard").status_code == 403
    login(shore, "admin")
    v = shore.post("/api/config/profile/standard").json()["version"]
    cur = shore.get("/api/config").json()["current"]
    assert cur["version"] == v and cur["content"]["rules"]["A6_OVERSPEED"]["trigger_sec"] == 600
    bad = shore.put("/api/config/roi/bridge-cam-01", json={"points": [[0, 0], [1, 1]]})
    assert bad.status_code == 422
    ok = shore.put("/api/config/roi/bridge-cam-01", json={"points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]})
    assert ok.json()["version"] == v + 1
    hb = {"device_id": "demo-edge-001", "boot_id": "b1", "seq": 1, "sent_at": "2026-01-01T00:00:00Z"}
    assert shore.post("/api/device/heartbeat", json=hb, headers=DEVICE).json()["config_version"] == v + 1
    cfg = shore.get("/api/device/config", headers=DEVICE).json()
    assert cfg["content"]["rois"]["bridge-cam-01"]["points"][2] == [0.5, 0.9]


def test_login_wrong_password(shore):
    r = shore.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401
    assert shore.get("/api/events").status_code == 401
