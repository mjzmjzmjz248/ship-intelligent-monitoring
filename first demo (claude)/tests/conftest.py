import hashlib
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from common.timeutil import to_iso, utcnow
from shore.bootstrap import init
from shore.db import Database
from shore.main import create_app

TEST_PW = "test-only-pw"
DEVICE = {"Authorization": "Bearer demo-device-token", "X-Device-Id": "demo-edge-001"}


@pytest.fixture
def shore(tmp_path):
    app = create_app(db_path=tmp_path / "shore.db", storage=tmp_path / "evidence", run_scheduler=False)
    init(app.state.ctx.db, "configs/demo.yaml", TEST_PW, None)
    with TestClient(app) as client:
        yield client


def login(client, user="operator"):
    r = client.post("/api/auth/login", json={"username": user, "password": TEST_PW})
    assert r.status_code == 200, r.text
    return client


def make_event(alarm="A1_BRIDGE_ABSENCE", risk="HIGH", revision=1, event_id=None, triggered_ago=0.0,
               status="ACTIVE", evidence=None):
    t = utcnow() - timedelta(seconds=triggered_ago)
    return {
        "event_id": event_id or str(uuid.uuid4()), "revision": revision,
        "device_id": "demo-edge-001", "vessel_id": "demo-vessel-001", "camera_id": "bridge-cam-01",
        "scene": "驾驶室", "session_id": "s1", "alarm_type": alarm, "risk": risk, "condition_status": status,
        "started_at": to_iso(t - timedelta(seconds=10)), "condition_satisfied_at": to_iso(t),
        "triggered_at": to_iso(t), "profile": "demo", "rule_version": 1,
        "rule_params": {"trigger_sec": 10}, "source_kind": {"video": "SYNTHETIC", "telemetry": "SIMULATED"},
        "evidence": evidence or [],
    }


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


@pytest.fixture
def mem_db():
    return Database(":memory:")
