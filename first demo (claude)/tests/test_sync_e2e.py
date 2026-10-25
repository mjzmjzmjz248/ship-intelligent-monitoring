"""船端 outbox → 岸端接口的端到端测试：断网补传、ACK 丢失、MD5 篡改、重启续传。

岸端用真实 FastAPI 应用（进程内），船端用真实 EdgeStore 和 OutboxWorker。
"""
import hashlib

import httpx
import pytest

from edge.store import EdgeStore
from edge.sync import OutboxWorker, ShoreClient
from tests.conftest import login, make_event


class FlakyTransport(httpx.BaseTransport):
    """请求已被岸端处理，但响应在途中丢失（模拟 ACK 丢失）。"""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.drop_next = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = self.inner.handle_request(request)
        if self.drop_next > 0:
            self.drop_next -= 1
            raise httpx.ReadTimeout("模拟响应丢失", request=request)
        return resp


@pytest.fixture
def edge(shore, tmp_path):
    transport = FlakyTransport(shore._transport)
    client = ShoreClient("http://testserver", "demo-edge-001", "demo-device-token", transport=transport)
    store = EdgeStore(tmp_path / "edge.db")
    return store, client, OutboxWorker(store, client), transport, tmp_path


def drain(store: EdgeStore, worker: OutboxWorker, rounds: int = 10) -> None:
    for _ in range(rounds):
        with store._lock, store._conn:  # 测试中跳过退避等待
            store._conn.execute("UPDATE outbox SET next_try_at='2000-01-01T00:00:00.000Z' WHERE state='PENDING'")
        tasks = store.due_tasks(100)
        if not tasks:
            return
        for t in tasks:
            worker.process(t)


def edge_event(store: EdgeStore, tmp_path, **kw) -> tuple[str, str]:
    p = make_event(**kw)
    p.pop("revision")
    img = tmp_path / f"{p['event_id']}.jpg"
    img.write_bytes(b"\xff\xd8" + p["event_id"].encode() * 50)
    sid = store.add_evidence(p["event_id"], "snapshot", "PENDING")
    store.finalize_evidence(sid, status="READY", path=str(img), md5=hashlib.md5(img.read_bytes()).hexdigest(),
                            size=img.stat().st_size)
    p["evidence"] = [{"evidence_id": sid, "kind": "snapshot", "status": "READY",
                      "md5": hashlib.md5(img.read_bytes()).hexdigest()}]
    store.save_event_revision(p)
    return p["event_id"], sid


def shore_events(shore):
    login(shore, "admin")
    return shore.get("/api/events?limit=100").json()


def test_offline_then_backfill_without_duplicates(shore, edge):
    store, client, worker, _, tmp = edge
    client.enabled = False  # sync off
    ids = [edge_event(store, tmp, triggered_ago=60)[0] for _ in range(3)]
    for t in store.due_tasks(100):
        assert worker.process(t) is False
    assert store.outbox_stats()["PENDING"] == 6
    assert shore_events(shore)["total"] == 0

    client.enabled = True  # sync on
    drain(store, worker)
    assert store.outbox_stats() == {"PENDING": 0, "DONE": 6, "DEAD": 0}
    data = shore_events(shore)
    assert data["total"] == 3 and {i["event_id"] for i in data["items"]} == set(ids)
    assert all(i["is_backfill"] == 1 and i["evidence"] == {"snapshot": "VERIFIED"} for i in data["items"])
    drain(store, worker)  # 再同步一次也不会重复
    assert shore_events(shore)["total"] == 3


def test_ack_lost_retry_is_idempotent(shore, edge):
    store, _, worker, transport, tmp = edge
    eid, sid = edge_event(store, tmp)
    transport.drop_next = 2  # 事件和截图的响应都丢失
    for t in store.due_tasks(100):
        assert worker.process(t) is False
    assert store.outbox_stats()["PENDING"] == 2
    drain(store, worker)
    assert store.outbox_stats()["DONE"] == 2
    login(shore, "admin")
    detail = shore.get(f"/api/events/{eid}").json()
    assert len(detail["revisions"]) == 1 and len(detail["evidence"]) == 1


def test_tampered_evidence_goes_dead(shore, edge):
    store, _, worker, _, tmp = edge
    eid, sid = edge_event(store, tmp)
    path = store.get_evidence(sid)["path"]
    with open(path, "ab") as f:
        f.write(b"tampered")
    drain(store, worker)
    assert store.outbox_stats() == {"PENDING": 0, "DONE": 1, "DEAD": 1}
    login(shore, "admin")
    ev = shore.get(f"/api/events/{eid}").json()["evidence"][0]
    assert ev["status"] == "READY"  # 岸端只记录船端声明，未收到合格文件


def test_restart_keeps_pending_tasks(shore, edge):
    store, client, _, _, tmp = edge
    client.enabled = False
    edge_event(store, tmp)
    store.close()
    store2 = EdgeStore(tmp / "edge.db")
    assert store2.outbox_stats()["PENDING"] == 2
    client.enabled = True
    drain(store2, OutboxWorker(store2, client))
    assert store2.outbox_stats()["DONE"] == 2
    assert shore_events(shore)["total"] == 1


def test_revisions_sent_in_order(shore, edge):
    store, _, worker, _, tmp = edge
    eid, _ = edge_event(store, tmp)
    p = store.get_event(eid)
    store.save_event_revision({**p, "condition_status": "RECOVERED", "recovered_at": p["triggered_at"]})
    drain(store, worker)
    login(shore, "admin")
    d = shore.get(f"/api/events/{eid}").json()
    assert d["latest_revision"] == 2 and d["condition_status"] == "RECOVERED"
