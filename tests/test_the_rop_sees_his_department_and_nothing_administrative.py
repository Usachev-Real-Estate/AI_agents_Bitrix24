"""Что РОП видит и чего не видит.

Два разных ограничения, и их легко перепутать.

Первое — область видимости: РОП видит свой отдел и только его. Оно живёт на
соединении с витриной, проверяется отдельно (test_dashboard_tenancy) и
никуда не делось.

Второе — набор разделов. «Таблица» и «Качество данных» — инструменты
сопровождения витрины: пустые поля, дубли, карточки без ответственного,
выгрузка всего подряд. «Люди» — сравнение сотрудников между собой, и
разговор этот ведётся не на уровне отдела. Решение агентства от 10.09: РОПу
их не показывать.

Список разделов один — context.ADMIN_ONLY_PAGES, — и перебирается он здесь
целиком. Новый закрытый раздел, добавленный в множество и забытый в
маршруте, обязан провалить этот тест: иначе меню и права разойдутся молча.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import store
from app import create_app
from config import get_settings
from context import ADMIN_ONLY_PAGES
from schema import analytics_session

BASE = "/dashboard"
PASSWORD = "correct-horse-battery"
DEPT = 44


@pytest.fixture
def app(analytics_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (18, 'Покупатели', 1, 10, 'x')")
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name,"
            " is_active, synced_at) VALUES (5, 'Анна Брокер', ?, 'Отдел', 1, 'x')",
            (DEPT,))
    application = create_app()
    store.create_user("boss", PASSWORD, "Директор", role="admin")
    store.create_user("rop", PASSWORD, "РОП", role="rop", department_ids=[DEPT])
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


# ── Разделы ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("slug", sorted(ADMIN_ONLY_PAGES))
def test_an_administrative_section_answers_a_rop_with_a_refusal(rop, slug):
    """Спрятать пункт меню — не ограничение: адрес набирается руками."""
    assert rop.get(f"{BASE}/{slug}").status_code == 403


@pytest.mark.parametrize("slug", sorted(ADMIN_ONLY_PAGES))
def test_the_same_section_opens_for_an_administrator(admin, slug):
    """Обратная сторона: закрыто должно быть только для РОПа."""
    assert admin.get(f"{BASE}/{slug}").status_code == 200


@pytest.mark.parametrize("slug", sorted(ADMIN_ONLY_PAGES))
def test_a_closed_section_is_not_offered_in_the_menu(rop, admin, slug):
    """Пункт, который всегда отвечает отказом, читается как поломка."""
    assert f'href="{BASE}/{slug}' not in rop.get(f"{BASE}/pulse").text
    assert f'href="{BASE}/{slug}' in admin.get(f"{BASE}/pulse").text


# ── То же самое, но через JSON ─────────────────────────────────────────
@pytest.mark.parametrize("path", ["/api/table", "/api/quality", "/api/export.csv"])
def test_the_data_behind_a_closed_section_is_closed_too(rop, admin, path):
    """Раздел, закрытый на странице и открытый в JSON, не закрыт."""
    assert rop.get(f"{BASE}{path}").status_code == 403
    assert admin.get(f"{BASE}{path}").status_code == 200


# ── Что осталось открытым ──────────────────────────────────────────────
@pytest.mark.parametrize("slug", ["pulse", "leads", "deals", "movement"])
def test_the_working_sections_stay_open(rop, slug):
    """Закрытие не должно расползтись: рабочие разделы РОПу нужны каждый день."""
    assert rop.get(f"{BASE}/{slug}").status_code == 200
