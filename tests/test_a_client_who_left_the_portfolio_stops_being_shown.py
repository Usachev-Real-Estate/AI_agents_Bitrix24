"""Клиент, пропавший из портфеля, уходит из списков — но не из истории.

Прогон обновляет только тех, кого видит. Клиент, чью сделку удалили или
увели в чужую воронку, оставался в книге навсегда с состоянием,
замороженным на последнем видевшем его прогоне: «брошен, тишина 40 дней»
— и так до конца времён, потому что тишину ему больше никто не считает.

Расхождение было видно числом: 710 клиентов по состояниям против 715
строк в таблице, и за сутки оно выросло с одного до пяти.

Удалять строку нельзя: на неё ссылаются разборы (`client_reviews` —
единственная таблица книги, которую нельзя пересобрать из витрины) и
журнал переездов ключа. Поэтому она остаётся, но помечается.
"""

import pytest

import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from clients.build import mark_departed
from clients.review import pending
from clients.schema import clients_session, init_clients_db
from clients_scope import apply_scope
from context import Scope

AT = "2026-09-25T12:00:00+00:00"


@pytest.fixture
def book(tmp_path, monkeypatch):
    path = tmp_path / "clients.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(path))
    init_clients_db(path)
    return path


def _client(conn, key, *, run_id, left_at=None, state="cooling", department=5):
    conn.execute(
        "INSERT INTO clients(client_key, triage_state, department_id,"
        " aggregates_run_id, left_at, last_event_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, '2026-09-01T00:00:00+00:00', '')",
        (key, state, department, run_id, left_at),
    )


# ── пометка ───────────────────────────────────────────────────────────

def test_a_client_the_run_did_not_see_is_marked_as_gone(book):
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", run_id=7)   # видели в этом прогоне
        _client(conn, "p:+79000000002", run_id=6)   # видели в прошлом
        counted = mark_departed(conn, 7, at=AT)

    with clients_session(book, readonly=True) as conn:
        left = dict(conn.execute("SELECT client_key, left_at FROM clients"))

    assert counted == {"ушли": 1, "вернулись": 0}
    assert left["p:+79000000001"] is None
    assert left["p:+79000000002"] == AT


def test_a_client_who_came_back_loses_the_mark(book):
    """Сделку вернули в воронку — клиент снова в списке.

    Без снятия пометки возвращённый клиент остался бы невидимым, и
    заметить это было бы нечем: в списке его нет, а строка на месте.
    """
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", run_id=7, left_at="2026-09-01T00:00:00+00:00")
        counted = mark_departed(conn, 7, at=AT)

    with clients_session(book, readonly=True) as conn:
        assert conn.execute("SELECT left_at FROM clients").fetchone()[0] is None
    assert counted == {"ушли": 0, "вернулись": 1}


def test_the_mark_is_not_restamped_every_night(book):
    """Дата ухода — когда ушёл, а не когда последний раз не увидели.

    Переставляя её каждую ночь, мы бы потеряли единственный ответ на
    вопрос «когда этот клиент пропал».
    """
    first = "2026-09-20T00:00:00+00:00"
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", run_id=6, left_at=first)
        mark_departed(conn, 7, at=AT)

    with clients_session(book, readonly=True) as conn:
        assert conn.execute("SELECT left_at FROM clients").fetchone()[0] == first


def test_a_client_nobody_ever_counted_is_marked_too(book):
    """`aggregates_run_id` пуст — значит полного прогона он не застал."""
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", run_id=None)
        mark_departed(conn, 7, at=AT)

    with clients_session(book, readonly=True) as conn:
        assert conn.execute("SELECT left_at FROM clients").fetchone()[0] == AT


# ── кто его больше не видит ───────────────────────────────────────────

def _scoped(book):
    session = clients_session(book, readonly=True)
    conn = session.__enter__()
    apply_scope(conn, Scope(unrestricted=True, department_ids=()))
    return session, conn


def test_the_dashboard_does_not_show_him(book):
    """Отсекается представлением — одним местом на список, счётчики и карточку."""
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", run_id=7)
        _client(conn, "p:+79000000002", run_id=6, left_at=AT)
        mark_departed(conn, 7, at=AT)

    session, conn = _scoped(book)
    try:
        keys = [row[0] for row in conn.execute("SELECT client_key FROM v_client")]
    finally:
        session.__exit__(None, None, None)

    assert keys == ["p:+79000000001"]


def test_the_review_queue_does_not_take_him(book):
    """Прогон читает книгу напрямую, представления у него нет."""
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", run_id=7)
        _client(conn, "p:+79000000002", run_id=7, left_at=AT)

    with clients_session(book, readonly=True) as conn:
        assert pending(conn) == ["p:+79000000001"]


def test_his_history_stays_in_the_book(book):
    """Разбор пережил уход: таблицу разборов нельзя пересобрать."""
    with clients_session(book) as conn:
        _client(conn, "p:+79000000002", run_id=6)
        conn.execute(
            "INSERT INTO client_reviews(client_key, created_at, reviewed_through,"
            " summary, author) VALUES ('p:+79000000002', ?, '', 'вывод', 'модель')",
            (AT,),
        )
        mark_departed(conn, 7, at=AT)

    with clients_session(book, readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM clients").fetchone()[0] == 1
        assert conn.execute(
            "SELECT summary FROM client_reviews").fetchone()[0] == "вывод"
