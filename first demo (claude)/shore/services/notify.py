"""站内通知与升级，规则来自指南 2.5（第 10、11 页）：

- 高风险告警：直接通知船长和企业负责人；
- 同船同类告警在同一自然日第 2 次：通知船长，限时确警处置；
- 第 3 次，或船长到期未处置：通知企业负责人；
- 企业值班室人员离岗：直接通知企业负责人。
计数按独立事件（event_id），重复提醒和重传不计入。补传的历史事件照常计数和记录，但不弹实时提醒。
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from common.enums import AlarmType, HandlingStatus, Risk
from common.timeutil import now_iso, to_iso, utcnow
from shore.db import Database


def _add(c: sqlite3.Connection, event_id: str, role: str, level: str, reason: str, realtime: bool,
         deadline: str | None = None) -> None:
    # (event_id, role, level) 唯一：重复调用不会产生重复通知
    c.execute("INSERT OR IGNORE INTO notifications(event_id, role, level, reason, created_at, deadline_at, realtime)"
              " VALUES(?,?,?,?,?,?,?)", (event_id, role, level, reason, now_iso(), deadline, int(realtime)))


def same_day_index(db: Database, ev: dict[str, Any]) -> int:
    """该事件是同船同类同日的第几个独立事件（按发生时间排序）。"""
    return db.scalar(
        "SELECT COUNT(*) FROM events WHERE vessel_id=? AND alarm_type=? AND business_day=? AND triggered_at<=?",
        (ev["vessel_id"], ev["alarm_type"], ev["business_day"], ev["triggered_at"]))


def on_new_event(db: Database, ev: dict[str, Any], captain_deadline_sec: float) -> None:
    realtime = not ev["is_backfill"]
    n = same_day_index(db, ev)
    with db.tx() as c:
        if ev["alarm_type"] == AlarmType.A2_DUTYROOM_ABSENCE.value:
            _add(c, ev["event_id"], "manager", "DUTYROOM", "企业值班室人员离岗，直接通知企业负责人", realtime)
        if ev["risk"] == Risk.HIGH.value:
            _add(c, ev["event_id"], "captain", "HIGH_RISK", "高风险告警直接通知船长", realtime)
            _add(c, ev["event_id"], "manager", "HIGH_RISK", "高风险告警直接通知企业负责人", realtime)
        if n == 2:
            deadline = to_iso(utcnow() + timedelta(seconds=captain_deadline_sec))
            _add(c, ev["event_id"], "captain", "REPEAT_2", f"同一自然日第 2 次发生，请在 {captain_deadline_sec:.0f} 秒内确警处置",
                 realtime, deadline)
        if n >= 3:
            _add(c, ev["event_id"], "manager", "REPEAT_3", f"同一自然日第 {n} 次发生", realtime)


def check_deadlines(db: Database) -> int:
    """船长到期未处置的，升级到企业负责人。返回本次升级数量。"""
    due = db.all("SELECT n.id, n.event_id, e.handling_status, n.realtime FROM notifications n JOIN events e USING(event_id)"
                 " WHERE n.role='captain' AND n.level='REPEAT_2' AND n.escalated=0 AND n.deadline_at<=?", (now_iso(),))
    count = 0
    with db.tx() as c:
        for d in due:
            c.execute("UPDATE notifications SET escalated=1 WHERE id=?", (d["id"],))
            if d["handling_status"] == HandlingStatus.PENDING.value:
                _add(c, d["event_id"], "manager", "CAPTAIN_TIMEOUT", "船长未在时限内确警处置，升级通知企业负责人",
                     bool(d["realtime"]))
                count += 1
    return count
