"""Текст одного звонка из кэша аудита.

Кэш лежит в `data/violations.db`, таблица `call_transcripts`, и
открывается **только на чтение** — у неё свои писатели.

Права здесь не проверяются, и это намеренно: в базе аудита области
видимости нет вовсе, спрашивать её тут не у кого и не о чем. Право
спрашивает вызывающий — у книги клиентов, по `client_of_call`, — и
спрашивает ПЕРВЫМ. Модуль, который отдаёт текст кому угодно, обязан быть
маленьким и очевидным: тогда видно, что единственный его вызов стоит после
проверки.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from clients.transcripts import DEFAULT_DB_PATH, TRANSCRIBED

logger = logging.getLogger(__name__)


def read_transcript(activity_id: int, *, db_path: str | Path | None = None) -> str | None:
    """Текст звонка. ``None`` — текста нет или кэш недоступен.

    Оба случая для читателя одинаковы: читать нечего. Различать их в
    ответе значило бы рассказывать про устройство очереди расшифровок
    тому, кто спросил про звонок.
    """
    path = Path(db_path or DEFAULT_DB_PATH)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT text FROM call_transcripts WHERE activity_id = ? AND status = ?",
                (int(activity_id), TRANSCRIBED),
            ).fetchone()
    except sqlite3.Error as error:
        logger.warning("Кэш расшифровок недоступен (%s): %s", path, error)
        return None
    return (row[0] or None) if row else None
