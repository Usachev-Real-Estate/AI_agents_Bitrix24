"""Машинный доступ к книге клиентов (раздел 7.1 ТЗ).

`RequireAuthMiddleware` — запрет по умолчанию, и до сих пор `authenticate`
читала только куку сессии. Модели куки не выдать, и без этой ветки разбор
извне записать было нечем.

Токен по ТЗ — доступ ко ВСЕМУ портфелю, без сужения по отделам: наружу
читает модель, а не человек. Отсюда всё остальное в этом файле: пустая
настройка не должна впускать пустой заголовок, токен не должен открывать
HTML-страницы, кука должна оставаться сильнее токена, а право записи —
отдельным ключом.
"""

import json
import re

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import store
from app import create_app
from security import SecurityConfig, bearer_user
from clients.schema import clients_session, init_clients_db
from config import get_settings

BASE = "/dashboard"
PASSWORD = "correct-horse-battery"
READ, WRITE = "r" * 40, "w" * 40
DEPT_A, DEPT_B = 44, 50


@pytest.fixture
def app(tmp_path, analytics_db, monkeypatch):
    """Дашборд с двумя клиентами в разных отделах и прописанными токенами."""
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("DOSSIER_READ_TOKEN", READ)
    monkeypatch.setenv("DOSSIER_WRITE_TOKEN", WRITE)
    book = tmp_path / "clients.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(book))
    get_settings.cache_clear()

    init_clients_db(book)
    with clients_session(book) as conn:
        conn.executemany(
            "INSERT INTO clients(client_key, department_id, name, triage_state,"
            " last_event_at, updated_at) VALUES (?, ?, ?, 'cooling',"
            " '2026-09-01T00:00:00+00:00', '')",
            [("p:+79001112233", DEPT_A, "Свой"), ("c:88", DEPT_B, "Чужой")],
        )
        conn.execute(
            "INSERT INTO client_issues(client_key, code)"
            " VALUES ('p:+79001112233', 'abandoned')"
        )
    application = create_app()
    store.create_user("boss", PASSWORD, "Директор", role="admin")
    store.create_user("rop_a", PASSWORD, "РОП А", role="rop", department_ids=[DEPT_A])
    yield application
    get_settings.cache_clear()


@pytest.fixture
def anon(app):
    return TestClient(app, follow_redirects=False)


def _bearer(app, token):
    client = TestClient(app, follow_redirects=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _login(app, username):
    session = TestClient(app, follow_redirects=False)
    session.get(f"{BASE}/login")
    response = session.post(f"{BASE}/login", data={
        "username": username, "password": PASSWORD,
        "csrf_token": session.cookies.get("dash_csrf"), "next": "",
    })
    assert response.status_code == 303, response.text[:200]
    return session


def _keys(response):
    return [json.loads(line)["client_key"]
            for line in response.text.splitlines() if line.strip()]


def _review(**over):
    body = {"summary": "Клиент ждёт звонка после праздников.",
            "issues": ["abandoned"], "recommendation": "Позвонить",
            "reviewed_through": "2026-09-01T00:00:00+00:00"}
    body.update(over)
    return body


# ── кого впускают ─────────────────────────────────────────────────────

def test_the_read_token_sees_the_whole_portfolio(app):
    """Без сужения по отделам — так и задумано разделом 7.1."""
    response = _bearer(app, READ).get(f"{BASE}/api/clients")

    assert response.status_code == 200
    assert set(_keys(response)) == {"p:+79001112233", "c:88"}


def test_a_wrong_token_is_refused_with_a_code_and_not_a_form(anon, app):
    """Машине нужен 401, а не разметка формы входа."""
    response = _bearer(app, "wrong-token").get(f"{BASE}/api/clients")

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_an_unconfigured_token_does_not_open_the_book(
        tmp_path, analytics_db, monkeypatch):
    """Пустая настройка и пустой заголовок не должны совпасть.

    Иначе книга оказалась бы открытой у каждого, кто не заполнил `.env`, —
    и заметить это было бы нечем: запрос проходит, данные отдаются.
    """
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.delenv("DOSSIER_READ_TOKEN", raising=False)
    monkeypatch.delenv("DOSSIER_WRITE_TOKEN", raising=False)
    book = tmp_path / "clients.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(book))
    get_settings.cache_clear()
    init_clients_db(book)
    application = create_app()

    try:
        for token in ("", " ", "anything"):
            response = _bearer(application, token).get(f"{BASE}/api/clients")
            assert response.status_code == 401, token
    finally:
        get_settings.cache_clear()


def test_a_token_does_not_open_the_html_pages(app):
    """Токен, впускающий в браузер, однажды оставят в незакрытой вкладке.

    Заодно отказ вне `/api` — это 303 на форму входа, то есть машина
    получила бы разметку вместо кода.
    """
    response = _bearer(app, READ).get(f"{BASE}/clients")

    assert response.status_code == 303


class _FakeRequest:
    """Запрос с заголовком, какой клиент из тестов послать не умеет."""

    def __init__(self, path: str, authorization: str) -> None:
        self.url = type("Url", (), {"path": path})()
        self.headers = {"authorization": authorization}


@pytest.mark.parametrize("value", [
    "Bearer t\u00f6k\u00e9n",          # latin-1: такое отдаст другой сервер
    "Bearer \u0442\u043e\u043a\u0435\u043d",  # кириллица
    "Bearer",                            # без значения
    "Basic " + READ,                     # правильный токен, чужая схема
    READ,                                # без схемы вовсе
])
def test_a_header_that_is_not_our_token_is_refused_without_crashing(value):
    """`hmac.compare_digest` на строках с не-ASCII падает TypeError.

    Проверяется НАПРЯМУЮ, а не через клиента: httpx кодирует заголовки в
    ASCII и не даст послать такое вовсе. Но заголовок приходит не только
    от него — HTTP разрешает там latin-1, и сервер перед нами может
    отдать байты выше 0x7F. Сравнение строк на них упало бы пятисоткой,
    как однажды в проверке CSRF, где подставленный мусор ронял обработчик
    любому желающему.

    Заодно тут заперты три соседние ошибки: пустое значение, чужая схема и
    токен без схемы. Все три обязаны быть отказом, а не пропуском.
    """
    config = SecurityConfig(base_path=BASE, secret_key="k" * 48,
                            read_token=READ, write_token=WRITE)

    assert bearer_user(_FakeRequest(f"{BASE}/api/clients", value), config) is None


def test_the_right_token_is_recognised_by_the_same_function():
    """Обратная сторона предыдущего: проверка не отказывает всем подряд."""
    config = SecurityConfig(base_path=BASE, secret_key="k" * 48,
                            read_token=READ, write_token=WRITE)
    path = f"{BASE}/api/clients"

    reader = bearer_user(_FakeRequest(path, f"bearer {READ}"), config)
    writer = bearer_user(_FakeRequest(path, f"Bearer {WRITE}"), config)

    assert reader["can_write"] is False, "схема регистронезависима"
    assert writer["can_write"] is True


def test_the_cookie_stays_stronger_than_the_token(app):
    """Человек с сессией остаётся собой, даже если в запросе есть токен.

    Иначе РОП, у которого в браузере почему-то оказался заголовок с
    токеном, увидел бы всю компанию — и решил бы, что так и должно быть.
    """
    session = _login(app, "rop_a")
    session.headers.update({"Authorization": f"Bearer {READ}"})

    response = session.get(f"{BASE}/api/clients")

    assert _keys(response) == ["p:+79001112233"], "область видимости РОПа уцелела"


def test_the_token_does_not_land_in_the_visit_log(app):
    """След посещений — про людей, и учётки `token:read` в базе нет."""
    _bearer(app, READ).get(f"{BASE}/api/clients")

    assert store.recent_visits(limit=50) == []


# ── право записи ──────────────────────────────────────────────────────

def test_the_read_token_may_not_write(app):
    """403, а не 401: кто ты установлено, не хватает права.

    Ответив 401, мы предложили бы читающей модели предъявить тот же токен
    ещё раз, и она бы честно попробовала.
    """
    response = _bearer(app, READ).post(
        f"{BASE}/api/clients/p:+79001112233/review", json=_review())

    assert response.status_code == 403


def test_an_anonymous_write_is_refused(anon):
    response = anon.post(f"{BASE}/api/clients/p:+79001112233/review", json=_review())

    assert response.status_code == 401


def test_the_write_token_appends_a_review(app):
    """Дописывает, а не перезаписывает: таблицу разборов не пересобрать."""
    with clients_session() as conn:
        conn.execute(
            "INSERT INTO client_reviews(client_key, created_at, reviewed_through,"
            " summary, author) VALUES ('p:+79001112233', '2026-09-02T00:00:00+00:00',"
            " '2026-08-01T00:00:00+00:00', 'прошлый вывод', 'человек')"
        )

    response = _bearer(app, WRITE).post(
        f"{BASE}/api/clients/p:+79001112233/review", json=_review())

    assert response.status_code == 201
    with clients_session(readonly=True) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT summary, issues_json, author FROM client_reviews ORDER BY id")]
    assert len(rows) == 2 and rows[0]["summary"] == "прошлый вывод"
    assert json.loads(rows[1]["issues_json"]) == ["abandoned"]


def test_a_review_for_an_unknown_client_is_not_written(app):
    response = _bearer(app, WRITE).post(
        f"{BASE}/api/clients/p:+70000000000/review", json=_review())

    assert response.status_code == 404


# ── проверка тела ─────────────────────────────────────────────────────

@pytest.mark.parametrize("body, expected", [
    ({"summary": "  "}, "summary"),
    ({"issues": "строка"}, "issues"),
    ({"issues": ["выдуманный_код"]}, "неизвестные коды"),
    ({"reviewed_through": ""}, "обязателен"),
    ({"reviewed_through": "2026-09-01T00:00:00"}, "без смещения"),
    ({"reviewed_through": "не дата"}, "не ISO-8601"),
    ({"reviewed_through": "2099-01-01T00:00:00+00:00"}, "в будущем"),
])
def test_a_malformed_review_is_refused_with_a_reason(app, body, expected):
    """Отказ обязан называть причину: отправитель — программа.

    Молча приняв или молча отбросив, ручка сказала бы «записал» там, где
    записи нет, — худший из отказов, потому что он не выглядит отказом.
    """
    response = _bearer(app, WRITE).post(
        f"{BASE}/api/clients/p:+79001112233/review", json=_review(**body))

    assert response.status_code == 400
    assert expected in response.json()["detail"]


def test_a_body_that_is_not_json_is_refused(app):
    response = _bearer(app, WRITE).post(
        f"{BASE}/api/clients/p:+79001112233/review",
        content=b"\\xff\\xfe not json", headers={"Content-Type": "application/json"})

    assert response.status_code == 400


def test_a_list_instead_of_an_object_is_refused(app):
    response = _bearer(app, WRITE).post(
        f"{BASE}/api/clients/p:+79001112233/review", json=[1, 2, 3])

    assert response.status_code == 400


# ── счётчики исключений ───────────────────────────────────────────────

def test_the_exceptions_endpoint_names_all_five_codes(app):
    got = _bearer(app, READ).get(f"{BASE}/api/exceptions").json()

    assert got["issues"]["abandoned"] == 1
    assert len(got["issues"]) == 5
    assert got["total"] == 1


def test_the_token_never_shows_up_in_the_answer(app):
    """Ни в ошибке, ни в успехе: ответы уходят в логи вызывающего."""
    refused = _bearer(app, "wrong-token").get(f"{BASE}/api/clients").text
    allowed = _bearer(app, READ).get(f"{BASE}/api/exceptions").text

    assert not re.search(r"[rw]{20}", refused + allowed)


# ── настройка, которую нельзя принять молча ───────────────────────────

def test_the_same_value_for_both_tokens_is_refused_at_startup():
    """Один ключ на чтение и запись порознь не отозвать.

    Сняв право записи, администратор снял бы и чтение у всех. Выбрать за
    него, каким из двух прав считать такой токен, нельзя: щедрый выбор
    отдаёт запись тем, кому её не давали, осторожный — молча ломает
    запись тому, кто её настраивал. Падать внятно лучше обоих, и это то
    же решение, что с коротким DASHBOARD_SECRET_KEY.
    """
    settings = type("S", (), {
        "dashboard_base_path": BASE, "dashboard_secret_key": "k" * 48,
        "dashboard_cookie_secure": False, "dashboard_session_ttl_hours": 12,
        "dashboard_session_idle_hours": 2, "dashboard_login_max_attempts": 5,
        "dashboard_login_lockout_minutes": 15,
        "dossier_read_token": "same-value", "dossier_write_token": "same-value",
    })()

    with pytest.raises(RuntimeError, match="совпадают"):
        SecurityConfig.from_settings(settings)


def test_two_empty_tokens_are_not_a_conflict():
    """Обе настройки пустые — это «выключено», а не «совпадают».

    Уронив дашборд на этом, мы потребовали бы токенов от каждого, кому
    машинный доступ не нужен вовсе.
    """
    settings = type("S", (), {
        "dashboard_base_path": BASE, "dashboard_secret_key": "k" * 48,
        "dashboard_cookie_secure": False, "dashboard_session_ttl_hours": 12,
        "dashboard_session_idle_hours": 2, "dashboard_login_max_attempts": 5,
        "dashboard_login_lockout_minutes": 15,
        "dossier_read_token": "", "dossier_write_token": "   ",
    })()

    config = SecurityConfig.from_settings(settings)

    assert (config.read_token, config.write_token) == ("", "")
