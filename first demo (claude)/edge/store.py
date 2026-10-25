"""船端本地事件库：事件、证据、待上传队列（outbox）。

事件的每次状态变化生成一个新 revision，并在同一事务里写入 outbox，保证程序崩溃后不丢。
多线程共用一个连接，所有操作加锁。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from common.timeutil import now_iso, parse_iso, to_iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    alarm_type TEXT NOT NULL,
    risk TEXT NOT NULL,
    condition_status TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT,
    md5 TEXT,
    size INTEGER,
    status TEXT NOT NULL,
    missing_reason TEXT,
    time_range TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    task_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- event / evidence
    ref_id TEXT NOT NULL,          -- event_id 或 evidence_id
    priority INTEGER NOT NULL,     -- 越小越先发
    payload TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_at TEXT NOT NULL,
    last_error TEXT,
    state TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING / DONE / DEAD
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(state, priority, next_try_at);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

PRIORITY_EVENT = 10
PRIORITY_SNAPSHOT = 20
PRIORITY_VIDEO = 30


class EdgeStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 事件 ----
    def save_event_revision(self, payload: dict[str, Any]) -> int:
        """写入事件的新 revision 并入队上传。payload 里的 revision 由本函数设置。"""
        with self._lock:
            row = self._conn.execute("SELECT revision FROM events WHERE event_id=?",
                                     (payload["event_id"],)).fetchone()
            revision = 1 if row is None else row["revision"] + 1
            payload = {**payload, "revision": revision}
            body = json.dumps(payload, ensure_ascii=False)
            now = now_iso()
            with self._conn:
                self._conn.execute(
                    "INSERT INTO events(event_id, revision, alarm_type, risk, condition_status, triggered_at, payload, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET revision=excluded.revision,"
                    " condition_status=excluded.condition_status, payload=excluded.payload, updated_at=excluded.updated_at",
                    (payload["event_id"], revision, payload["alarm_type"], payload["risk"],
                     payload["condition_status"], payload["triggered_at"], body, now))
                self._enqueue("event", payload["event_id"], PRIORITY_EVENT, body)
            return revision

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM events WHERE event_id=?", (event_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT payload FROM events ORDER BY triggered_at DESC LIMIT ?",
                                      (limit,)).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def open_events(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM events WHERE condition_status IN ('ACTIVE','UNKNOWN')").fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def count_events(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # ---- 证据 ----
    def add_evidence(self, event_id: str, kind: str, status: str, missing_reason: str | None = None) -> str:
        evidence_id = str(uuid.uuid4())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO evidence(evidence_id, event_id, kind, status, missing_reason, created_at) VALUES(?,?,?,?,?,?)",
                (evidence_id, event_id, kind, status, missing_reason, now_iso()))
        return evidence_id

    def finalize_evidence(self, evidence_id: str, *, status: str, path: str | None, md5: str | None,
                          size: int | None, missing_reason: str | None = None,
                          time_range: list[str | None] | None = None) -> None:
        """证据定稿。有文件时同时入队上传。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE evidence SET status=?, path=?, md5=?, size=?, missing_reason=?, time_range=? WHERE evidence_id=?",
                (status, path, md5, size, missing_reason, json.dumps(time_range) if time_range else None, evidence_id))
            if path:
                kind = self._conn.execute("SELECT kind FROM evidence WHERE evidence_id=?",
                                          (evidence_id,)).fetchone()["kind"]
                prio = PRIORITY_SNAPSHOT if kind == "snapshot" else PRIORITY_VIDEO
                self._enqueue("evidence", evidence_id, prio, None)

    def fail_pending_evidence(self, reason: str) -> list[str]:
        """启动时调用：上次运行中断的采集/编码任务不会再完成，标记失败。返回受影响的事件。"""
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT DISTINCT event_id FROM evidence WHERE status='PENDING'").fetchall()
            self._conn.execute("UPDATE evidence SET status='FAILED', missing_reason=? WHERE status='PENDING'", (reason,))
        return [r["event_id"] for r in rows]

    def evidence_for(self, event_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM evidence WHERE event_id=? ORDER BY kind DESC",
                                      (event_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["time_range"] = json.loads(d["time_range"]) if d["time_range"] else None
            out.append(d)
        return out

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["time_range"] = json.loads(d["time_range"]) if d["time_range"] else None
        return d

    # ---- outbox ----
    def _enqueue(self, kind: str, ref_id: str, priority: int, payload: str | None) -> None:
        now = now_iso()
        self._conn.execute(
            "INSERT INTO outbox(task_id, kind, ref_id, priority, payload, next_try_at, created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), kind, ref_id, priority, payload, now, now))

    def due_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        now = now_iso()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE state='PENDING' AND next_try_at<=? ORDER BY priority, created_at LIMIT ?",
                (now, limit)).fetchall()
        return [dict(r) for r in rows]

    def mark_done(self, task_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE outbox SET state='DONE', done_at=?, last_error=NULL WHERE task_id=?",
                               (now_iso(), task_id))

    def mark_retry(self, task_id: str, error: str) -> float:
        """失败后按 1、2、4、8、16、30 秒退避，之后每 30 秒重试。返回下次等待秒数。"""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT attempts FROM outbox WHERE task_id=?", (task_id,)).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            delay = min(30.0, 2 ** (attempts - 1))
            nxt_iso = to_iso(utcnow() + timedelta(seconds=delay))
            self._conn.execute("UPDATE outbox SET attempts=?, next_try_at=?, last_error=? WHERE task_id=?",
                               (attempts, nxt_iso, error[:500], task_id))
        return delay

    def mark_dead(self, task_id: str, error: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE outbox SET state='DEAD', last_error=? WHERE task_id=?", (error[:500], task_id))

    def outbox_stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) n FROM outbox GROUP BY state").fetchall()
        stats = {"PENDING": 0, "DONE": 0, "DEAD": 0}
        stats.update({r["state"]: r["n"] for r in rows})
        return stats

    def oldest_pending_age_sec(self) -> float | None:
        with self._lock:
            row = self._conn.execute("SELECT MIN(created_at) m FROM outbox WHERE state='PENDING'").fetchone()
        if not row or not row["m"]:
            return None
        return (utcnow() - parse_iso(row["m"])).total_seconds()

    # ---- 键值 ----
    def kv_get(self, k: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, k: str, v: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO kv(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
