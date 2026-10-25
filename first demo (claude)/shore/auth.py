"""账号、登录会话和设备令牌。密码只存 PBKDF2 哈希。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from common.enums import Role
from common.timeutil import now_iso, to_iso, utcnow
from shore.db import Database

SESSION_HOURS = 12

# 角色可做的事
CAN_HANDLE = {Role.ADMIN, Role.OPERATOR, Role.CAPTAIN, Role.MANAGER}
CAN_CONFIG = {Role.ADMIN}


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_user(db: Database, username: str, display_name: str, role: Role, password: str) -> None:
    salt = secrets.token_hex(16)
    with db.tx() as c:
        c.execute("INSERT INTO users(username, display_name, role, salt, password_hash) VALUES(?,?,?,?,?)"
                  " ON CONFLICT(username) DO UPDATE SET display_name=excluded.display_name, role=excluded.role,"
                  " salt=excluded.salt, password_hash=excluded.password_hash",
                  (username, display_name, role.value, salt, hash_password(password, salt)))


def verify_login(db: Database, username: str, password: str) -> dict | None:
    u = db.one("SELECT * FROM users WHERE username=?", (username,))
    if not u or not hmac.compare_digest(u["password_hash"], hash_password(password, u["salt"])):
        return None
    return u


def new_session(db: Database, username: str) -> str:
    token = secrets.token_urlsafe(32)
    with db.tx() as c:
        c.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now_iso(),))
        c.execute("INSERT INTO auth_sessions(token, username, expires_at) VALUES(?,?,?)",
                  (hash_token(token), username, to_iso(utcnow() + timedelta(hours=SESSION_HOURS))))
    return token


def session_user(db: Database, token: str | None) -> dict | None:
    if not token:
        return None
    return db.one("SELECT u.username, u.display_name, u.role FROM auth_sessions s JOIN users u USING(username)"
                  " WHERE s.token=? AND s.expires_at > ?", (hash_token(token), now_iso()))


def end_session(db: Database, token: str | None) -> None:
    if token:
        with db.tx() as c:
            c.execute("DELETE FROM auth_sessions WHERE token=?", (hash_token(token),))


def device_by_token(db: Database, device_id: str | None, token: str | None) -> dict | None:
    if not device_id or not token:
        return None
    d = db.one("SELECT * FROM devices WHERE device_id=?", (device_id,))
    if d and hmac.compare_digest(d["token_hash"], hash_token(token)):
        return d
    return None
