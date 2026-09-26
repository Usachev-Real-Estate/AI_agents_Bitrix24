"""Область видимости в клиентской книге держится соединением, а не вниманием.

Витрина решила эту задачу так: метрики читают только представления, которых
на неограниченном соединении нет, — забыть ограничение невозможно, забывать
нечего. Книга клиентов живёт в отдельной базе, и тех представлений в ней
нет, а фильтр `WHERE department_id IN (...)`, расставленный по
обработчикам, держится на внимательности. Первый же запрос, где его забыли,
покажет РОПу чужой отдел и не скажет об этом никак: чужие данные выглядят
ровно так же, как свои.

Поэтому проверяется не только «свой отдел видно, чужой нет», но и то, что
запрос БЕЗ области видимости падает. Падение — это и есть защита; молчаливый
показ всей компании ею не является.
"""

import sqlite3

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

from analytics.scope import Scope
from clients.schema import clients_session, init_clients_db
from clients_scope import SCOPED_VIEWS, scoped_clients


@pytest.fixture
def book(tmp_path, monkeypatch):
    """Книга с клиентами трёх видов: свой отдел, чужой и без отдела."""
    from config import get_settings

    path = tmp_path / "clients.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(path))
    get_settings.cache_clear()
    init_clients_db(path)
    with clients_session(path) as conn:
        conn.executemany(
            "INSERT INTO clients(client_key, department_id, updated_at)"
            " VALUES (?, ?, '')",
            [("p:свой", 5), ("p:чужой", 9), ("p:ничей", None)],
        )
        conn.executemany(
            "INSERT INTO client_links(client_key, entity_type, entity_id)"
            " VALUES (?, 'deal', ?)",
            [("p:свой", 1), ("p:чужой", 2), ("p:ничей", 3)],
        )
        conn.executemany(
            "INSERT INTO client_events(client_key, at, kind, source_id,"
            " entity_type, entity_id) VALUES (?, '', 'comment', ?, 'deal', 1)",
            [("p:свой", "a"), ("p:чужой", "b"), ("p:ничей", "c")],
        )
        conn.executemany(
            "INSERT INTO client_reviews(client_key, created_at, reviewed_through)"
            " VALUES (?, '', '')",
            [("p:свой",), ("p:чужой",), ("p:ничей",)],
        )
        conn.executemany(
            "INSERT INTO client_issues(client_key, code) VALUES (?, 'abandoned')",
            [("p:свой",), ("p:чужой",), ("p:ничей",)],
        )
    yield path
    get_settings.cache_clear()


def _keys(conn, view: str = "v_client") -> list[str]:
    return [row[0] for row in conn.execute(f"SELECT client_key FROM {view} ORDER BY 1")]


def test_a_query_without_a_scope_has_nothing_to_read(book):
    """На соединении без области видимости представлений просто нет.

    Это и есть весь приём. Обработчик, забывший открыть соединение через
    `scoped_clients`, упадёт на отсутствующей таблице — громко и сразу,
    а не покажет РОПу всю компанию.
    """
    with clients_session(book, readonly=True) as conn:
        for view in SCOPED_VIEWS:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                conn.execute(f"SELECT * FROM {view}")


def test_a_head_of_department_sees_only_his_own(book):
    """РОП видит свой отдел и не видит чужой."""
    with scoped_clients(Scope.departments([5])) as conn:
        assert _keys(conn) == ["p:свой"]


def test_an_administrator_sees_the_whole_company(book):
    """Администратор видит всех, включая клиентов без отдела."""
    with scoped_clients(Scope.everything()) as conn:
        assert _keys(conn) == ["p:ничей", "p:свой", "p:чужой"]


def test_a_client_without_a_department_is_invisible_to_a_head(book):
    """Клиент без отдела РОПу не виден — как и в витрине.

    `department_id` пуст, когда у ответственного нет отдела или
    ответственного нет вовсе. `IN (...)` такую строку не пропускает сам
    собой — и именно поэтому правило проверяется: «само собой» перестаёт
    работать от одной правки запроса, и заметить это будет нечем.
    """
    with scoped_clients(Scope.departments([5, 9])) as conn:
        assert "p:ничей" not in _keys(conn)


def test_an_empty_scope_shows_nothing(book):
    """Пустая область видимости — это «не видно ничего», а не «видно всё».

    `Scope.for_user` отдаёт её и незалогиненному, и пользователю с
    нераспознанной ролью. Ошибка в эту сторону стоит всего портфеля.
    """
    with scoped_clients(Scope.departments([])) as conn:
        assert _keys(conn) == []


@pytest.mark.parametrize("view", ["v_client_link", "v_client_event", "v_client_review"])
def test_the_companions_are_narrowed_through_the_client(book, view):
    """Карточки, лента и разборы сужаются вместе с клиентом.

    Своего поля отдела у них нет, и заводить копию значило бы держать два
    источника правды о том, чей это клиент, — расходящихся ровно в тот
    день, когда клиент переезжает к другому брокеру. Но сузить клиента и
    забыть спутников значит отдать РОПу чужую переписку через ленту.
    """
    with scoped_clients(Scope.departments([5])) as conn:
        rows = conn.execute(f"SELECT DISTINCT client_key FROM {view}").fetchall()

    assert [row[0] for row in rows] == ["p:свой"]


def test_the_scope_does_not_open_the_book_for_writing(book):
    """Соединение остаётся только на чтение, хотя и создаёт представления.

    Временная база у SQLite своя, `mode=ro` ей не мешает — на этом вся
    конструкция и держится. Но основная база обязана остаться закрытой:
    у книги один писатель, ночная пересборка.
    """
    with scoped_clients(Scope.everything()) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE clients SET name = 'чужое'")


def test_every_scoped_view_is_actually_narrowed(book):
    """Ни одно представление не осталось без ограничения.

    Список `SCOPED_VIEWS` — это обещание обработчикам: «читайте только
    отсюда». Представление, попавшее в список и забывшее про область
    видимости, было бы худшим из возможных: обработчик читает его с
    чистой совестью.
    """
    with scoped_clients(Scope.departments([5])) as conn:
        for view in SCOPED_VIEWS:
            rows = conn.execute(f"SELECT DISTINCT client_key FROM {view}").fetchall()
            assert [row[0] for row in rows] == ["p:свой"], view


def test_the_list_of_scoped_views_matches_what_is_created(book):
    """Список и реальность сходятся.

    Добавленное представление, не попавшее в `SCOPED_VIEWS`, выпало бы из
    проверки выше — и осталось бы единственным неограниченным.
    """
    with scoped_clients(Scope.everything()) as conn:
        created = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_temp_master WHERE type = 'view'"
            )
        }

    assert created == set(SCOPED_VIEWS)
