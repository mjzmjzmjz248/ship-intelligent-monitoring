"""岸端服务：python -m shore（或 uvicorn shore.main:create_app --factory --port 8000）

同时托管网页（web/ 目录），演示时只需打开一个地址。
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from common.config import ROOT, RUNTIME_DIR, ShoreConfig
from common.enums import Risk
from common.timeutil import parse_iso, utcnow
from shore.db import Database
from shore.services import config as config_svc
from shore.services import notify
from shore.services.events import ServiceError, open_offline_event

log = logging.getLogger("shore")


@dataclass
class ShoreContext:
    db: Database
    storage: Path
    previews: dict[str, tuple[bytes, dict[str, Any], str]] = field(default_factory=dict)
    started_mono: float = field(default_factory=time.monotonic)

    def shore_params(self) -> ShoreConfig:
        cur = config_svc.latest(self.db)
        if cur is None:
            raise RuntimeError("岸端尚未初始化配置，请先运行 scripts/init_shore.py")
        return ShoreConfig.model_validate(cur["content"]["shore"])

    def profile(self) -> str:
        cur = config_svc.latest(self.db)
        return cur["profile"] if cur else "-"


class Scheduler(threading.Thread):
    """每秒检查：终端心跳超时、船长确警超时。"""

    def __init__(self, ctx: ShoreContext) -> None:
        super().__init__(name="scheduler", daemon=True)
        self.ctx = ctx
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("调度任务异常")
            self._stop.wait(1.0)

    def tick(self) -> None:
        db = self.ctx.db
        sp = self.ctx.shore_params()
        # 岸端刚启动时给设备一个完整的超时窗口，避免把岸端自己的停机算成设备离线
        if time.monotonic() - self.ctx.started_mono >= sp.offline_after_sec:
            cutoff = utcnow() - timedelta(seconds=sp.offline_after_sec)
            for d in db.all("SELECT * FROM devices WHERE link_status='ONLINE'"):
                last = parse_iso(d["last_seen_at"])
                if last is not None and last < cutoff:
                    eid = open_offline_event(db, d, Risk(sp.offline_risk), self.ctx.profile(), sp.offline_basis,
                                             sp.captain_deadline_sec)
                    log.warning("终端 %s 离线，生成事件 %s", d["device_id"], eid[:8])
        notify.check_deadlines(db)


def create_app(db_path: str | Path | None = None, storage: str | Path | None = None,
               run_scheduler: bool = True) -> FastAPI:
    db = Database(db_path or RUNTIME_DIR / "shore" / "shore.db")
    ctx = ShoreContext(db=db, storage=Path(storage or RUNTIME_DIR / "shore" / "evidence"))
    scheduler = Scheduler(ctx)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="船舶智能监控 岸端", lifespan=lifespan)
    app.state.ctx = ctx
    app.state.scheduler = scheduler

    @app.exception_handler(ServiceError)
    async def _service_error(_, exc: ServiceError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    from shore.api.device import router as device_router
    from shore.api.user import router as user_router

    app.include_router(device_router)
    app.include_router(user_router)
    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
    return app

