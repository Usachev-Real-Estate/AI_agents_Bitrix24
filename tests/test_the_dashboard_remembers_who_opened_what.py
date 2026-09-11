"""След посещений: пользуется ли РОП дашбордом вообще.

Сессия говорит «заходил», но не говорит, дошёл ли человек дальше первого
экрана. А РОП, ни разу не открывший «План на день», — это не поломка
дашборда, это разговор с человеком, и увидеть такое без следа нельзя.

Три свойства, ради которых след и заводился.

**Пишется в одном месте.** Запись стоит в том же middleware, через который
проходит каждый запрос к данным: новый раздел попадает в след сам. Расставь
запись по обработчикам — и первый же добавленный экран окажется невидимым,
а понять это по журналу невозможно: отсутствие строк выглядит точно так же,
как «человек туда не заходил».

**Пишется раздел, а не адрес.** Фильтры и строка поиска в след не попадают:
вопрос стоит «чем пользуются», а не «что искали».

**Ошибка записи не роняет страницу.** След — вспомогательная вещь, и ради
него нельзя отдать пятисотую человеку, открывшему отчёт.
"""

from __future__ import annotations

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import store
from app import create_app
from config import get_settings
from schema import analytics_session
from security import section_of

BASE = "/dashboard"
PASSWORD = "correct-horse-battery"
DEPT = 44


@pytest.fixture
def app(analytics_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active,"
                     " sort, synced_at) VALUES (18, 'Покупатели', 1, 10, 'x')")
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name,"
            " is_active, synced_at) VALUES (5, 'Анна', ?, 'Отдел', 1, 'x')",
            (DEPT,))
    application = create_app()
    store.create_user("boss", PASSWORD, "Директор", role=store.ROLE_ADMIN)
    store.create_user("rop", PASSWORD, "РОП", role=store.ROLE_ROP,
                      department_ids=[DEPT])
    store.create_user("lazy", PASSWORD, "Ленивый", role=store.ROLE_ROP,
                      department_ids=[DEPT])
    return application


def _login(app, username):
    session = TestClient(app, follow_redirects=False)
    session.get(f"{BASE}/login")
    session.post(f"{BASE}/login", data={
        "username": username, "password": PASSWORD,
        "csrf_token": session.cookies.get("dash_csrf"), "next": "",
    })
    return session


@pytest.fixture
def rop(app):
    return _login(app, "rop")


@pytest.fixture
def admin(app):
    return _login(app, "boss")


# ── Что записывается ───────────────────────────────────────────────────
def test_opening_a_section_leaves_a_trace(rop):
    rop.get(f"{BASE}/pulse")
    rop.get(f"{BASE}/deals?category=18")

    seen = store.visit_summary()["rop"]
    assert seen["visits"] == 2
    assert {item["section"] for item in seen["sections"]} == {"pulse", "deals"}


def test_the_trace_keeps_the_section_not_the_search(rop):
    """Строка поиска в след не попадает: спрашивают «чем пользуются»."""
    rop.get(f"{BASE}/movement?department=44&q=Иванов&start=2026-08-01")

    sections = [row["section"] for row in store.recent_visits()]
    assert sections == ["movement"]
    assert not any("Иванов" in row["section"] for row in store.recent_visits())


def test_a_refusal_is_not_use(rop):
    """403 в закрытом разделе — не пользование разделом."""
    assert rop.get(f"{BASE}/table").status_code == 403

    assert store.visit_summary() == {}


def test_static_files_and_health_leave_nothing(rop):
    rop.get(f"{BASE}/static/css/app.css")
    rop.get("/healthz")

    assert store.recent_visits() == []


def test_the_redirect_at_the_root_is_not_a_second_visit(rop):
    """Корень уводит на «План на день», и записью считается сам раздел."""
    rop.get(f"{BASE}/", follow_redirects=True)

    sections = [row["section"] for row in store.recent_visits()]
    assert sections == ["today"], sections


# ── Кто не заходил ─────────────────────────────────────────────────────
def test_someone_who_never_opened_it_is_simply_absent(rop):
    """Главный ответ этого экрана: пустая строка — это ответ, а не пробел."""
    rop.get(f"{BASE}/pulse")

    summary = store.visit_summary()
    assert "rop" in summary
    assert "lazy" not in summary, "не заходил — значит записи нет"


def test_the_page_says_so_in_words(rop, admin):
    rop.get(f"{BASE}/pulse")
    body = admin.get(f"{BASE}/users").text

    assert "не заходил" in body
    assert "Последние открытия" in body


def test_the_window_narrows_the_answer(rop):
    """«Пользуется сейчас» и «пользовался вообще» — разные вопросы."""
    rop.get(f"{BASE}/pulse")
    with store.store_session() as conn:
        conn.execute("UPDATE dash_visit SET at = '2026-01-01T00:00:00+00:00'")

    assert store.visit_summary(days=7) == {}
    assert "rop" in store.visit_summary(days=10_000)


# ── Разбор адреса ──────────────────────────────────────────────────────
@pytest.mark.parametrize("path,expected", [
    ("/dashboard/pulse", "pulse"),
    ("/dashboard/today", "today"),
    ("/dashboard/api/export.csv", "api/export.csv"),
    ("/dashboard/users/password", "users/password"),
    ("/dashboard/", ""),
    ("/dashboard", ""),
])
def test_the_section_is_read_from_the_path(path, expected):
    assert section_of(path, "/dashboard") == expected


# ── Надёжность ─────────────────────────────────────────────────────────
def test_a_broken_trace_stays_silent(app, monkeypatch):
    """Ради следа нельзя отдать пятисотую человеку, открывшему отчёт.

    Проверяется сама запись, а не страница: подменить соединение с базой
    целиком нельзя — на нём же держится проверка сессии, и тест сломал бы
    вход вместо следа.
    """
    def explode(*args, **kwargs):
        raise RuntimeError("база занята")

    monkeypatch.setattr(store, "store_session", explode)

    store.record_visit("rop", "pulse")  # молча, без исключения


def test_nothing_is_written_without_a_name_or_a_section(app):
    """Пустая строка в следе — мусор, который потом придётся объяснять."""
    store.record_visit("", "pulse")
    store.record_visit("rop", "")

    assert store.recent_visits() == []


def test_old_traces_are_purged():
    store.init_store()
    store.record_visit("rop", "pulse")
    with store.store_session() as conn:
        conn.execute("UPDATE dash_visit SET at = '2020-01-01T00:00:00+00:00'")

    assert store.purge_old_visits() == 1
    assert store.recent_visits() == []
