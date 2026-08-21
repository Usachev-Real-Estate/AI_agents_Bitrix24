"""Хранилище авторизации дашборда — data/dashboard.db.

Отдельный файл, а не таблицы в витрине, и это не косметика: веб-процесс
открывает витрину строго только на чтение, а писать умеет исключительно в
собственную базу сессий. Скомпрометированный обработчик не сможет испортить
бизнес-данные, и веб не конкурирует за блокировки с ETL.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/dashboard.db")
BUSY_TIMEOUT_MS = 5_000

_HASHER = PasswordHasher()

# Пароль несуществующего пользователя всё равно «проверяется» об этот хеш:
# иначе форма логина отвечает быстрее на неизвестный логин, чем на неверный
# пароль, и превращается в перечислитель учётных записей.
_DUMMY_HASH = _HASHER.hash("dummy-password-for-constant-time-compare")

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS dash_user (
        username       TEXT PRIMARY KEY,
        password_hash  TEXT NOT NULL,
        display_name   TEXT NOT NULL DEFAULT '',
        is_active      INTEGER NOT NULL DEFAULT 1,
        created_at     TEXT NOT NULL,
        last_login_at  TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dash_session (
        sid          TEXT PRIMARY KEY,
        username     TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        ip           TEXT NOT NULL DEFAULT '',
        user_agent   TEXT NOT NULL DEFAULT '',
        revoked_at   TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dash_login_attempt (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        username  TEXT NOT NULL DEFAULT '',
        ip        TEXT NOT NULL DEFAULT '',
        ok        INTEGER NOT NULL DEFAULT 0,
        at        TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_login_attempt ON dash_login_attempt(at);",
    "CREATE INDEX IF NOT EXISTS idx_session_user ON dash_session(username, expires_at);",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    try:
        from config import get_settings

        analytics = Path(get_settings().analytics_db_path)
        return analytics.with_name("dashboard.db")
    except Exception:  # pragma: no cover
        return DEFAULT_DB_PATH


@contextmanager
def store_session(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_store(db_path: str | Path | None = None) -> None:
    """Создать таблицы авторизации. Идемпотентно."""
    with store_session(db_path) as conn:
        for statement in _DDL:
            conn.execute(statement)


# --------------------------------------------------------------------------
# пользователи
# --------------------------------------------------------------------------

def create_user(username: str, password: str, display_name: str = "") -> None:
    """Завести пользователя. Пароль хешируется argon2id."""
    username = username.strip().lower()
    if not username:
        raise ValueError("Пустой логин")
    if len(password) < 10:
        raise ValueError("Пароль короче 10 символов")
    with store_session() as conn:
        conn.execute(
            "INSERT INTO dash_user(username, password_hash, display_name, is_active, created_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(username) DO UPDATE SET "
            "password_hash=excluded.password_hash, display_name=excluded.display_name, "
            "is_active=1",
            (username, _HASHER.hash(password), display_name or username, _iso(_now())),
        )


def set_user_active(username: str, active: bool) -> bool:
    with store_session() as conn:
        cursor = conn.execute(
            "UPDATE dash_user SET is_active = ? WHERE username = ?",
            (1 if active else 0, username.strip().lower()),
        )
        return cursor.rowcount > 0


def list_users() -> list[dict[str, Any]]:
    with store_session() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT username, display_name, is_active, created_at, last_login_at "
            "FROM dash_user ORDER BY username"
        )]


def verify_password(username: str, password: str) -> dict[str, Any] | None:
    """Проверить пару логин/пароль. None — если не сошлось.

    Несуществующий логин и неверный пароль обрабатываются одинаково по
    времени: сравнение всё равно выполняется, только с фиктивным хешем.
    """
    username = (username or "").strip().lower()
    with store_session() as conn:
        row = conn.execute(
            "SELECT username, password_hash, display_name, is_active "
            "FROM dash_user WHERE username = ?",
            (username,),
        ).fetchone()

        stored_hash = row["password_hash"] if row else _DUMMY_HASH
        try:
            _HASHER.verify(stored_hash, password or "")
            matched = True
        except (VerifyMismatchError, InvalidHashError):
            matched = False
        except Exception:  # pragma: no cover — argon2 внутренняя ошибка
            logger.exception("Ошибка проверки пароля")
            matched = False

        if not matched or row is None or not row["is_active"]:
            return None

        if _HASHER.check_needs_rehash(stored_hash):
            conn.execute(
                "UPDATE dash_user SET password_hash = ? WHERE username = ?",
                (_HASHER.hash(password), username),
            )
        conn.execute(
            "UPDATE dash_user SET last_login_at = ? WHERE username = ?",
            (_iso(_now()), username),
        )
        return {"username": row["username"], "display_name": row["display_name"]}


# --------------------------------------------------------------------------
# защита от подбора
# --------------------------------------------------------------------------

def record_attempt(username: str, ip: str, ok: bool) -> None:
    with store_session() as conn:
        conn.execute(
            "INSERT INTO dash_login_attempt(username, ip, ok, at) VALUES (?, ?, ?, ?)",
            ((username or "").strip().lower(), ip or "", 1 if ok else 0, _iso(_now())),
        )


def is_locked_out(username: str, ip: str, *, max_attempts: int, window_minutes: int) -> bool:
    """Слишком много неудач за окно — по логину ИЛИ по адресу.

    Счёт по адресу нужен отдельно: подбор одного пароля по списку логинов
    («password spraying») не даёт много неудач ни на одном логине.
    """
    since = _iso(_now() - timedelta(minutes=window_minutes))
    with store_session() as conn:
        by_user = conn.execute(
            "SELECT COUNT(*) FROM dash_login_attempt "
            "WHERE ok = 0 AND at >= ? AND username = ?",
            (since, (username or "").strip().lower()),
        ).fetchone()[0]
        by_ip = conn.execute(
            "SELECT COUNT(*) FROM dash_login_attempt WHERE ok = 0 AND at >= ? AND ip = ?",
            (since, ip or ""),
        ).fetchone()[0]
    return by_user >= max_attempts or by_ip >= max_attempts * 3


def purge_old_attempts(days: int = 30) -> None:
    cutoff = _iso(_now() - timedelta(days=days))
    with store_session() as conn:
        conn.execute("DELETE FROM dash_login_attempt WHERE at < ?", (cutoff,))


# --------------------------------------------------------------------------
# сессии
# --------------------------------------------------------------------------

def create_session(username: str, *, ttl_hours: int, ip: str = "", user_agent: str = "") -> str:
    """Создать серверную сессию, вернуть её идентификатор."""
    sid = secrets.token_urlsafe(32)
    now = _now()
    with store_session() as conn:
        conn.execute(
            "INSERT INTO dash_session(sid, username, created_at, last_seen_at, expires_at, "
            "ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, username, _iso(now), _iso(now), _iso(now + timedelta(hours=ttl_hours)),
             ip or "", (user_agent or "")[:200]),
        )
    return sid


def touch_session(sid: str, *, idle_hours: int) -> dict[str, Any] | None:
    """Проверить сессию и продлить её скользящее окно.

    Две независимые границы: абсолютный срок (expires_at) и простой
    (idle_hours). Первый ограничивает украденную куку, второй закрывает
    забытую вкладку на чужом устройстве.
    """
    if not sid:
        return None
    now = _now()
    with store_session() as conn:
        row = conn.execute(
            "SELECT s.sid, s.username, s.last_seen_at, s.expires_at, s.revoked_at, "
            "u.display_name, u.is_active "
            "FROM dash_session s LEFT JOIN dash_user u ON u.username = s.username "
            "WHERE s.sid = ?",
            (sid,),
        ).fetchone()
        if row is None or row["revoked_at"] or not row["is_active"]:
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"])
            last_seen = datetime.fromisoformat(row["last_seen_at"])
        except ValueError:
            return None
        if now >= expires or now - last_seen > timedelta(hours=idle_hours):
            conn.execute(
                "UPDATE dash_session SET revoked_at = ? WHERE sid = ?", (_iso(now), sid),
            )
            return None
        conn.execute("UPDATE dash_session SET last_seen_at = ? WHERE sid = ?", (_iso(now), sid))
        return {"username": row["username"], "display_name": row["display_name"] or row["username"]}


def revoke_session(sid: str) -> None:
    if not sid:
        return
    with store_session() as conn:
        conn.execute(
            "UPDATE dash_session SET revoked_at = ? WHERE sid = ? AND revoked_at IS NULL",
            (_iso(_now()), sid),
        )


def revoke_all_sessions(username: str) -> int:
    with store_session() as conn:
        cursor = conn.execute(
            "UPDATE dash_session SET revoked_at = ? WHERE username = ? AND revoked_at IS NULL",
            (_iso(_now()), username.strip().lower()),
        )
        return cursor.rowcount


def list_sessions(username: str | None = None) -> list[dict[str, Any]]:
    with store_session() as conn:
        if username:
            rows = conn.execute(
                "SELECT sid, username, created_at, last_seen_at, expires_at, ip, revoked_at "
                "FROM dash_session WHERE username = ? ORDER BY created_at DESC",
                (username.strip().lower(),),
            )
        else:
            rows = conn.execute(
                "SELECT sid, username, created_at, last_seen_at, expires_at, ip, revoked_at "
                "FROM dash_session ORDER BY created_at DESC LIMIT 100"
            )
        return [dict(row) for row in rows]


def purge_expired_sessions() -> None:
    with store_session() as conn:
        conn.execute("DELETE FROM dash_session WHERE expires_at < ?", (_iso(_now()),))
