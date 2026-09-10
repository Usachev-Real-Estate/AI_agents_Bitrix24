"""Раздел «Доступы»: завести человека, сменить пароль, закрыть доступ.

Экран, раздающий доступы, — сам по себе объект нападения, и проверок ему
нужно больше, чем обычной странице. Здесь их четыре рода.

**Кто внутрь.** Раздел администраторский, и одного отсутствия пункта в меню
мало: адрес набирается руками, а действие — это POST, до которого меню
вообще не при чём. Закрыт должен быть каждый обработчик.

**Форма своя.** Без проверки CSRF чужая страница смогла бы отправить в
дашборд форму от имени вошедшего администратора — и завести себе учётку.

**Себя менять нельзя.** Администратор — единственная роль, которая
раздаёт роли. Разжаловав себя, последний администратор закрыл бы раздел
для всех, и открыть его снова было бы нечем, кроме ssh.

**Смена пароля меняет только пароль.** Отдельная проверка ровно потому,
что здесь эта ошибка уже случалась: upsert переписывал строку целиком и
разжаловал администратора молча.
"""

from __future__ import annotations

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import store
from app import create_app
from config import get_settings
from schema import analytics_session

BASE = "/dashboard"
PASSWORD = "correct-horse-battery"
DEPT_A, DEPT_B = 44, 50


@pytest.fixture
def app(analytics_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (18, 'Покупатели', 1, 10, 'x')")
        for user_id, dept in ((5, DEPT_A), (6, DEPT_B)):
            conn.execute(
                "INSERT INTO dim_user(user_id, name, department_id,"
                " department_name, is_active, synced_at)"
                " VALUES (?, ?, ?, ?, 1, 'x')",
                (user_id, f"Брокер {dept}", dept, f"Отдел {dept}"))
            conn.execute(
                """
                INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
                    assigned_by_id, source_id, opportunity, currency_id,
                    date_create, date_modify, closedate, is_closed, is_won,
                    is_lost, is_deleted, synced_at)
                VALUES (?, 'Квартира', 18, 'C18:NEW', ?, 'CALL', 100000, 'RUB',
                        '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
                        NULL, 0, 0, 0, 0, 'x')
                """,
                (user_id * 100, user_id))

    application = create_app()
    store.create_user("boss", PASSWORD, "Директор", role=store.ROLE_ADMIN)
    store.create_user("rop", PASSWORD, "РОП", role=store.ROLE_ROP,
                      department_ids=[DEPT_A])
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
def admin(app):
    session = _login(app, "boss")
    session.get(f"{BASE}/users")          # положит свежий csrf в куку
    return session


@pytest.fixture
def rop(app):
    return _login(app, "rop")


def _post(session, path, **data):
    data.setdefault("csrf_token", session.cookies.get("dash_csrf"))
    return session.post(f"{BASE}{path}", data=data)


def _user(username):
    return {row["username"]: row for row in store.list_users()}.get(username)


# ── Кто внутрь ─────────────────────────────────────────────────────────
def test_a_rop_does_not_see_the_section(rop):
    assert rop.get(f"{BASE}/users").status_code == 403
    assert ">Доступы<" not in rop.get(f"{BASE}/pulse").text


def test_an_administrator_sees_it(admin):
    body = admin.get(f"{BASE}/users").text
    assert "Завести человека" in body
    assert ">Доступы<" in body


@pytest.mark.parametrize("path,payload", [
    ("/users/create", {"username": "intruder", "password": "long-enough-pass",
                       "role": "admin"}),
    ("/users/password", {"username": "boss", "password": "long-enough-pass"}),
    ("/users/role", {"username": "boss", "role": "rop"}),
    ("/users/active", {"username": "boss", "enabled": "0"}),
])
def test_a_rop_cannot_reach_the_actions_either(rop, path, payload):
    """Меню тут ни при чём: POST отправляют, не открывая страницы."""
    _post(rop, path, **payload)

    assert _user("intruder") is None
    assert _user("boss")["role"] == store.ROLE_ADMIN
    assert _user("boss")["is_active"] == 1
    assert store.verify_password("boss", PASSWORD) is not None


def test_a_form_without_a_matching_token_does_nothing(admin):
    """Иначе чужая страница отправила бы форму от имени вошедшего."""
    response = admin.post(f"{BASE}/users/create", data={
        "username": "intruder", "password": "long-enough-pass",
        "role": "admin", "csrf_token": "подделка",
    })

    assert response.status_code == 303
    assert "failed=csrf" in response.headers["location"]
    assert _user("intruder") is None


# ── Завести ────────────────────────────────────────────────────────────
def test_a_new_rop_gets_exactly_the_departments_ticked(admin):
    _post(admin, "/users/create", username="Ivanov", display_name="Иван Иванов",
          password="long-enough-pass", role="rop",
          department=[str(DEPT_A), str(DEPT_B)])

    created = _user("ivanov")
    assert created["role"] == store.ROLE_ROP
    assert created["department_ids"] == sorted([DEPT_A, DEPT_B])
    assert created["display_name"] == "Иван Иванов"
    assert store.verify_password("ivanov", "long-enough-pass") is not None


def test_an_administrator_is_created_without_departments(admin):
    """Отделы администратору не нужны, и забытая галочка не должна оседать."""
    _post(admin, "/users/create", username="second", password="long-enough-pass",
          role="admin", department=[str(DEPT_A)])

    assert _user("second")["department_ids"] == []


def test_a_taken_login_is_refused_instead_of_overwriting(admin):
    """Иначе «завести Иванова» стёрло бы пароль и права уже существующего."""
    response = _post(admin, "/users/create", username="rop",
                     password="long-enough-pass", role="admin")

    assert "failed=login" in response.headers["location"]
    assert _user("rop")["role"] == store.ROLE_ROP
    assert store.verify_password("rop", PASSWORD) is not None


def test_a_short_password_is_refused(admin):
    response = _post(admin, "/users/create", username="ivanov",
                     password="korotko", role="rop")

    assert "failed=password" in response.headers["location"]
    assert _user("ivanov") is None


# ── Пароль ─────────────────────────────────────────────────────────────
def test_changing_a_password_keeps_the_role_and_departments(admin):
    """Та самая ошибка, из-за которой пароль меняется отдельной функцией."""
    _post(admin, "/users/password", username="rop", password="brand-new-password")

    changed = _user("rop")
    assert changed["role"] == store.ROLE_ROP
    assert changed["department_ids"] == [DEPT_A]
    assert changed["display_name"] == "РОП"
    assert store.verify_password("rop", "brand-new-password") is not None


def test_changing_a_password_closes_the_old_sessions(app, admin):
    """Иначе тот, ради кого меняли пароль, продолжит сидеть по своей куке."""
    victim = _login(app, "rop")
    assert victim.get(f"{BASE}/pulse").status_code == 200

    _post(admin, "/users/password", username="rop", password="brand-new-password")

    assert victim.get(f"{BASE}/pulse").status_code == 303


def test_the_response_never_carries_the_password(admin):
    """Ответ сервера пароля не содержит: его придумал браузер и показал один раз."""
    response = _post(admin, "/users/password", username="rop",
                     password="brand-new-password")
    assert "brand-new-password" not in response.text
    assert "brand-new-password" not in response.headers["location"]

    assert "brand-new-password" not in admin.get(f"{BASE}/users").text


# ── Права ──────────────────────────────────────────────────────────────
def test_the_role_change_works_immediately(app, admin):
    """Права читаются на каждый запрос — перезаходить человеку не нужно."""
    promoted = _login(app, "rop")
    assert promoted.get(f"{BASE}/table").status_code == 403

    _post(admin, "/users/role", username="rop", role="admin")

    assert promoted.get(f"{BASE}/table").status_code == 200


def test_an_administrator_cannot_demote_himself(admin):
    """Разжаловав себя, последний администратор закрыл бы раздел для всех."""
    response = _post(admin, "/users/role", username="boss", role="rop")

    assert "failed=self" in response.headers["location"]
    assert _user("boss")["role"] == store.ROLE_ADMIN


def test_an_administrator_cannot_disable_himself(admin):
    response = _post(admin, "/users/active", username="boss", enabled="0")

    assert "failed=self" in response.headers["location"]
    assert _user("boss")["is_active"] == 1


# ── Закрыть доступ ─────────────────────────────────────────────────────
def test_closing_access_takes_effect_at_once(app, admin):
    """Уволили — доступ закрывают в ту же минуту, а не к концу суток."""
    fired = _login(app, "rop")
    assert fired.get(f"{BASE}/pulse").status_code == 200

    _post(admin, "/users/active", username="rop", enabled="0")

    assert fired.get(f"{BASE}/pulse").status_code == 303
    assert _user("rop")["is_active"] == 0


def test_access_can_be_opened_back(admin):
    _post(admin, "/users/active", username="rop", enabled="0")
    _post(admin, "/users/active", username="rop", enabled="1")

    assert _user("rop")["is_active"] == 1


def test_an_unknown_login_is_reported_not_created(admin):
    response = _post(admin, "/users/password", username="nikto",
                     password="long-enough-pass")

    assert "failed=missing" in response.headers["location"]
    assert _user("nikto") is None


# ── Повторная отправка ─────────────────────────────────────────────────
def test_every_action_answers_with_a_redirect(admin):
    """Обновление страницы не должно повторять смену пароля.

    Ответ на POST — 303 на сам раздел: браузер после него делает GET, и
    F5 перезагружает список, а не отправляет форму заново.
    """
    for path, payload in (
        ("/users/create", {"username": "ivanov", "password": "long-enough-pass",
                           "role": "rop"}),
        ("/users/password", {"username": "rop", "password": "long-enough-pass"}),
        ("/users/role", {"username": "rop", "role": "rop"}),
        ("/users/active", {"username": "rop", "enabled": "0"}),
    ):
        response = _post(admin, path, **payload)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith(f"{BASE}/users"), path
