"""船端本地控制台（只监听 127.0.0.1）。

断网时船员仍可在本地页面看到告警；演示时用它切换同步、注入故障、调节模拟船况。
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from common.config import ROOT

if TYPE_CHECKING:
    from edge.app import EdgeApp

log = logging.getLogger("edge.console")


class SimBody(BaseModel):
    speed_kn: float | None = None
    nav_state: str | None = None
    ais: bool | None = None
    telemetry: bool | None = None
    clear: bool = False


def create_console_app(edge: "EdgeApp") -> FastAPI:
    app = FastAPI(title="船端本地控制台")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (ROOT / "edge" / "console.html").read_text(encoding="utf-8")

    @app.get("/status")
    def status() -> dict[str, Any]:
        return edge.local_status()

    @app.post("/sync/{state}")
    def sync(state: str) -> dict[str, Any]:
        if state not in ("on", "off"):
            raise HTTPException(400, "state 只能是 on 或 off")
        edge.client.enabled = state == "on"
        log.warning("船岸同步已%s", "开启" if edge.client.enabled else "关闭")
        return {"sync_enabled": edge.client.enabled}

    @app.post("/fault/{mode}")
    def fault(mode: str) -> dict[str, Any]:
        if mode not in ("stream_cut", "black", "clear"):
            raise HTTPException(400, "mode 只能是 stream_cut / black / clear")
        edge.faults.set(None if mode == "clear" else mode)
        log.warning("故障注入：%s", mode)
        return {"fault": edge.faults.mode}

    @app.post("/sim")
    def sim(body: SimBody) -> dict[str, Any]:
        if body.clear:
            edge.sim.clear_overrides()
        else:
            edge.sim.set_override(**body.model_dump(exclude={"clear"}, exclude_none=True))
        return edge.sim.describe()

    @app.post("/camera/{camera_id}")
    def camera(camera_id: str) -> dict[str, Any]:
        try:
            edge.cfg.camera(camera_id)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        edge.commands.put(("switch_camera", camera_id))
        return {"switching_to": camera_id}

    @app.post("/source/{action}")
    def source(action: str) -> dict[str, Any]:
        if action not in ("restart", "pause", "resume"):
            raise HTTPException(400, "action 只能是 restart / pause / resume")
        edge.commands.put(("restart_source" if action == "restart" else action, None))
        return {"ok": True}

    return app


def start_console(edge: "EdgeApp", port: int) -> threading.Thread:
    config = uvicorn.Config(create_console_app(edge), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="console", daemon=True)
    t.start()
    log.info("船端控制台：http://127.0.0.1:%s", port)
    return t
