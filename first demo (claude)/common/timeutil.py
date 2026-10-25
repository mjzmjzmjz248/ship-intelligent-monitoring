"""时间工具。数据库统一存 UTC ISO 字符串，业务日按配置时区切换。"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return to_iso(utcnow())


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def business_day(dt: datetime, tz: ZoneInfo = BUSINESS_TZ) -> date:
    return dt.astimezone(tz).date()


def local_str(dt: datetime | None, tz: ZoneInfo = BUSINESS_TZ) -> str:
    if dt is None:
        return "-"
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


class SessionClock:
    """可暂停的单调时钟，返回会话内秒数。暂停期间不累计，规则计时随之停止。"""

    def __init__(self) -> None:
        self._base = time.monotonic()
        self._paused_at: float | None = None
        self._paused_total = 0.0

    def now(self) -> float:
        ref = self._paused_at if self._paused_at is not None else time.monotonic()
        return ref - self._base - self._paused_total

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = time.monotonic()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def wall_of(self, t: float) -> datetime:
        """把会话秒数换算成墙上时间（UTC）。"""
        return datetime.fromtimestamp(time.time() - (self.now() - t), timezone.utc)
