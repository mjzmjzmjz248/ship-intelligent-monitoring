"""船岸同步：持久化补传、心跳、配置拉取、预览上送。

所有连接由船端主动发起。`sync off` 在本客户端层拦截全部请求（含心跳和预览），
用来真实模拟船岸断网；只关电脑 Wi-Fi 无法断开本机 localhost 通信。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from common.timeutil import now_iso
from edge.store import EdgeStore

log = logging.getLogger("edge.sync")


class SyncDisabled(Exception):
    pass


class ShoreClient:
    def __init__(self, base_url: str, device_id: str, token: str, timeout: float = 5.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.device_id = device_id
        self.enabled = True
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                    headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id})

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        if not self.enabled:
            raise SyncDisabled("同步已关闭")
        return self._client.request(method, path, **kw)


def _permanent(status: int) -> bool:
    """这些错误重试也不会成功，进入死信并在控制台显示。"""
    return status in (400, 401, 403, 404, 413, 422)


class OutboxWorker(threading.Thread):
    def __init__(self, store: EdgeStore, client: ShoreClient) -> None:
        super().__init__(name="outbox", daemon=True)
        self.store = store
        self.client = client
        self._stop = threading.Event()
        self.last_error: str | None = None
        self.sent = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if not self.client.enabled:
                self._stop.wait(1.0)
                continue
            tasks = self.store.due_tasks()
            if not tasks:
                self._stop.wait(0.5)
                continue
            for t in tasks:
                if self._stop.is_set() or not self.client.enabled:
                    break
                self.process(t)

    def process(self, task: dict[str, Any]) -> bool:
        """发送一个任务，返回是否成功。单独暴露便于测试。"""
        try:
            if task["kind"] == "event":
                r = self.client.request("POST", "/api/device/events", content=task["payload"],
                                        headers={"Content-Type": "application/json"})
            else:
                r = self._send_evidence(task["ref_id"])
                if r is None:
                    self.store.mark_dead(task["task_id"], "本地证据文件缺失")
                    return False
        except SyncDisabled:
            return False
        except httpx.HTTPError as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self.store.mark_retry(task["task_id"], self.last_error)
            return False
        if r.status_code == 200:
            self.store.mark_done(task["task_id"])
            self.sent += 1
            self.last_error = None
            return True
        msg = f"HTTP {r.status_code}: {r.text[:200]}"
        self.last_error = msg
        if _permanent(r.status_code):
            log.error("上传失败且不再重试：%s", msg)
            self.store.mark_dead(task["task_id"], msg)
        else:
            self.store.mark_retry(task["task_id"], msg)
        return False

    def _send_evidence(self, evidence_id: str) -> httpx.Response | None:
        ev = self.store.get_evidence(evidence_id)
        if not ev or not ev["path"] or not Path(ev["path"]).exists():
            return None
        data = Path(ev["path"]).read_bytes()
        headers = {
            "Content-Type": "image/jpeg" if ev["kind"] == "snapshot" else "video/mp4",
            "X-Event-Id": ev["event_id"],
            "X-Kind": ev["kind"],
            "X-MD5": ev["md5"] or "",
            "X-Status": ev["status"],
            "X-Missing-Reason": json.dumps(ev["missing_reason"]),  # json 转义后是 ASCII，可放进 HTTP 头
            "X-Time-Range": json.dumps(ev["time_range"]),
        }
        return self.client.request("PUT", f"/api/device/evidence/{evidence_id}", content=data, headers=headers)


class LinkThread(threading.Thread):
    """心跳 + 配置拉取 + 预览上送，三者都可以丢，不进入持久化队列。"""

    def __init__(self, client: ShoreClient, heartbeat_sec: float, preview_fps: float,
                 health: Callable[[], dict[str, Any]],
                 applied_version: Callable[[], int],
                 apply_config: Callable[[dict[str, Any]], None],
                 preview: Callable[[], tuple[bytes, dict[str, Any]] | None]) -> None:
        super().__init__(name="link", daemon=True)
        self.client = client
        self.heartbeat_sec = heartbeat_sec
        self.preview_period = 1.0 / max(preview_fps, 0.1)
        self.health = health
        self.applied_version = applied_version
        self.apply_config = apply_config
        self.preview = preview
        self.boot_id = str(uuid.uuid4())
        self.seq = 0
        self._stop = threading.Event()
        self.online = False
        self.last_ok: str | None = None
        self.last_error: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        next_hb = 0.0
        elapsed = 0.0
        while not self._stop.is_set():
            if elapsed >= next_hb:
                self._heartbeat()
                next_hb = elapsed + self.heartbeat_sec
            self._push_preview()
            self._stop.wait(self.preview_period)
            elapsed += self.preview_period

    def _heartbeat(self) -> None:
        self.seq += 1
        msg = {"device_id": self.client.device_id, "boot_id": self.boot_id, "seq": self.seq,
               "sent_at": now_iso(), "applied_config_version": self.applied_version(), "health": self.health()}
        try:
            r = self.client.request("POST", "/api/device/heartbeat", json=msg)
            r.raise_for_status()
            reply = r.json()
            self.online = True
            self.last_ok = now_iso()
            self.last_error = None
            if reply.get("config_version", 0) > self.applied_version():
                cr = self.client.request("GET", "/api/device/config")
                cr.raise_for_status()
                self.apply_config(cr.json())
        except SyncDisabled:
            self.online = False
        except Exception as e:  # noqa: BLE001
            self.online = False
            self.last_error = f"{type(e).__name__}: {e}"

    def _push_preview(self) -> None:
        item = self.preview()
        if item is None:
            return
        jpeg, info = item
        try:
            self.client.request("POST", "/api/device/preview",
                                files={"image": ("preview.jpg", jpeg, "image/jpeg")},
                                data={"info": json.dumps(info, ensure_ascii=False)})
        except (SyncDisabled, httpx.HTTPError):
            pass
