"""Область видимости в клиентской книге (раздел 7.1 ТЗ).

Витрина ограничивает отделы **на уровне соединения**: метрики обращаются
только к представлениям `v_deal`, `v_lead`, и на неограниченном соединении
их просто нет — забыть ограничение в новом запросе невозможно, забывать
нечего (`analytics/scope.py`).

Клиентская книга — отдельная база, и тех представлений в ней нет. Но приём
переносится целиком, и переносить его обязательно. Фильтр `WHERE
department_id IN (...)`, расставленный по обработчикам, держится на
внимательности: первый же добавленный запрос, где его забыли, покажет РОПу
чужой отдел и не скажет об этом никак. Здесь обработчики читают `v_client`
и его спутников, а на соединении без области видимости этих представлений
нет — запрос упадёт на отсутствующей таблице, а не покажет всю компанию.

Соединение открывается только на чтение. Временные представления при этом
создаются: временная база у SQLite своя, и `mode=ro` ей не мешает —
проверено тестом, потому что на нём держится вся конструкция.

**Клиент без отдела РОПу не виден.** `department_id` пуст, когда у
ответственного нет отдела или ответственного нет вовсе; `IN (...)` такую
строку не пропускает сам собой. Это то же правило, что в витрине, и оно
проверяется тестом: «само собой» — плохая опора для правила видимости.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from analytics.scope import Scope
from clients.schema import clients_session

# Представления, которые создаёт область видимости. Обработчики читают
# только их; список нужен тесту, который сверяет, что ни одно не осталось
# без ограничения.
SCOPED_VIEWS = ("v_client", "v_client_link", "v_client_event", "v_client_review",
                "v_client_issue")


class BookMissing(RuntimeError):
    """Книги клиентов нет или она пуста как файл.

    Дашборд живёт независимо от ночной пересборки: его поднимают раньше,
    чем слой собрался хоть раз, а `CLIENTS_DB_PATH` может смотреть не туда.
    Это не «клиентов ноль» и не поломка кода — это «книги ещё нет», и
    отвечать на такое пятисоткой значит показать человеку трассировку
    вместо объяснения.
    """


def apply_scope(conn: sqlite3.Connection, scope: Scope) -> None:
    """Создать на соединении суженные представления книги."""
    # Представление над несуществующей таблицей SQLite создаёт молча:
    # имена он разрешает при первом запросе, а не при CREATE VIEW. Без
    # этой проверки книга, которой нет, обнаружилась бы посреди отдачи
    # потока — когда заголовки ответа уже ушли читателю.
    if not _book_is_there(conn):
        raise BookMissing("в книге клиентов нет таблицы clients")
    conn.execute("CREATE TEMP TABLE scope_department (department_id INTEGER PRIMARY KEY)")
    # Ушедшие из портфеля отсекаются ЗДЕСЬ, а не в каждом запросе. Через
    # это представление дашборд видит книгу целиком — список, счётчики,
    # карточку и всех спутников, — и отфильтровав один раз, забыть об
    # этом в четвёртом месте уже нельзя. Состояние у такой строки
    # заморожено на последнем видевшем её прогоне, и показывать его —
    # значит звать брокера к человеку, которого в портфеле нет.
    conditions = ["left_at IS NULL"]
    if not scope.unrestricted:
        conn.executemany(
            "INSERT INTO scope_department(department_id) VALUES (?)",
            [(int(value),) for value in scope.department_ids],
        )
        conditions.append(
            "department_id IN (SELECT department_id FROM scope_department)"
        )
    where = " WHERE " + " AND ".join(conditions)

    conn.execute(f"CREATE TEMP VIEW v_client AS SELECT * FROM clients{where}")
    # Спутники сужаются ЧЕРЕЗ клиента, а не своим полем отдела: у них его
    # нет, и заводить копию значило бы держать два источника правды о том,
    # чей это клиент, — расходящихся ровно в тот день, когда клиент
    # переезжает к другому брокеру.
    for view, table in (
        ("v_client_link", "client_links"),
        ("v_client_event", "client_events"),
        ("v_client_review", "client_reviews"),
        ("v_client_issue", "client_issues"),
    ):
        conn.execute(
            f"CREATE TEMP VIEW {view} AS SELECT * FROM {table}"
            " WHERE client_key IN (SELECT client_key FROM v_client)"
        )


def _book_is_there(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'clients'"
    ).fetchone()
    return row is not None


@contextmanager
def scoped_clients(scope: Scope) -> Iterator[sqlite3.Connection]:
    """Соединение с книгой, суженное до области видимости.

    Единственный способ читать книгу из веба. Книги нет — ``BookMissing``,
    и обработчик обязан сказать об этом словами.
    """
    try:
        session = clients_session(readonly=True)
        conn = session.__enter__()
    except sqlite3.Error as error:
        # Файла нет вовсе: readonly-соединение его не создаёт и падает
        # прямо здесь.
        raise BookMissing(str(error)) from error
    try:
        apply_scope(conn, scope)
        yield conn
    finally:
        session.__exit__(None, None, None)
