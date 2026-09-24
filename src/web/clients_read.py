"""Чтение книги клиентов для ручек раздела 7.2 ТЗ.

Запросы живут отдельно от обработчиков, как `objects.py` отдельно от
`routes_api.py`: HTTP меняется по одним причинам, а данные по другим.

Читается ТОЛЬКО через представления, которые создаёт `clients_scope`. Ни
одного обращения к `clients` напрямую здесь нет и быть не должно: на
соединении без области видимости такой запрос молча показал бы РОПу всю
компанию, а запрос к `v_client` — упадёт.

**Курсор по `client_key`, а не по `updated_at`** (раздел 7.2). Время
обновления переписывает та самая ночная пересборка, во время которой идёт
чтение: клиент, обновившийся между страницами, уехал бы в конец списка, и
читатель получил бы одних дважды, а других не увидел вовсе. Ключ не
меняется — кроме переезда, а переезд удаляет строку целиком и потому
страницу не рвёт.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterator

# Потолок страницы из раздела 7.2. Читатель просит сколько хочет, отдаётся
# не больше: ручка отдаёт поток, и страница в десять тысяч строк держала бы
# соединение с книгой открытым всё время её разбора.
PAGE_DEFAULT = 500
PAGE_MAX = 2000

# Состояния разбора для фильтра `reviewed` (раздел 7.3).
REVIEWED_YES = "yes"
REVIEWED_NO = "no"
REVIEWED_STALE = "stale"

# Поля клиента, уходящие в список. Перечислены поимённо, а не `SELECT *`:
# в книге лежит `phone_raw`, и «звёздочка» вынесла бы наружу телефон в том
# виде, в каком его записал брокер, вместе со всем, что он туда дописал.
LIST_COLUMNS = (
    "client_key", "phone_norm", "is_agent", "agent_reason", "key_reason",
    "contact_id", "name", "assignee_id", "assignee_name", "assignee_count",
    "department_id", "triage_state", "triage_reason", "last_touch_at",
    "last_touch_kind", "last_event_at", "silence_days", "next_step_at",
    "next_step_overdue", "calls_total", "comments_total",
    "comments_by_assignee", "comments_by_assignee_30d", "updated_at",
)

# Разбор клиента: последний по времени. `reviewed_through` нужен фильтру
# `stale`, и сравнивается он строго — равенство означает «разобрано ровно
# по это событие» (раздел 7.3).
_LAST_REVIEW = """
SELECT r.client_key, r.created_at, r.reviewed_through, r.verdict, r.enough_data
  FROM v_client_review r
 WHERE r.created_at = (
       SELECT MAX(created_at) FROM v_client_review
        WHERE client_key = r.client_key)
"""


def _clamp_limit(value: Any) -> int:
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        return PAGE_DEFAULT
    return max(1, min(wanted, PAGE_MAX))


def list_clients(
    conn: sqlite3.Connection,
    *,
    filters: dict[str, Any] | None = None,
    limit: Any = PAGE_DEFAULT,
    cursor: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Страница списка клиентов, по одному словарю на строку.

    Итератор, а не список: ручка отдаёт JSONL потоком, и собирать две
    тысячи строк в память, чтобы тут же их отдать, незачем.
    """
    where, params = _conditions(filters or {}, cursor)
    columns = ", ".join(f"c.{name}" for name in LIST_COLUMNS)
    rows = conn.execute(
        f"SELECT {columns},"
        " (SELECT COUNT(*) FROM v_client_link l WHERE l.client_key = c.client_key)"
        " AS cards,"
        " rv.created_at AS reviewed_at, rv.reviewed_through, rv.verdict"
        " FROM v_client c"
        f" LEFT JOIN ({_LAST_REVIEW}) rv ON rv.client_key = c.client_key"
        f"{where}"
        " ORDER BY c.client_key"
        " LIMIT ?",
        (*params, _clamp_limit(limit)),
    )
    for row in rows:
        item = dict(row)
        item["reviewed"] = _reviewed_state(item)
        yield item


def _reviewed_state(row: dict[str, Any]) -> str:
    """`yes`, `no` или `stale` (раздел 7.3).

    Сравнение строгое: равенство означает «разобрано ровно по это
    событие». Известное ограничение этапа 1 — опоздавший комментарий с
    более ранней отметкой времени `last_event_at` не сдвинет, и разбор
    останется помеченным свежим, хотя прочитано не всё.
    """
    if not row.get("reviewed_at"):
        return REVIEWED_NO
    through, last = row.get("reviewed_through") or "", row.get("last_event_at") or ""
    return REVIEWED_STALE if last > through else REVIEWED_YES


def _conditions(filters: dict[str, Any], cursor: str | None) -> tuple[str, list[Any]]:
    """Условия запроса из параметров строки. Пустое значение фильтром не является."""
    clauses: list[str] = []
    params: list[Any] = []

    for column, key in (
        ("triage_state", "triage_state"),
        ("assignee_id", "assignee_id"),
        ("department_id", "department_id"),
    ):
        value = filters.get(key)
        if value not in (None, ""):
            clauses.append(f"c.{column} = ?")
            params.append(value)

    if filters.get("is_agent") not in (None, ""):
        clauses.append("c.is_agent = ?")
        params.append(int(bool(filters["is_agent"])))

    silence = filters.get("silence_gt")
    if silence not in (None, ""):
        # Клиент без посчитанной тишины в фильтр по тишине не попадает:
        # NULL означает «не считали», и выдавать его за «молчит дольше
        # N дней» значит придумывать за прогон, который не состоялся.
        clauses.append("c.silence_days IS NOT NULL AND c.silence_days > ?")
        params.append(int(silence))

    # Карточка и стадия живут у связей, а не у клиента: у него их
    # несколько. Условие «есть хотя бы одна такая» — это EXISTS, а не
    # соединение: соединение размножило бы клиента по числу карточек.
    for column, key in (("category_id", "category_id"), ("stage_id", "stage_id")):
        value = filters.get(key)
        if value not in (None, ""):
            clauses.append(
                f"EXISTS (SELECT 1 FROM v_client_link l"
                f" WHERE l.client_key = c.client_key AND l.{column} = ?)"
            )
            params.append(value)

    reviewed = filters.get("reviewed")
    if reviewed == REVIEWED_NO:
        clauses.append("rv.created_at IS NULL")
    elif reviewed == REVIEWED_YES:
        clauses.append("rv.created_at IS NOT NULL")
        clauses.append("COALESCE(c.last_event_at, '') <= COALESCE(rv.reviewed_through, '')")
    elif reviewed == REVIEWED_STALE:
        clauses.append("rv.created_at IS NOT NULL")
        clauses.append("COALESCE(c.last_event_at, '') > COALESCE(rv.reviewed_through, '')")

    if cursor:
        clauses.append("c.client_key > ?")
        params.append(cursor)

    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def read_client(conn: sqlite3.Connection, client_key: str) -> dict[str, Any] | None:
    """Клиент целиком: он сам, карточки, вся лента и все разборы.

    Ручка для машины (раздел 7.2): ничего не обрезается и не выбирается по
    важности. Обрезав здесь, мы решали бы за читателя, какая часть истории
    ему нужна, — а он затем и приходит, что этого не знает заранее.
    """
    row = conn.execute(
        "SELECT * FROM v_client WHERE client_key = ?", (client_key,),
    ).fetchone()
    if row is None:
        return None

    client = dict(row)
    return {
        "client": client,
        "cards": [
            dict(item) for item in conn.execute(
                "SELECT * FROM v_client_link WHERE client_key = ?"
                " ORDER BY entity_type, entity_id", (client_key,),
            )
        ],
        "events": [
            _event(item) for item in conn.execute(
                "SELECT * FROM v_client_event WHERE client_key = ?"
                " ORDER BY at, id", (client_key,),
            )
        ],
        "reviews": [
            dict(item) for item in conn.execute(
                "SELECT * FROM v_client_review WHERE client_key = ?"
                " ORDER BY created_at DESC", (client_key,),
            )
        ],
    }


def _event(row: sqlite3.Row) -> dict[str, Any]:
    """Событие ленты с разобранным payload.

    Читателю нужен словарь, а не строка с JSON внутри строки: разбирать её
    второй раз пришлось бы каждому, кто ручку вызывает.
    """
    item = dict(row)
    raw = item.pop("payload_json", "") or "{}"
    try:
        item["payload"] = json.loads(raw)
    except ValueError:
        # Битый JSON не повод не отдать событие: время, вид и автор у него
        # целы, а это уже история.
        item["payload"] = {}
    return item


def client_of_call(conn: sqlite3.Connection, activity_id: int) -> str | None:
    """Чей это звонок. ``None`` — в области видимости такого звонка нет.

    По нему ручка расшифровки решает, отдавать ли текст: сам текст лежит в
    базе аудита, где никакой области видимости нет вовсе, и спросить её
    больше не у кого.
    """
    row = conn.execute(
        "SELECT client_key FROM v_client_event"
        " WHERE kind = 'call' AND source_id = ? LIMIT 1",
        (str(activity_id),),
    ).fetchone()
    return row[0] if row else None
