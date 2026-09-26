"""Ручки книги клиентов: кто что видит и чего не видит никогда.

Область видимости проверена отдельно на уровне соединения. Здесь
проверяется, что ручки ею действительно пользуются, — и главное, что
расшифровка чужого звонка не отдаётся.

Текст звонка лежит в базе аудита, где области видимости нет вовсе. Право
спрашивается у книги клиентов и спрашивается ПЕРВЫМ; обратный порядок отдал
бы чужой разговор тому, кто угадал номер, а номера у звонков подряд идущие.
"""

import json
import os
import re
import sqlite3

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import store
from app import create_app
import clients_read
from clients.schema import (
    ISSUE_ORDER, clients_session, init_clients_db,
)
from clients_scope import scoped_clients
from context import Scope
from config import get_settings

BASE = "/dashboard"
PASSWORD = "correct-horse-battery"
DEPT_A, DEPT_B = 44, 50
CALL_A, CALL_B = 777, 888


@pytest.fixture
def app(tmp_path, analytics_db, monkeypatch):
    """Дашборд с книгой из двух клиентов в разных отделах."""
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    book = tmp_path / "clients.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(book))
    get_settings.cache_clear()

    init_clients_db(book)
    with clients_session(book) as conn:
        conn.executemany(
            "INSERT INTO clients(client_key, department_id, name, phone_norm,"
            " phone_raw, triage_state, calls_total, calls_with_transcript,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')",
            [
                ("p:+79001112233", DEPT_A, "Свой", "+79001112233",
                 "+7 900 111-22-33 (жена Ольга)", "abandoned", 4, 1),
                # Чужой не считан ни разу: у него колонки пустые.
                ("c:88", DEPT_B, "Чужой", None, "", "moving", None, None),
            ],
        )
        conn.executemany(
            "INSERT INTO client_events(client_key, at, kind, source_id, entity_type,"
            " entity_id, payload_json) VALUES (?, '2026-09-01T00:00:00+00:00',"
            " 'call', ?, 'deal', 1, '{\"direction\": 1}')",
            [("p:+79001112233", str(CALL_A)), ("c:88", str(CALL_B))],
        )

    # Кэш расшифровок — там же, где его ищет продукт.
    cache = tmp_path / "violations.db"
    conn = sqlite3.connect(cache)
    conn.execute(
        "CREATE TABLE call_transcripts (activity_id INTEGER PRIMARY KEY,"
        " deal_id INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '',"
        " status TEXT NOT NULL, fetched_at TEXT NOT NULL DEFAULT '',"
        " chars INTEGER NOT NULL DEFAULT 0,"
        " activity_created TEXT NOT NULL DEFAULT '')"
    )
    conn.executemany(
        "INSERT INTO call_transcripts(activity_id, deal_id, text, status)"
        " VALUES (?, 1, ?, 'ok')",
        [(CALL_A, "разговор своего клиента"), (CALL_B, "СЕКРЕТ ЧУЖОГО ОТДЕЛА")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("transcripts_read.DEFAULT_DB_PATH", cache)

    application = create_app()
    store.create_user("boss", PASSWORD, "Директор", role="admin")
    store.create_user("rop_a", PASSWORD, "РОП А", role="rop", department_ids=[DEPT_A])
    yield application
    get_settings.cache_clear()


def _screen(response) -> str:
    """Текст страницы так, как его увидит человек: без тегов и переносов.

    Проверять вёрстку подстрокой — значит ломать тест каждым переносом
    строки в шаблоне и каждым `<span>`, добавленным ради оформления.
    Утверждение должно держаться за фразу, а не за разметку вокруг неё.
    """
    return " ".join(re.sub(r"<[^>]+>", " ", response.text).lower().split())


def _login(app, username: str) -> TestClient:
    session = TestClient(app, follow_redirects=False)
    session.get(f"{BASE}/login")
    response = session.post(f"{BASE}/login", data={
        "username": username, "password": PASSWORD,
        "csrf_token": session.cookies.get("dash_csrf"), "next": "",
    })
    assert response.status_code == 303, response.text[:300]
    return session


@pytest.fixture
def admin(app):
    return _login(app, "boss")


@pytest.fixture
def rop_a(app):
    return _login(app, "rop_a")


def _rows(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


# ── без входа ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/clients", "/api/clients/p:+79001112233", f"/api/calls/{CALL_A}/transcript",
])
def test_an_anonymous_request_gets_a_refusal_and_not_a_login_page(app, path):
    """Аноним получает 401 и пустое тело, а не HTML формы входа.

    Ручки нарочно живут под `/api`: вне его `RequireAuthMiddleware`
    отвечает 303 на форму, и программа-читатель разбирала бы страницу
    входа как данные — то есть считала бы, что клиентов в книге ноль.
    """
    guest = TestClient(app, follow_redirects=False)

    response = guest.get(f"{BASE}{path}")

    assert response.status_code == 401
    assert "СЕКРЕТ" not in response.text


# ── книги ещё нет ──────────────────────────────────────────────────────

@pytest.fixture
def app_without_book(tmp_path, analytics_db, monkeypatch):
    """Дашборд там, где клиентский слой ни разу не собирался."""
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("CLIENTS_DB_PATH", str(tmp_path / "которой-нет.db"))
    get_settings.cache_clear()
    application = create_app()
    store.create_user("boss", PASSWORD, "Директор", role="admin")
    yield application
    get_settings.cache_clear()


@pytest.mark.parametrize("path", [
    "/api/clients", "/api/clients/p:+79001112233", f"/api/calls/{CALL_A}/transcript",
])
def test_a_dashboard_without_a_book_explains_itself(app_without_book, path):
    """Книги нет — 503 и объяснение словами, а не трассировка.

    Дашборд живёт независимо от ночной пересборки: его поднимают раньше,
    чем слой собрался хоть раз, и CLIENTS_DB_PATH может смотреть не туда.
    Пятисотка показала бы человеку трассировку вместо ответа, а пустой
    список соврал бы: «книга не собрана» и «клиентов ноль» — разные вещи,
    и по второму РОП сделал бы вывод, что работать не с кем.
    """
    session = _login(app_without_book, "boss")

    response = session.get(f"{BASE}{path}")

    assert response.status_code == 503
    assert response.json()["error"] == "clients_book_missing"
    assert "build.py" in response.json()["detail"], "ответ говорит, что делать"


def test_the_missing_book_is_noticed_before_the_stream_starts(app_without_book):
    """Отсутствие книги обнаруживается до первого байта потока.

    Список отдаётся потоком. Узнав о пропаже внутри генератора, ответить
    кодом было бы уже нечем: заголовки со статусом 200 ушли читателю, и
    тот получил бы пустой успешный ответ вместо отказа.
    """
    session = _login(app_without_book, "boss")

    response = session.get(f"{BASE}/api/clients")

    assert response.status_code == 503
    assert not response.headers["content-type"].startswith("application/x-ndjson")


# ── список ─────────────────────────────────────────────────────────────

def test_the_list_comes_back_as_lines_of_json(admin):
    """JSONL: по клиенту на строку, читается по мере поступления."""
    response = admin.get(f"{BASE}/api/clients")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert len(_rows(response)) == 2


def test_a_head_of_department_gets_only_his_own_clients(rop_a):
    """РОП видит свой отдел и не догадывается о чужом."""
    rows = _rows(rop_a.get(f"{BASE}/api/clients"))

    assert [row["client_key"] for row in rows] == ["p:+79001112233"]


def test_the_raw_phone_does_not_reach_the_browser(admin):
    """Телефон с дописками брокера в список не уходит."""
    assert "жена Ольга" not in admin.get(f"{BASE}/api/clients").text


def test_the_filters_reach_the_query(admin):
    """Параметр строки запроса сужает выборку."""
    rows = _rows(admin.get(f"{BASE}/api/clients?triage_state=moving"))

    assert [row["client_key"] for row in rows] == ["c:88"]


# ── клиент целиком ─────────────────────────────────────────────────────

def test_a_phone_key_survives_the_url(admin):
    """Ключ `p:+7…` доезжает до обработчика целиком.

    Плюс и двоеточие в пути — обычное дело для телефонного ключа, и
    обычный параметр пути вернул бы 404 на ровном месте.
    """
    response = admin.get(f"{BASE}/api/clients/p:+79001112233")

    assert response.status_code == 200
    assert response.json()["client"]["client_key"] == "p:+79001112233"


def test_the_whole_client_carries_his_history(admin):
    """Ручка для машины отдаёт клиента, карточки, ленту и разборы."""
    got = admin.get(f"{BASE}/api/clients/p:+79001112233").json()

    assert set(got) == {"client", "cards", "events", "reviews", "issues"}
    assert got["events"][0]["payload"] == {"direction": 1}


def test_a_foreign_client_is_simply_not_found(rop_a):
    """Чужой клиент отвечает 404, а не 403.

    Разные ответы превратили бы ручку в перечислитель: по 404 против 403
    можно узнать, какие ключи в книге есть, не увидев ни одного.
    """
    response = rop_a.get(f"{BASE}/api/clients/c:88")

    assert response.status_code == 404
    assert "Чужой" not in response.text


# ── расшифровка ────────────────────────────────────────────────────────

def test_the_transcript_of_your_own_call_comes_back(rop_a):
    """Свой звонок отдаёт текст."""
    response = rop_a.get(f"{BASE}/api/calls/{CALL_A}/transcript")

    assert response.status_code == 200
    assert response.json()["text"] == "разговор своего клиента"


def test_a_foreign_call_never_gives_up_its_transcript(rop_a):
    """Чужой разговор не отдаётся ни при каких условиях.

    Самая дорогая проверка этого файла. Текст лежит в базе аудита, где
    области видимости нет вовсе: если спросить её раньше, чем книгу, то
    номер звонка — а они идут подряд — станет ключом к чужим разговорам.
    """
    response = rop_a.get(f"{BASE}/api/calls/{CALL_B}/transcript")

    assert response.status_code == 404
    assert "СЕКРЕТ ЧУЖОГО ОТДЕЛА" not in response.text


def test_an_unknown_call_is_not_found(admin):
    """Несуществующий звонок — 404, и администратору тоже."""
    assert admin.get(f"{BASE}/api/calls/999999/transcript").status_code == 404


def test_a_call_without_a_transcript_says_so_without_guessing(admin, tmp_path):
    """Звонок есть, текста нет — 404 и `text: null`.

    Почему текста нет — очередь не дошла, расшифровка не получилась, кэш
    недоступен — читателю безразлично: читать нечего. Различать это в
    ответе значило бы рассказывать про устройство очереди тому, кто
    спросил про звонок.
    """
    with sqlite3.connect(tmp_path / "violations.db") as conn:
        conn.execute("UPDATE call_transcripts SET status = 'error'"
                     f" WHERE activity_id = {CALL_A}")

    response = admin.get(f"{BASE}/api/calls/{CALL_A}/transcript")

    assert response.status_code == 404
    assert response.json()["text"] is None


# ── экраны ─────────────────────────────────────────────────────────────

def test_the_list_screen_shows_the_clients(admin):
    """Вкладка «Клиенты» открывается и показывает список."""
    response = admin.get(f"{BASE}/clients")

    assert response.status_code == 200
    assert "Свой" in response.text and "Чужой" in response.text


def test_the_list_screen_keeps_a_foreign_department_away(rop_a):
    """На экране РОПа чужого клиента нет — как и в ручке."""
    body = rop_a.get(f"{BASE}/clients").text

    assert "Свой" in body
    assert "Чужой" not in body


def test_the_raw_phone_is_not_printed_in_the_list_screen(admin):
    """Дописки брокера к номеру в таблицу не попадают.

    На карточке одного человека они нужны и показываются; список уходит
    целиком и его выгружают.
    """
    assert "жена Ольга" not in admin.get(f"{BASE}/clients").text


def test_the_state_filter_narrows_the_screen(admin):
    """Фишка состояния в шапке сужает список."""
    body = admin.get(f"{BASE}/clients?triage_state=moving").text

    assert "Чужой" in body
    assert "Свой" not in body


def test_the_client_screen_opens_by_its_phone_key(admin):
    """Страница клиента открывается по ключу `p:+7…` целиком."""
    response = admin.get(f"{BASE}/clients/p:+79001112233")

    assert response.status_code == 200
    assert "Свой" in response.text


def test_the_client_screen_does_not_render_the_transcript(admin):
    """Текст звонка в ленту не рендерится — только ссылка.

    У клиента с сорока звонками страница иначе весила бы мегабайты и
    открывалась бы соответственно (раздел 9 ТЗ).
    """
    body = admin.get(f"{BASE}/clients/p:+79001112233").text

    assert "разговор своего клиента" not in body
    assert f"/api/calls/{CALL_A}/transcript" in body


def test_a_foreign_client_screen_is_not_found(rop_a):
    """Чужая карточка не открывается и ничего о себе не сообщает."""
    response = rop_a.get(f"{BASE}/clients/c:88")

    assert response.status_code == 404
    assert "Чужой" not in response.text


def test_the_screen_without_a_book_explains_itself(app_without_book):
    """Экран без книги объясняет словами, а не показывает пустую таблицу.

    Пустая таблица читается как «работать не с кем» — вывод, который РОП
    сделает, а поправить будет нечем.
    """
    session = _login(app_without_book, "boss")

    response = session.get(f"{BASE}/clients")

    assert response.status_code == 200
    assert "не собрана" in response.text
    assert "Ничего не найдено" not in response.text


def test_the_menu_has_the_clients_tab(admin):
    """Пункт меню есть, и он ведёт на экран, а не в никуда."""
    body = admin.get(f"{BASE}/clients").text

    assert f'href="{BASE}/clients' in body


def test_the_client_screen_says_how_much_there_is_to_read(admin):
    """«Расшифровано 1 из 4 звонков» — прежде, чем верить любому разбору.

    Число стоит рядом с состоянием не для красоты: разбор клиента, у
    которого не записано ни одного разговора, опирается на даты и чужие
    пометки, и отличить такой разбор от опирающегося на слова клиента
    брокер обязан с первого взгляда.
    """
    page = _screen(admin.get(f"{BASE}/clients/p:+79001112233"))

    assert "расшифровано 1 из 4 звонков" in page


def test_a_client_nobody_counted_does_not_claim_to_have_no_calls(admin):
    """Пустая колонка читается как «не считано», а не как «звонков нет».

    Ноль на экране у неподсчитанного клиента — это приглашение закрыть
    карточку, не открывая ленту. Между «мы не смотрели» и «смотреть нечего»
    разница в решении брокера, а не в оформлении.
    """
    page = _screen(admin.get(f"{BASE}/clients/c:88"))

    assert "не считано" in page
    assert "расшифровано 0" not in page


def test_a_run_that_could_not_read_the_cache_does_not_report_zero(admin):
    """Прогон был, база аудита не открылась — «расшифровки не считаны».

    Третий случай колонки, и единственный, в котором число звонков уже
    известно, а число расшифровок ещё нет. Свести его к нулю значило бы
    объявить, что у клиента с четырьмя разговорами читать нечего, — и
    разбор, и брокер сделали бы из этого один и тот же неверный вывод.
    """
    with clients_session(os.environ["CLIENTS_DB_PATH"]) as conn:
        conn.execute("UPDATE clients SET calls_with_transcript = NULL"
                     " WHERE client_key = ?", ("p:+79001112233",))

    page = _screen(admin.get(f"{BASE}/clients/p:+79001112233"))

    assert "4 звонка, расшифровки не считаны" in page
    assert "расшифровано" not in page


def test_the_review_reaches_the_card_with_its_verdict_and_issues(admin):
    """Разбор без вердикта и списка проблем — это просто абзац текста.

    Вердикт стоит рядом с датой, потому что по нему решают, читать ли
    дальше; проблемы — потому что их будет считать справочник раздела 8.
    """
    with clients_session(os.environ["CLIENTS_DB_PATH"]) as conn:
        conn.execute(
            "INSERT INTO client_reviews(client_key, created_at, reviewed_through,"
            " summary, verdict, issues_json, recommendation, enough_data, author)"
            " VALUES (?, '2026-09-25T09:00:00+00:00', '2026-09-24T00:00:00+00:00',"
            " ?, ?, ?, ?, 1, 'модель')",
            ("p:+79001112233", "Клиент просил перезвонить после майских.",
             "нужен звонок", '["обещали и не перезвонили", "нет следующего шага"]',
             "Позвонить и предложить два варианта"),
        )

    page = _screen(admin.get(f"{BASE}/clients/p:+79001112233"))

    assert "клиент просил перезвонить после майских" in page
    assert "нужен звонок" in page
    assert "обещали и не перезвонили · нет следующего шага" in page
    assert "позвонить и предложить два варианта" in page
    assert "данных мало" not in page


def test_a_review_made_out_of_dates_says_so_on_the_card(admin):
    """Вывод по датам и вывод по словам клиента — разные вещи.

    Различать их брокер должен раньше, чем прочтёт сам вывод, иначе
    «клиент остыл» из пустой карточки читается так же уверенно, как то же
    самое из расшифровки разговора.
    """
    with clients_session(os.environ["CLIENTS_DB_PATH"]) as conn:
        conn.execute(
            "INSERT INTO client_reviews(client_key, created_at, reviewed_through,"
            " summary, verdict, issues_json, enough_data, author)"
            " VALUES (?, '2026-09-25T09:00:00+00:00', '2026-09-24T00:00:00+00:00',"
            " ?, ?, '[]', 0, 'модель')",
            ("p:+79001112233", "Данных мало: ни одного записанного разговора.",
             "скорее потерян"),
        )

    page = _screen(admin.get(f"{BASE}/clients/p:+79001112233"))

    assert "данных мало" in page


def test_a_broken_issues_list_does_not_break_the_card(admin):
    """В колонке лежит JSON, и однажды там окажется не список.

    Уронить карточку из-за кривой строки, которую написала модель, —
    значит отдать ей право гасить экран.
    """
    with clients_session(os.environ["CLIENTS_DB_PATH"]) as conn:
        conn.execute(
            "INSERT INTO client_reviews(client_key, created_at, reviewed_through,"
            " summary, issues_json, enough_data, author)"
            " VALUES (?, '2026-09-25T09:00:00+00:00', '', ?, ?, 1, 'модель')",
            ("p:+79001112233", "Вывод есть.", "не json вовсе"),
        )

    response = admin.get(f"{BASE}/clients/p:+79001112233")

    assert response.status_code == 200
    assert "вывод есть." in _screen(response)


# ── вкладка «Исключения» (раздел 8) ───────────────────────────────────

def _issue(conn, key, code):
    conn.execute("INSERT INTO client_issues(client_key, code) VALUES (?, ?)",
                 (key, code))


def test_the_exceptions_tab_counts_people_not_rows(admin):
    """У одного человека бывает несколько претензий сразу.

    Сумма счётчиков поэтому больше числа клиентов — и это правда, которую
    экран говорит вслух. Считать строками значило бы обещать РОПу, что
    работы вдвое больше, чем людей.
    """
    with clients_session(os.environ["CLIENTS_DB_PATH"]) as conn:
        _issue(conn, "p:+79001112233", "abandoned")
        _issue(conn, "p:+79001112233", "no_assignee_comment")
        _issue(conn, "c:88", "abandoned")

    page = _screen(admin.get(f"{BASE}/exceptions"))

    assert "брошен дольше порога" in page
    assert "ответственный ни разу не написал" in page
    assert "всего претензий 3" in page


def test_the_counter_names_all_five_codes_even_at_zero(app):
    """Проверяется у функции, а не по экрану: шаблон терпелив.

    Он перебирает ISSUE_ORDER и берёт число через `get(code, 0)`, так что
    пропавший ключ на экране незаметен. А наружу, в выгрузку и в ручку,
    уйдёт именно то, что вернула функция, — и там пропажа означала бы
    «такой проблемы у нас не бывает».
    """
    with scoped_clients(Scope.everything()) as conn:
        counts = clients_read.counts_by_issue(conn)

    assert list(counts) == list(ISSUE_ORDER)
    assert set(counts.values()) == {0}


def test_a_counter_at_zero_stays_on_the_screen(admin):
    """Пропавшая строка читается как «такой проблемы не бывает».

    А правда в том, что сегодня её нет ни у кого, — и это хорошая
    новость, которую видно только если строка на месте.
    """
    page = _screen(admin.get(f"{BASE}/exceptions"))

    assert "обещание просрочено" in page
    assert "никого" in page


def test_a_head_of_department_counts_only_his_own(rop_a):
    """Счётчик суживается тем же представлением, что и список."""
    with clients_session(os.environ["CLIENTS_DB_PATH"]) as conn:
        _issue(conn, "p:+79001112233", "abandoned")   # свой отдел
        _issue(conn, "c:88", "abandoned")             # чужой

    page = _screen(rop_a.get(f"{BASE}/exceptions"))

    assert "всего претензий 1" in page


def test_the_counter_leads_to_the_filtered_list(admin):
    """Счётчик без ссылки — это число, по которому нечего сделать."""
    with clients_session(os.environ["CLIENTS_DB_PATH"]) as conn:
        _issue(conn, "p:+79001112233", "abandoned")

    assert "/clients?issue=abandoned" in admin.get(f"{BASE}/exceptions").text


def test_the_issue_filter_narrows_the_client_list(admin):
    with clients_session(os.environ["CLIENTS_DB_PATH"]) as conn:
        _issue(conn, "p:+79001112233", "promise_overdue")

    page = _screen(admin.get(f"{BASE}/clients?issue=promise_overdue"))
    other = _screen(admin.get(f"{BASE}/clients?issue=abandoned"))

    assert "свой" in page
    assert "под фильтр никто не подошёл" in other


def test_a_client_with_two_issues_appears_in_the_list_once(admin):
    """EXISTS, а не соединение: иначе страница на сто строк — это шестьдесят
    человек, и «всего» врёт."""
    with clients_session(os.environ["CLIENTS_DB_PATH"]) as conn:
        _issue(conn, "p:+79001112233", "abandoned")
        _issue(conn, "p:+79001112233", "no_assignee_comment")

    page = _screen(admin.get(f"{BASE}/clients?issue=abandoned"))

    assert page.count("(без имени)") == 0
    assert "всего 1 по фильтру" in page


def test_the_card_carries_the_codes(admin):
    """Ручка отдаёт их списком: фильтр и карточка читают одно и то же."""
    with clients_session(os.environ["CLIENTS_DB_PATH"]) as conn:
        _issue(conn, "p:+79001112233", "abandoned")

    got = admin.get(f"{BASE}/api/clients/p:+79001112233").json()

    assert got["issues"] == ["abandoned"]


def test_the_exceptions_tab_explains_itself_without_a_book(app_without_book):
    """Экран «книги нет» не должен падать на чужом шаблоне."""
    session = _login(app_without_book, "boss")
    response = session.get(f"{BASE}/exceptions")

    assert response.status_code == 200
    assert "книга клиентов ещё не собрана" in _screen(response)
