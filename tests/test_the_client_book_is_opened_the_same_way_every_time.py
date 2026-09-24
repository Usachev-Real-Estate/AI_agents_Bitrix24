"""Открытие базы клиентов не зависит от того, кто и когда её открывает.

Уроки взяты из витрины, а не придуманы. Там init гнал DDL безусловно, и
задача, которая всего лишь убеждалась, что таблицы на месте, вставала
поперёк идущей загрузки: DDL — даже ничего не меняющий — берёт блокировку
записи. Встреча читателя с ночным прогоном была вопросом времени и
случилась 21.09.

Второй урок оттуда же: версия схемы — это условие, а не украшение. Миграция,
приехавшая без поднятия версии, не позовётся никогда, и боевая база остаётся
без колонки, будучи помеченной «правильной» версией.

Здесь оба урока закреплены на пустом месте, пока база ещё ничего не стоит.
"""

import sqlite3

import pytest

from clients import schema
from clients.schema import _DDL, clients_session, init_clients_db

# Полный список того, что слой обязан завести сам.
EXPECTED_TABLES = {
    "clients_meta",
    "clients",
    "client_links",
    "client_aliases",
    "client_events",
    "client_reviews",
    "client_merges",
    "merge_conflicts",
    "client_runs",
}

# И то, чего он заводить НЕ должен, хотя ТЗ перечисляет это в одном блоке с
# остальными таблицами. См. модульный докстринг schema.py.
FOREIGN_TABLES = {"assignee_log", "transcript_queue", "transcript_launches"}


def _tables(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows if not row[0].startswith("sqlite_")}


def _shape(conn) -> list[tuple[str, str, str]]:
    """Снимок схемы: имя, тип и текст определения каждого объекта."""
    rows = conn.execute(
        "SELECT name, type, COALESCE(sql, '') FROM sqlite_master ORDER BY name"
    ).fetchall()
    return [tuple(row) for row in rows]


def test_every_table_the_layer_needs_is_created(tmp_path):
    """Первое открытие заводит все таблицы разделов 2–3."""
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)

    with clients_session(db_path, readonly=True) as conn:
        assert _tables(conn) == EXPECTED_TABLES


def test_neither_the_queue_nor_the_assignee_log_is_kept_here(tmp_path):
    """Чужие хранилища не дублируются.

    `assignee_log` — это JSONL рядом с выгрузкой досье, `transcript_launches`
    — таблица основной базы агента, а `transcript_queue` не существует
    вовсе. Копия любого из них здесь — это второй счётчик на тот же ресурс,
    который разойдётся с первым в тот день, когда его никто не проверяет.
    """
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)

    with clients_session(db_path, readonly=True) as conn:
        created = _tables(conn)

    assert created & FOREIGN_TABLES == set(), (
        "очередь расшифровок и журнал ответственных ведёт досье, слой их только читает"
    )


def test_opening_the_book_twice_changes_nothing(tmp_path):
    """Повторный init идемпотентен: схема после него та же до буквы."""
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)
    with clients_session(db_path, readonly=True) as conn:
        before = _shape(conn)

    init_clients_db(db_path)
    with clients_session(db_path, readonly=True) as conn:
        after = _shape(conn)

    assert before == after


def test_a_second_opening_does_not_take_the_write_lock(tmp_path, monkeypatch):
    """База нужной версии открывается, пока сосед держит запись.

    Это та самая проверка, которой не было у витрины. Сосед здесь —
    работающая сборка: она держит транзакцию записи, а мы приходим всего
    лишь убедиться, что таблицы на месте. Терпение сбито до 50 мс нарочно:
    с боевыми десятью секундами тест ждал бы их молча и проходил бы, даже
    если бы блокировка бралась.
    """
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)
    monkeypatch.setattr(schema, "BUSY_TIMEOUT_MS", 50)

    rival = sqlite3.connect(str(db_path), timeout=0.05)
    try:
        rival.execute("BEGIN IMMEDIATE")
        init_clients_db(db_path)
    finally:
        rival.rollback()
        rival.close()


def test_a_book_of_the_wrong_version_really_does_need_the_lock(tmp_path, monkeypatch):
    """Обратная половина предыдущего теста.

    Без неё тот был бы истинен и для init, который вообще ничего не делает.
    Как только версия разошлась, DDL обязан пойти в базу — и упереться в
    того же соседа.
    """
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)
    monkeypatch.setattr(schema, "BUSY_TIMEOUT_MS", 50)
    monkeypatch.setattr(schema, "SCHEMA_VERSION", schema.SCHEMA_VERSION + 1)

    rival = sqlite3.connect(str(db_path), timeout=0.05)
    try:
        rival.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            init_clients_db(db_path)
    finally:
        rival.rollback()
        rival.close()


def test_the_tables_are_created_by_ddl_alone(tmp_path):
    """_DDL самодостаточен: ни одна таблица не держится на миграции.

    Миграций у слоя пока нет, и именно поэтому проверка ставится сейчас. На
    витрине колонку, выпавшую из DDL, миграция дописывала обратно — и тесты
    оставались зелёными, пока кто-то не создавал базу с нуля.
    """
    conn = sqlite3.connect(":memory:")
    try:
        for statement in _DDL:
            conn.execute(statement)
        assert _tables(conn) == EXPECTED_TABLES
    finally:
        conn.close()


def test_a_column_added_later_reaches_a_book_opened_earlier(tmp_path):
    """Книга прежней редакции дополняется, а не заводится заново.

    Заново нельзя: в ней уже лежат разборы, переезды ключей и лента — всё,
    что нельзя пересчитать из портала. Проверяется каждая дописываемая
    колонка, а не последняя: список _ADDED_COLUMNS растёт, и молча
    выпавшая из него колонка обнаружилась бы на боевой базе, где новых
    книг никто больше не заводит.
    """
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)
    with clients_session(db_path) as conn:
        for name, _declaration in schema._ADDED_COLUMNS:
            conn.execute(f"ALTER TABLE clients DROP COLUMN {name}")
        conn.execute("UPDATE clients_meta SET value = '1' WHERE key = 'schema_version'")
        conn.execute(
            "INSERT INTO clients(client_key, updated_at) VALUES ('c:77', '')"
        )

    init_clients_db(db_path)

    with clients_session(db_path, readonly=True) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(clients)")}
        kept = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]

    assert {name for name, _ in schema._ADDED_COLUMNS} <= columns
    assert kept == 1, "прежние клиенты пережили дополнение схемы"


def test_the_version_is_written_down(tmp_path):
    """Версия схемы записана в базу, иначе следующий init не с чем сверять."""
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)

    with clients_session(db_path, readonly=True) as conn:
        row = conn.execute(
            "SELECT value FROM clients_meta WHERE key = 'schema_version'"
        ).fetchone()

    assert row is not None and row[0] == str(schema.SCHEMA_VERSION)


def test_the_book_is_kept_in_wal(tmp_path):
    """WAL включён: читатель дашборда не ждёт писателя сборки."""
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)

    with clients_session(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode.lower() == "wal"


def test_the_reader_cannot_write(tmp_path):
    """Соединение на чтение не умеет писать — не по уговору, а физически."""
    db_path = tmp_path / "clients.db"
    init_clients_db(db_path)

    with clients_session(db_path, readonly=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO clients(client_key) VALUES ('p:+70000000000')")


def test_the_path_comes_from_the_environment_when_settings_fail(tmp_path, monkeypatch):
    """Настройки не загрузились — путь всё равно named, а не боевой.

    Ловушка уже срабатывала на витрине: Settings молча падает на нехватке
    ключа Битрикса, и запрос уходит в data/clients.db вместо названного
    файла, ничем этого не показав.
    """
    import config

    def _broken():
        raise RuntimeError("нет обязательного поля")

    monkeypatch.setattr(config, "get_settings", _broken)
    named = tmp_path / "named.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(named))

    assert schema.resolve_db_path() == named


def test_the_named_path_wins_over_everything(tmp_path, monkeypatch):
    """Явно переданный путь не переопределяется ни настройками, ни средой."""
    monkeypatch.setenv("CLIENTS_DB_PATH", str(tmp_path / "from-env.db"))
    named = tmp_path / "explicit.db"

    assert schema.resolve_db_path(named) == named
