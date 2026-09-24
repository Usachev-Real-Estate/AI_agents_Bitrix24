"""Чтение книги клиентов: фильтры, страницы и полная история.

Самое дорогое здесь — курсор. Страница листается по `client_key`, а не по
`updated_at`, и это не вкусовщина: время обновления переписывает та самая
ночная пересборка, во время которой идёт чтение. Клиент, обновившийся между
двумя страницами, уехал бы в конец списка — читатель получил бы одних
дважды, а других не увидел бы вовсе и не узнал об этом.

Второе по цене — `SELECT` поимённо. В книге лежит `phone_raw`: номер в том
виде, в каком его записал брокер, вместе со всем, что он дописал рядом.
«Звёздочка» вынесла бы это в список, который уходит в браузер и в выгрузки.
"""

import sqlite3

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import clients_read as read
from analytics.scope import Scope
from clients.schema import clients_session, init_clients_db
from clients_scope import scoped_clients


@pytest.fixture
def book(tmp_path, monkeypatch):
    """Книга из трёх клиентов с карточками, лентой и разбором."""
    from config import get_settings

    path = tmp_path / "clients.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(path))
    get_settings.cache_clear()
    init_clients_db(path)
    with clients_session(path) as conn:
        conn.executemany(
            "INSERT INTO clients(client_key, department_id, assignee_id, is_agent,"
            " triage_state, silence_days, last_event_at, phone_raw, name, updated_at)"
            " VALUES (?, 5, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("p:1", 10, 0, "abandoned", 40, "2026-09-01T00:00:00+00:00",
                 "+7 900 111-22-33 (жена Ольга, звонить после 18)", "Первый", "в"),
                ("p:2", 20, 0, "moving", 2, "2026-09-20T00:00:00+00:00", "", "Второй", "а"),
                ("p:3", 20, 1, "cooling", None, None, "", "Третий", "б"),
            ],
        )
        conn.executemany(
            "INSERT INTO client_links(client_key, entity_type, entity_id, category_id,"
            " stage_id) VALUES (?, 'deal', ?, ?, ?)",
            [("p:1", 1, 18, "C18:NEW"), ("p:2", 2, 0, "C0:NEW"),
             ("p:2", 3, 18, "C18:NEW"), ("p:2", 4, 18, "C18:NEW")],
        )
        conn.execute(
            "INSERT INTO client_events(client_key, at, kind, source_id, entity_type,"
            " entity_id, payload_json) VALUES ('p:1', '2026-09-01T00:00:00+00:00',"
            " 'call', '777', 'deal', 1, '{\"direction\": 1}')"
        )
        conn.executemany(
            "INSERT INTO client_reviews(client_key, created_at, reviewed_through,"
            " summary) VALUES (?, ?, ?, 'разобрано')",
            [("p:1", "2026-09-02T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
             ("p:2", "2026-09-21T00:00:00+00:00", "2026-09-20T00:00:00+00:00")],
        )
    yield path
    get_settings.cache_clear()


@pytest.fixture
def conn(book):
    with scoped_clients(Scope.everything()) as connection:
        connection.row_factory = sqlite3.Row
        yield connection


def _keys(rows) -> list[str]:
    return [row["client_key"] for row in rows]


# ── страницы ───────────────────────────────────────────────────────────

def test_the_page_is_ordered_and_continued_by_key(conn):
    """Список отсортирован по ключу, и страницы стыкуются без нахлёста.

    Порядок обновления в книге нарочно обратен порядку ключей: иначе
    сортировка по `updated_at` выглядела бы точно так же, и подмена
    осталась бы незамеченной до первой ночной пересборки.
    """
    first = list(read.list_clients(conn, limit=2))
    rest = list(read.list_clients(conn, cursor=first[-1]["client_key"]))

    assert _keys(first) == ["p:1", "p:2"]
    assert _keys(rest) == ["p:3"]


def test_a_client_updated_between_pages_does_not_move(book, conn):
    """Пересборка, прошедшая между страницами, список не рвёт.

    Ради этого курсор и по ключу. Сортировка по `updated_at` отправила бы
    обновлённого клиента в конец: читатель получил бы его дважды, а того,
    кто занял освободившееся место, — ни разу.
    """
    first = list(read.list_clients(conn, limit=1))
    with clients_session(book) as writer:
        writer.execute("UPDATE clients SET updated_at = 'я' WHERE client_key = 'p:1'")

    rest = list(read.list_clients(conn, cursor=first[-1]["client_key"]))

    assert _keys(first) == ["p:1"]
    assert _keys(rest) == ["p:2", "p:3"], "обновлённый клиент не всплыл и никого не вытеснил"


@pytest.mark.parametrize("asked, expected", [
    (None, read.PAGE_DEFAULT), ("не число", read.PAGE_DEFAULT),
    (0, 1), (-5, 1), (10, 10), (99999, read.PAGE_MAX),
])
def test_the_page_size_is_clamped(asked, expected):
    """Читатель просит сколько хочет, отдаётся не больше потолка.

    Ручка отдаёт поток, и страница в десять тысяч строк держала бы
    соединение с книгой открытым всё время её разбора.
    """
    assert read._clamp_limit(asked) == expected


# ── что уходит наружу ──────────────────────────────────────────────────

def test_the_raw_phone_never_leaves_in_the_list(conn):
    """Телефон в том виде, как его записал брокер, наружу не уходит.

    В `phone_raw` попадает всё, что дописано рядом с номером: имя жены,
    когда звонить, чей это вообще телефон. Список уходит в браузер и в
    выгрузки; нормализованного номера для работы достаточно.
    """
    assert "phone_raw" not in read.LIST_COLUMNS

    row = next(iter(read.list_clients(conn)))

    assert "phone_raw" not in row
    assert "жена Ольга" not in repr(row)


def test_the_list_says_how_many_cards_the_client_has(conn):
    """Число карточек считается, а сами карточки в список не едут."""
    rows = {row["client_key"]: row["cards"] for row in read.list_clients(conn)}

    assert rows == {"p:1": 1, "p:2": 3, "p:3": 0}


def test_one_client_stays_one_row_however_many_cards(conn):
    """Клиент с двумя карточками — одна строка, а не две.

    Условие «есть карточка такой воронки» — это EXISTS, а не соединение:
    соединение размножило бы клиента по числу карточек, и список показал бы РОПу
    одного человека дважды.
    """
    rows = list(read.list_clients(conn, filters={"category_id": 18}))

    assert _keys(rows) == ["p:1", "p:2"]


# ── фильтры ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("filters, expected", [
    ({"triage_state": "abandoned"}, ["p:1"]),
    ({"assignee_id": 20}, ["p:2", "p:3"]),
    ({"is_agent": 1}, ["p:3"]),
    ({"is_agent": 0}, ["p:1", "p:2"]),
    ({"category_id": 0}, ["p:2"]),
    ({"stage_id": "C18:NEW"}, ["p:1", "p:2"]),
    ({}, ["p:1", "p:2", "p:3"]),
    ({"triage_state": ""}, ["p:1", "p:2", "p:3"]),
])
def test_the_filters_narrow_the_list(conn, filters, expected):
    """Пустое значение фильтром не является — иначе «все» давало бы ноль."""
    assert _keys(read.list_clients(conn, filters=filters)) == expected


def test_a_client_without_counted_silence_is_not_silent(conn):
    """NULL в тишине — это «не считали», а не «молчит вечно».

    Выдав его за молчащего, фильтр поставил бы в очередь к брокеру
    клиента, про которого прогон ещё ничего не сказал.
    """
    rows = read.list_clients(conn, filters={"silence_gt": 10})

    assert _keys(rows) == ["p:1"]


# ── разобран / устарел ─────────────────────────────────────────────────

def test_a_review_older_than_the_last_event_is_stale(conn):
    """Событие новее разбора — разбор устарел (раздел 7.3)."""
    rows = {row["client_key"]: row["reviewed"] for row in read.list_clients(conn)}

    assert rows == {"p:1": read.REVIEWED_STALE, "p:2": read.REVIEWED_YES,
                    "p:3": read.REVIEWED_NO}


def test_a_review_exactly_up_to_the_last_event_is_fresh(book, conn):
    """Равенство — это «разобрано ровно по это событие», а не «устарело».

    Сравнение строгое. Нестрогое объявляло бы устаревшим каждый разбор в
    ту же секунду, как его записали.
    """
    with clients_session(book) as writer:
        writer.execute(
            "UPDATE client_reviews SET reviewed_through = '2026-09-01T00:00:00+00:00'"
            " WHERE client_key = 'p:1'"
        )

    rows = {row["client_key"]: row["reviewed"] for row in read.list_clients(conn)}

    assert rows["p:1"] == read.REVIEWED_YES


@pytest.mark.parametrize("wanted, expected", [
    ("no", ["p:3"]), ("yes", ["p:2"]), ("stale", ["p:1"]),
])
def test_the_reviewed_filter_matches_what_the_row_says(conn, wanted, expected):
    """Фильтр и колонка отвечают одинаково.

    Считаются они в разных местах — фильтр в SQL, колонка в питоне, — и
    разойтись им проще всего: список «неразобранных» показывал бы строки,
    помеченные «разобран».
    """
    rows = list(read.list_clients(conn, filters={"reviewed": wanted}))

    assert _keys(rows) == expected
    assert all(row["reviewed"] == wanted for row in rows)


# ── клиент целиком ─────────────────────────────────────────────────────

def test_the_whole_client_holds_everything_he_has(conn):
    """Ручка для машины отдаёт всё: карточки, ленту, разборы."""
    got = read.read_client(conn, "p:1")

    assert got["client"]["client_key"] == "p:1"
    assert len(got["cards"]) == 1
    assert len(got["events"]) == 1
    assert len(got["reviews"]) == 1


def test_the_event_payload_arrives_parsed(conn):
    """Словарь, а не строка с JSON внутри строки."""
    event = read.read_client(conn, "p:1")["events"][0]

    assert event["payload"] == {"direction": 1}
    assert "payload_json" not in event


def test_a_broken_payload_does_not_hide_the_event(book, conn):
    """Битый JSON не повод не отдать событие.

    Время, вид и автор у него целы, а это уже история. Уронив на нём
    ручку, мы потеряли бы всю ленту из-за одной строки.
    """
    with clients_session(book) as writer:
        writer.execute("UPDATE client_events SET payload_json = 'не json' WHERE id = 1")

    event = read.read_client(conn, "p:1")["events"][0]

    assert event["payload"] == {}
    assert event["kind"] == "call"


def test_a_client_outside_the_scope_simply_is_not_there(book):
    """Чужого клиента ручка не находит, а не отдаёт с пустыми полями."""
    with scoped_clients(Scope.departments([99])) as narrow:
        narrow.row_factory = sqlite3.Row

        assert read.read_client(narrow, "p:1") is None
        assert list(read.list_clients(narrow)) == []


# ── чей звонок ─────────────────────────────────────────────────────────

def test_a_call_is_traced_back_to_its_client(conn):
    """По номеру звонка находится клиент — иначе текст отдавать некому."""
    assert read.client_of_call(conn, 777) == "p:1"


def test_a_call_outside_the_scope_has_no_owner(book):
    """Звонок чужого отдела не принадлежит никому.

    Текст лежит в базе аудита, где области видимости нет вовсе, и
    спросить её больше не у кого: ответ этой функции — единственное, что
    стоит между РОПом и чужим разговором.
    """
    with scoped_clients(Scope.departments([99])) as narrow:
        assert read.client_of_call(narrow, 777) is None


def test_an_unknown_call_has_no_owner(conn):
    """Несуществующий звонок ничьим не объявляется."""
    assert read.client_of_call(conn, 999) is None


# ── порядок на экране: по срочности, а не по алфавиту ──────────────────

def test_the_screen_orders_by_urgency_and_not_by_key(conn):
    """Экран открывают, чтобы узнать, кого смотреть первым.

    Порядок здесь другой, чем у ручки: там по ключу ради курсора, здесь по
    срочности состояния. Алфавит на вопрос «кого первым» не отвечает, а
    выглядит ровно так же правдоподобно — список отсортирован, строки на
    месте, и понять, что сортировка не та, можно только зная, какой она
    должна быть.
    """
    rows, _ = read.page_of_clients(conn)

    assert _keys(rows) == ["p:1", "p:3", "p:2"], "брошен, остыл, движется"


def test_inside_one_state_the_longest_silence_comes_first(book, conn):
    """Внутри состояния сверху те, кто молчит дольше."""
    with clients_session(book) as writer:
        writer.execute(
            "UPDATE clients SET triage_state = 'abandoned', silence_days = 100"
            " WHERE client_key = 'p:2'"
        )

    rows, _ = read.page_of_clients(conn)

    assert _keys(rows)[:2] == ["p:2", "p:1"], "сто дней молчания раньше сорока"


def test_a_client_without_counted_silence_does_not_lead_the_state(book, conn):
    """Непосчитанная тишина не выдаёт себя за самую долгую.

    Держится это на неявном правиле движка: при `DESC` SQLite кладёт NULL
    последними (при `ASC` — первыми). Оговорки в запросе нет нарочно, она
    ничего не меняет; но правило чужое, и менять сортировку на возрастание
    или переезжать на другой движок можно только сломав этот тест.

    Цена ошибки: клиент, которого прогон ещё не считал, встал бы во главе
    списка брошенных — то есть первым, кому звонить.
    """
    with clients_session(book) as writer:
        writer.execute(
            "UPDATE clients SET triage_state = 'abandoned', silence_days = NULL"
            " WHERE client_key = 'p:3'"
        )

    rows, _ = read.page_of_clients(conn)

    assert _keys(rows)[0] == "p:1", "посчитанные сорок дней раньше непосчитанного"


def test_the_screen_says_how_many_there_are_in_total(conn):
    """Всего — отдельным числом, а не длиной страницы.

    Подпись «показаны первые сто из тысячи» и есть то, ради чего его
    считают: без неё страница выглядит как весь портфель.
    """
    rows, total = read.page_of_clients(conn, limit=1)

    assert len(rows) == 1
    assert total == 3


def test_the_counts_cover_the_whole_book_not_the_page(conn):
    """Числа у фильтров считаются по всей книге, а не по странице.

    Число рядом с фильтром обязано говорить, сколько там всего, иначе
    фильтр незачем и открывать.
    """
    counts = read.counts_by_state(conn)

    assert counts == {"abandoned": 1, "cooling": 1, "moving": 1}


def test_an_empty_state_does_not_clutter_the_filters(book, conn):
    """Состояние, в котором никого нет, из шапки пропадает."""
    with clients_session(book) as writer:
        writer.execute("UPDATE clients SET triage_state = 'moving'")

    assert read.counts_by_state(conn) == {"moving": 3}
