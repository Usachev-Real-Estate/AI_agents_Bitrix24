"""Чтение портфеля из аналитической витрины (раздел 4 ТЗ, шаг 3).

Витрину клиентский слой только читает, и читает соединением
``analytics_session(readonly=True)``: у витрины один писатель — ETL, и
сентябрьские «database is locked» стоили ночного бюджета ровно потому, что
к нему подсаживались соседи. Читатель в WAL писателю не мешает, а вот
случайная запись мешала бы всем.

Всё, что нужно прогону, забирается запросом на таблицу и складывается в
память: портфель — это тысячи строк, а не миллионы, и держать соединение с
витриной открытым на всё время добора контактов из портала незачем.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

from analytics.work import CALL
from clients.keys import Card

logger = logging.getLogger(__name__)

# Владелец дела в Битриксе. Дела висят и на сделке, и на контакте, и
# различать их обязательно: id 42 у сделки и id 42 у контакта — разные
# сущности. Замер портала (work.py): 13 277 дел на контактах против 7 131
# на сделках, то есть взяв только сделки, отчёт назвал бы молчащими тех,
# кто звонил.
OWNER_TYPE_DEAL = 2
OWNER_TYPE_CONTACT = 3

ENTITY_DEAL = "deal"

# Размер пачки для `IN (...)`. У SQLite есть потолок числа параметров в
# запросе, и портфель на тридцать тысяч сделок в него однажды упрётся —
# упрётся не здесь, в тесте, а ночью на боевом прогоне. Пятьсот — с запасом
# ниже любого известного потолка и всё ещё один запрос на две сотни строк.
CHUNK = 500

# Тип дела «звонок» в терминах витрины. Значение берётся из analytics.work,
# а не переписывается сюда: там оно уже работает на боевых отчётах, и
# вторая копия разойдётся с первой в тот день, когда портал заведёт новый
# провайдер телефонии.
CALL_PROVIDER = CALL


@dataclass(frozen=True)
class Portfolio:
    """Всё, что прогон взял из витрины, одним куском."""

    cards: tuple[Card, ...]
    deals: dict[int, dict[str, Any]]
    comments: tuple[dict[str, Any], ...]
    activities: tuple[dict[str, Any], ...]
    moves: tuple[dict[str, Any], ...]
    users: dict[int, dict[str, Any]]
    stages: dict[tuple[str, int], str]
    mart_full_sync_at: str | None

    @property
    def contact_ids(self) -> tuple[int, ...]:
        """Контакты, на которые ссылаются сделки портфеля."""
        found = {
            int(deal["contact_id"]) for deal in self.deals.values()
            if deal.get("contact_id")
        }
        return tuple(sorted(found))


def _chunks(values: Sequence[int]) -> Iterator[tuple[int, ...]]:
    ordered = tuple(values)
    for start in range(0, len(ordered), CHUNK):
        yield ordered[start:start + CHUNK]


def _fetch_in(conn, sql: str, ids: Sequence[int], *, extra: tuple = ()) -> list[dict]:
    """Выполнить запрос с `IN (...)` по пачкам и склеить строки.

    ``sql`` обязан содержать один ``{placeholders}``; остальные параметры
    идут перед списком идентификаторов.
    """
    out: list[dict] = []
    for chunk in _chunks(ids):
        marks = ", ".join("?" * len(chunk))
        rows = conn.execute(sql.format(placeholders=marks), (*extra, *chunk)).fetchall()
        out.extend(dict(row) for row in rows)
    return out


def read_deals(conn, categories: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Сделки нужных воронок.

    Фильтра по дате создания здесь нарочно НЕТ. Окно уже задано самой
    витриной — она грузит только его, — а отсечь дополнительно по
    `date_create` значило бы спрятать сделку, заведённую четырнадцать
    месяцев назад и живую до сих пор. Это не старьё, это клиент в работе.
    """
    wanted = tuple(sorted({int(value) for value in categories}))
    marks = ", ".join("?" * len(wanted))
    rows = conn.execute(
        "SELECT deal_id, title, category_id, stage_id, assigned_by_id, contact_id,"
        " date_create, closedate, is_closed, is_won, is_lost"
        f" FROM fact_deal WHERE is_deleted = 0 AND category_id IN ({marks})"
        " ORDER BY deal_id",
        wanted,
    ).fetchall()
    return {int(row["deal_id"]): dict(row) for row in rows}


def read_comments(conn, deal_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Комментарии таймлайна по сделкам портфеля.

    Лидов здесь нет и быть не может: витрина комментарии лидов не хранит
    вовсе (`sync_comments` пишет `entity_type = 'deal'` жёстко). Фильтр по
    типу оставлен явным, чтобы это было видно читающему, а не выводилось
    из знания чужого модуля.
    """
    return _fetch_in(
        conn,
        "SELECT comment_id, entity_type, entity_id, author_id, body, is_auto,"
        " created_at FROM fact_comment"
        " WHERE entity_type = ? AND entity_id IN ({placeholders})",
        deal_ids,
        extra=(ENTITY_DEAL,),
    )


def read_activities(
    conn,
    deal_ids: Sequence[int],
    contact_ids: Sequence[int],
) -> list[dict[str, Any]]:
    """Дела по сделкам И по их контактам (раздел 4.1 ТЗ).

    Вторая половина не доборка, а большая часть: звонок в портале
    привязывают к контакту, а сделка ссылается на тот же контакт своим
    полем. Взяв только сделки, отчёт назвал бы молчащими тех, кто звонил.
    """
    columns = (
        "SELECT activity_id, owner_type_id, owner_id, provider_type_id, direction,"
        " subject, description, author_id, responsible_id, created_at,"
        " start_time, end_time, deadline, completed FROM fact_activity"
    )
    rows = _fetch_in(
        conn,
        columns + " WHERE owner_type_id = ? AND owner_id IN ({placeholders})",
        deal_ids,
        extra=(OWNER_TYPE_DEAL,),
    )
    rows.extend(_fetch_in(
        conn,
        columns + " WHERE owner_type_id = ? AND owner_id IN ({placeholders})",
        contact_ids,
        extra=(OWNER_TYPE_CONTACT,),
    ))
    return rows


def read_moves(conn, deal_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Движение сделок по стадиям, по порядку внутри карточки."""
    rows = _fetch_in(
        conn,
        "SELECT entity_type, entity_id, category_id, stage_id, entered_at, seq"
        " FROM fact_stage_event"
        " WHERE entity_type = ? AND entity_id IN ({placeholders})",
        deal_ids,
        extra=(ENTITY_DEAL,),
    )
    rows.sort(key=lambda row: (int(row["entity_id"]), int(row["seq"])))
    return rows


def read_users(conn) -> dict[int, dict[str, Any]]:
    """Справочник сотрудников: имя и отдел.

    Отдел нужен не для красоты: без него ручки не умеют соблюдать границу
    видимости, и РОП отдела A увидел бы клиентов отдела B `[V11]`.
    """
    rows = conn.execute(
        "SELECT user_id, name, last_name, department_id, department_name, is_active"
        " FROM dim_user"
    ).fetchall()
    return {int(row["user_id"]): dict(row) for row in rows}


def read_stages(conn) -> dict[tuple[str, int], str]:
    """Имена стадий по (идентификатор, воронка).

    Ключ составной: один и тот же `stage_id` встречается в разных воронках
    с разными именами, и витрина держит их парой не случайно.
    """
    rows = conn.execute("SELECT stage_id, category_id, name FROM dim_stage").fetchall()
    return {(row["stage_id"], int(row["category_id"])): row["name"] for row in rows}


def last_full_sync(conn) -> str | None:
    """Когда витрину в последний раз сверяли целиком.

    Комментарии закрытых сделок обновляются только полной сверкой. Без этой
    отметки «у клиента нет новых комментариев» неотличимо от «полная сверка
    не доходила» (раздел 14 ТЗ), и разбор писался бы по неполной истории.
    """
    row = conn.execute(
        "SELECT MAX(finished_at) FROM etl_run WHERE kind = 'full' AND status = 'ok'"
    ).fetchone()
    return row[0] if row else None


def read_portfolio(conn, categories: Iterable[int]) -> Portfolio:
    """Весь портфель одним заходом.

    Порядок неслучаен: контакты известны только после сделок, а дела на
    контактах — только после контактов.
    """
    deals = read_deals(conn, categories)
    deal_ids = tuple(sorted(deals))
    contact_ids = tuple(sorted({
        int(deal["contact_id"]) for deal in deals.values() if deal.get("contact_id")
    }))

    cards = tuple(
        Card(
            deal_id=deal_id,
            title=deals[deal_id].get("title") or "",
            stage_id=deals[deal_id].get("stage_id") or "",
            contact_id=int(deals[deal_id]["contact_id"]) if deals[deal_id].get("contact_id")
            else None,
        )
        for deal_id in deal_ids
    )

    portfolio = Portfolio(
        cards=cards,
        deals=deals,
        comments=tuple(read_comments(conn, deal_ids)),
        activities=tuple(read_activities(conn, deal_ids, contact_ids)),
        moves=tuple(read_moves(conn, deal_ids)),
        users=read_users(conn),
        stages=read_stages(conn),
        mart_full_sync_at=last_full_sync(conn),
    )
    logger.info(
        "Из витрины: сделок %d, контактов %d, комментариев %d, дел %d, движений %d",
        len(deals), len(contact_ids), len(portfolio.comments),
        len(portfolio.activities), len(portfolio.moves),
    )
    return portfolio
