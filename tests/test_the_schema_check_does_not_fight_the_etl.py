"""Убедиться, что схема на месте, — это чтение, а не запись.

Боевая авария: читатель комментариев упал на первой же строке с
«database is locked». Упал не на работе, а на проверке — init_analytics_db()
безусловно гнал DDL, а DDL берёт блокировку записи, даже когда ничего не
меняет. Рядом шла догрузка витрины, и она эту блокировку держала.

Встреча была не случайностью, а расписанием: ETL тикает каждые 15 минут,
читатель по крону стоит на 03:00. Раз в четыре ночи они попадали бы в одну
минуту, читатель падал бы с ненулевым кодом, будил админа и не читал
ничего.

Починка — проверять версию схемы чтением. У витрины включён WAL, читатель
писателю не мешает; писать нужно, только когда версия разошлась.

Отсюда же второе правило, и оно опаснее первого: НОВАЯ МИГРАЦИЯ ОБЯЗАНА
ПОДНИМАТЬ SCHEMA_VERSION. Пока DDL шёл безусловно, забытое поднятие
сходило с рук — ровно так приехала колонка prompt_version. С проверкой
версии такая миграция не выполнится никогда.
"""

from __future__ import annotations

import sqlite3

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

from schema import (  # noqa: E402
    SCHEMA_VERSION,
    analytics_session,
    get_connection,
    init_analytics_db,
)


def test_the_check_survives_a_writer_holding_the_lock(analytics_db):
    """Главная проверка: рядом пишут, а мы всё равно стартуем.

    Соседнее соединение держит транзакцию записи — ровно то, что делает
    ETL. Раньше здесь падало «database is locked».
    """
    other = get_connection()
    other.execute("BEGIN IMMEDIATE")
    other.execute(
        "INSERT INTO analytics_meta(key, value) VALUES('busy', '1')"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    try:
        init_analytics_db()          # не должно поднять OperationalError
    finally:
        other.rollback()
        other.close()


def test_a_writer_still_blocks_a_real_migration(analytics_db):
    """Обратная сторона: когда писать НАДО, блокировка по-прежнему мешает.

    Тест не про желаемое поведение, а про честную границу: чуда не
    случилось, мы просто перестали писать без нужды. Разошлась версия —
    приходится ждать своей очереди, и это правильно: миграция обязана
    состояться, а не быть пропущенной ради тишины.
    """
    with analytics_session() as conn:
        conn.execute("UPDATE analytics_meta SET value = '1'"
                     " WHERE key = 'schema_version'")

    other = get_connection()
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO analytics_meta(key, value) VALUES('busy', '1')"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    try:
        with pytest.raises(sqlite3.OperationalError):
            init_analytics_db()
    finally:
        other.rollback()
        other.close()


def test_the_version_is_stamped_after_a_migration(analytics_db):
    """Отметка обновляется, иначе миграция шла бы каждый запуск."""
    with analytics_session() as conn:
        conn.execute("UPDATE analytics_meta SET value = '1'"
                     " WHERE key = 'schema_version'")

    init_analytics_db()

    with analytics_session(readonly=True) as conn:
        stamped = conn.execute(
            "SELECT value FROM analytics_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert stamped == str(SCHEMA_VERSION)


def test_an_empty_file_is_built_from_scratch(tmp_path):
    """Витрины ещё нет — проверка версии не должна этого испугаться."""
    fresh = tmp_path / "new.db"

    init_analytics_db(fresh)

    with analytics_session(fresh, readonly=True) as conn:
        stamped = conn.execute(
            "SELECT value FROM analytics_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert stamped == str(SCHEMA_VERSION)
