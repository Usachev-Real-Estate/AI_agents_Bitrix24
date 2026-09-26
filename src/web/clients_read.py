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

from clients.schema import ISSUE_ORDER, TRIAGE_ORDER

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

    # Проблема тоже не у клиента, а рядом: их у него бывает несколько
    # сразу. EXISTS по той же причине, что и у карточек — соединение
    # размножило бы клиента по числу его проблем, и страница на сто строк
    # показала бы шестьдесят человек.
    issue = filters.get("issue")
    if issue not in (None, ""):
        clauses.append(
            "EXISTS (SELECT 1 FROM v_client_issue i"
            " WHERE i.client_key = c.client_key AND i.code = ?)"
        )
        params.append(issue)

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


# Порядок срочности из раздела 9 ТЗ. Строится из TRIAGE_ORDER, а не
# переписывается рядом: список, оторванный от набора значений, расходится с
# ним молча — новое состояние просто уезжает в конец, и никто не замечает,
# что оно там не по смыслу, а по недосмотру.
_RANK = " ".join(
    f"WHEN '{state}' THEN {index}" for index, state in enumerate(TRIAGE_ORDER)
)
TRIAGE_RANK = f"CASE c.triage_state {_RANK} ELSE {len(TRIAGE_ORDER)} END"


def page_of_clients(
    conn: sqlite3.Connection,
    *,
    filters: dict[str, Any] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Страница списка для экрана: строки и сколько их всего.

    Порядок другой, чем у ручки: там по ключу ради курсора, здесь по
    СРОЧНОСТИ — экран открывают, чтобы узнать, кого смотреть первым, и
    алфавит на этот вопрос не отвечает. Внутри одного состояния сверху
    те, кто молчит дольше.

    Клиент с непосчитанной тишиной оказывается в конце своего состояния
    сам собой: при `DESC` SQLite кладёт NULL последними. Это неявное
    правило движка, а не наше решение, и держится оно тестом — явная
    оговорка `silence_days IS NULL` тут ничего не меняет, и стоять рядом
    с комментарием, будто она что-то защищает, не должна.

    Всего — отдельным запросом, а не `len(rows)`: подпись «показаны первые
    сто из тысячи» и есть то, ради чего его считают.
    """
    where, params = _conditions(filters or {}, None)
    columns = ", ".join(f"c.{name}" for name in LIST_COLUMNS)
    rows = [
        dict(row) for row in conn.execute(
            f"SELECT {columns},"
            " (SELECT COUNT(*) FROM v_client_link l WHERE l.client_key = c.client_key)"
            " AS cards,"
            " rv.created_at AS reviewed_at, rv.reviewed_through"
            " FROM v_client c"
            f" LEFT JOIN ({_LAST_REVIEW}) rv ON rv.client_key = c.client_key"
            f"{where}"
            f" ORDER BY {TRIAGE_RANK}, c.silence_days DESC, c.client_key"
            " LIMIT ? OFFSET ?",
            (*params, max(1, int(limit)), max(0, int(offset))),
        )
    ]
    for row in rows:
        row["reviewed"] = _reviewed_state(row)

    total = conn.execute(
        "SELECT COUNT(*) FROM v_client c"
        f" LEFT JOIN ({_LAST_REVIEW}) rv ON rv.client_key = c.client_key{where}",
        params,
    ).fetchone()[0]
    return rows, total


def counts_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько клиентов в каждом состоянии — для шапки экрана.

    Считается по всей видимой книге, а не по текущей странице: число рядом
    с фильтром обязано говорить, сколько там всего, иначе фильтр незачем
    и открывать.
    """
    rows = conn.execute(
        "SELECT triage_state, COUNT(*) FROM v_client GROUP BY triage_state"
    ).fetchall()
    found = {str(state): int(count) for state, count in rows}
    return {state: found.get(state, 0) for state in TRIAGE_ORDER if found.get(state)}


def counts_by_issue(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько клиентов с каждой проблемой — вкладка «Исключения».

    Сумма счётчиков больше числа людей: у человека бывает пять проблем
    сразу. Так и задумано — счётчик отвечает «сколько таких случаев», а не
    «сколько людей всего», и экран говорит это вслух.

    Внутри кода дважды один клиент не встретится: `(client_key, code)` —
    первичный ключ таблицы, и `COUNT(DISTINCT client_key)` здесь дал бы
    ровно то же, что `COUNT(*)`. Лишнего DISTINCT нет намеренно: он
    выглядел бы защитой от того, чего схема не допускает.

    Нулевые коды остаются в ответе, в отличие от состояний. Пропавший
    счётчик читается как «такой проблемы у нас не бывает», а правда в том,
    что сегодня её нет ни у кого, — и это хорошая новость, которую видно
    только если строка на месте.
    """
    rows = conn.execute(
        "SELECT i.code, COUNT(*) FROM v_client_issue i GROUP BY i.code"
    ).fetchall()
    found = {str(code): int(count) for code, count in rows}
    return {code: found.get(code, 0) for code in ISSUE_ORDER}


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
        "issues": [
            str(row[0]) for row in conn.execute(
                "SELECT code FROM v_client_issue WHERE client_key = ?"
                " ORDER BY code", (client_key,),
            )
        ],
        "reviews": [
            _review(item) for item in conn.execute(
                "SELECT * FROM v_client_review WHERE client_key = ?"
                " ORDER BY created_at DESC", (client_key,),
            )
        ],
    }


def _review(row: sqlite3.Row) -> dict[str, Any]:
    """Разбор с разобранным списком проблем.

    В базе он строкой JSON — той же причины ради, что и payload события:
    колонок под заранее неизвестный список не бывает. Читателю нужен
    список, а не строка со списком внутри: разбирать её второй раз
    пришлось бы и экрану, и ручке, и они разошлись бы на первом же
    кривом значении.
    """
    item = dict(row)
    raw = item.pop("issues_json", "") or "[]"
    try:
        found = json.loads(raw)
    except (TypeError, ValueError):
        found = []
    item["issues"] = [str(value) for value in found] if isinstance(found, list) else []
    return item


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
