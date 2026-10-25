"""岸端数据库（SQLite）。单连接 + 锁，满足单进程演示；后续可换 PostgreSQL。"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vessels (
    vessel_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ship_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    vessel_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    boot_id TEXT,
    seq INTEGER,
    last_seen_at TEXT,
    health TEXT,
    applied_config_version INTEGER DEFAULT 0,
    link_status TEXT DEFAULT 'NEVER',          -- NEVER / ONLINE / OFFLINE
    offline_event_id TEXT
);
CREATE TABLE IF NOT EXISTS cameras (
    camera_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    name TEXT NOT NULL,
    scene TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,                       -- EDGE / SHORE
    device_id TEXT NOT NULL,
    vessel_id TEXT NOT NULL,
    camera_id TEXT,
    scene TEXT,
    alarm_type TEXT NOT NULL,
    subtype TEXT,
    risk TEXT NOT NULL,
    condition_status TEXT NOT NULL,
    handling_status TEXT NOT NULL DEFAULT 'PENDING',
    latest_revision INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    sound_started_at TEXT,
    recovered_at TEXT,
    ended_reason TEXT,
    received_at TEXT NOT NULL,
    is_backfill INTEGER NOT NULL DEFAULT 0,
    business_day TEXT NOT NULL,
    profile TEXT,
    rule_version INTEGER,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(triggered_at);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(vessel_id, alarm_type, business_day);
CREATE TABLE IF NOT EXISTS event_revisions (
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (event_id, revision)
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,                       -- 船端报告的状态，或 VERIFIED（岸端已收到且 MD5 一致）
    edge_status TEXT,
    md5 TEXT,
    size INTEGER,
    path TEXT,
    missing_reason TEXT,
    time_range TEXT,
    received_at TEXT
);
CREATE TABLE IF NOT EXISTS event_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT UNIQUE,
    event_id TEXT NOT NULL,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    role TEXT NOT NULL,                         -- captain / manager
    level TEXT NOT NULL,                        -- HIGH_RISK / REPEAT_2 / REPEAT_3 / CAPTAIN_TIMEOUT / DUTYROOM
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deadline_at TEXT,
    realtime INTEGER NOT NULL DEFAULT 1,        -- 0 表示补传的历史事件，不弹实时提醒
    read_at TEXT,
    escalated INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_id, role, level)
);
CREATE TABLE IF NOT EXISTS device_link_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    status TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """事务：块内全部成功才提交。"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
        return r[0] if r else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def loads(s: str | None) -> Any:
    return json.loads(s) if s else None
