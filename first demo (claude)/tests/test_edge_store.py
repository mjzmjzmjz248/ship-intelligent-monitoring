"""船端本地事件库：revision 递增、事件与 outbox 同时写入、退避、重启后的未完成证据。"""
from edge.store import EdgeStore
from tests.conftest import make_event


def test_revisions_and_outbox(tmp_path):
    s = EdgeStore(tmp_path / "e.db")
    p = make_event()
    p.pop("revision")
    assert s.save_event_revision(p) == 1
    assert s.save_event_revision({**p, "condition_status": "RECOVERED"}) == 2
    assert s.get_event(p["event_id"])["revision"] == 2
    tasks = s.due_tasks()
    assert [t["kind"] for t in tasks] == ["event", "event"]
    assert s.outbox_stats()["PENDING"] == 2
    assert s.open_events() == []


def test_backoff_grows_and_caps(tmp_path):
    s = EdgeStore(tmp_path / "e.db")
    p = make_event()
    p.pop("revision")
    s.save_event_revision(p)
    tid = s.due_tasks()[0]["task_id"]
    delays = [s.mark_retry(tid, "x") for _ in range(8)]
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
    assert s.due_tasks() == []  # 退避期间不会立即再发


def test_pending_evidence_failed_after_restart(tmp_path):
    s = EdgeStore(tmp_path / "e.db")
    eid = s.add_evidence("ev1", "video", "PENDING")
    s.close()
    s2 = EdgeStore(tmp_path / "e.db")
    assert s2.fail_pending_evidence("上次运行退出时尚未完成") == ["ev1"]
    e = s2.get_evidence(eid)
    assert e["status"] == "FAILED" and "尚未完成" in e["missing_reason"]
