"""升级通知与终端离线，对应实施方案 R12、R13 及 A4。"""
from datetime import timedelta

from common.timeutil import to_iso, utcnow
from shore.main import Scheduler
from tests.conftest import DEVICE, login, make_event


def notes(shore, event_id):
    login(shore, "admin")
    return sorted((n["role"], n["level"]) for n in shore.get(f"/api/events/{event_id}").json()["notifications"])


def test_high_risk_notifies_captain_and_manager(shore):
    ev = make_event(risk="HIGH")
    shore.post("/api/device/events", json=ev, headers=DEVICE)
    assert notes(shore, ev["event_id"]) == [("captain", "HIGH_RISK"), ("manager", "HIGH_RISK")]


def test_second_and_third_same_day(shore):
    evs = [make_event(alarm="A6_OVERSPEED", risk="MEDIUM", triggered_ago=30 - i) for i in range(3)]
    for ev in evs:
        shore.post("/api/device/events", json=ev, headers=DEVICE)
    assert notes(shore, evs[0]["event_id"]) == []
    assert notes(shore, evs[1]["event_id"]) == [("captain", "REPEAT_2")]
    assert notes(shore, evs[2]["event_id"]) == [("manager", "REPEAT_3")]
    # 同一事件重传不增加计数
    shore.post("/api/device/events", json={**evs[0], "revision": 2}, headers=DEVICE)
    ev4 = make_event(alarm="A6_OVERSPEED", risk="MEDIUM")
    shore.post("/api/device/events", json=ev4, headers=DEVICE)
    assert notes(shore, ev4["event_id"]) == [("manager", "REPEAT_3")]


def test_cross_day_resets_count(shore):
    yesterday = make_event(alarm="A6_OVERSPEED", risk="MEDIUM", triggered_ago=86400 * 1.5)
    today = make_event(alarm="A6_OVERSPEED", risk="MEDIUM")
    shore.post("/api/device/events", json=yesterday, headers=DEVICE)
    shore.post("/api/device/events", json=today, headers=DEVICE)
    assert notes(shore, today["event_id"]) == []


def test_captain_timeout_escalates_once(shore):
    ctx = shore.app.state.ctx
    a, b = (make_event(alarm="A5_AIS_OFFLINE", risk="MEDIUM", triggered_ago=10 - i) for i in range(2))
    for ev in (a, b):
        shore.post("/api/device/events", json=ev, headers=DEVICE)
    with ctx.db.tx() as c:  # 把截止时间改到过去，模拟船长超时
        c.execute("UPDATE notifications SET deadline_at=? WHERE level='REPEAT_2'", (to_iso(utcnow() - timedelta(seconds=1)),))
    s = Scheduler(ctx)
    s.tick()
    s.tick()
    assert notes(shore, b["event_id"]) == [("captain", "REPEAT_2"), ("manager", "CAPTAIN_TIMEOUT")]


def test_captain_handled_in_time_no_escalation(shore):
    ctx = shore.app.state.ctx
    a, b = (make_event(alarm="A5_AIS_OFFLINE", risk="MEDIUM", triggered_ago=10 - i) for i in range(2))
    for ev in (a, b):
        shore.post("/api/device/events", json=ev, headers=DEVICE)
    login(shore, "captain")
    shore.post(f"/api/events/{b['event_id']}/actions", json={"action": "confirm", "action_id": "x"})
    with ctx.db.tx() as c:
        c.execute("UPDATE notifications SET deadline_at=? WHERE level='REPEAT_2'", (to_iso(utcnow() - timedelta(seconds=1)),))
    Scheduler(ctx).tick()
    assert notes(shore, b["event_id"]) == [("captain", "REPEAT_2")]


def test_dutyroom_goes_to_manager(shore):
    ev = make_event(alarm="A2_DUTYROOM_ABSENCE", risk="HIGH")
    shore.post("/api/device/events", json=ev, headers=DEVICE)
    assert ("manager", "DUTYROOM") in notes(shore, ev["event_id"])


def test_backfill_marked_and_not_realtime(shore):
    ev = make_event(risk="HIGH", triggered_ago=120)
    shore.post("/api/device/events", json=ev, headers=DEVICE)
    login(shore, "admin")
    d = shore.get(f"/api/events/{ev['event_id']}").json()
    assert d["is_backfill"] == 1
    assert all(n["realtime"] == 0 for n in d["notifications"])


def hb(shore, seq, boot="boot-1"):
    msg = {"device_id": "demo-edge-001", "boot_id": boot, "seq": seq, "sent_at": to_iso(utcnow())}
    return shore.post("/api/device/heartbeat", json=msg, headers=DEVICE)


def test_terminal_offline_and_recovery(shore):
    ctx = shore.app.state.ctx
    ctx.started_mono -= 3600  # 跳过岸端启动宽限
    hb(shore, 1)
    s = Scheduler(ctx)
    s.tick()
    login(shore, "admin")
    assert shore.get("/api/events").json()["total"] == 0
    with ctx.db.tx() as c:
        c.execute("UPDATE devices SET last_seen_at=?", (to_iso(utcnow() - timedelta(seconds=60)),))
    s.tick()
    s.tick()
    items = shore.get("/api/events").json()["items"]
    assert len(items) == 1 and items[0]["alarm_type"] == "A4_TERMINAL_OFFLINE" and items[0]["origin"] == "SHORE"
    assert {e["status"] for e in shore.get(f"/api/events/{items[0]['event_id']}").json()["evidence"]} == {"NOT_APPLICABLE"}
    # 同一次启动的旧序号心跳不能让设备恢复在线
    assert hb(shore, 1).json().get("ignored") == "stale_seq"
    assert shore.get("/api/devices").json()[0]["link_status"] == "OFFLINE"
    hb(shore, 2)
    d = shore.get(f"/api/events/{items[0]['event_id']}").json()
    assert d["condition_status"] == "RECOVERED"
    assert shore.get("/api/devices").json()[0]["link_status"] == "ONLINE"
    assert shore.get("/api/events").json()["total"] == 1
