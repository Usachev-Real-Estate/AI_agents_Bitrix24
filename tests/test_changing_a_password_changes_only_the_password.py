"""Смена пароля не должна ничего больше менять.

Выглядит очевидным ровно до того, как посмотришь. `create_user` на
существующем логине — не правка, а полная перезапись строки: роль, отделы
и имя берутся из аргументов, то есть несказанное сбрасывается в умолчание.
А умолчание роли — «РОП без отделов», то есть «не видит ничего».

Команда смены пароля вызывала именно её. Директор, попросивший сменить
пароль, становился РОПом без отделов; РОП терял свой отдел; у обоих
отображаемое имя заменялось логином. Заметить это по экрану нельзя:
учётка на месте, вход работает, просто человек открывает дашборд и не
видит там ничего — и идёт жаловаться не на смену пароля, а на дашборд.

Отсюда две проверки: пароль действительно меняется, и не меняется больше
ничего.
"""

from __future__ import annotations

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import store
from config import get_settings


@pytest.fixture
def users(analytics_db, monkeypatch):
    """Учётки в изолированной базе: рядом с витриной теста, а не с боевой."""
    get_settings.cache_clear()
    store.init_store()
    store.create_user("kretov", "correct-horse-1", "Антон Кретов",
                      role=store.ROLE_ROP, department_ids=[60])
    store.create_user("boss", "correct-horse-2", "Директор",
                      role=store.ROLE_ADMIN)
    return {row["username"]: row for row in store.list_users()}


def _user(username: str) -> dict:
    return {row["username"]: row for row in store.list_users()}[username]


# ── Пароль меняется ────────────────────────────────────────────────────
def test_the_new_password_works_and_the_old_one_does_not(users):
    assert store.set_password("kretov", "brand-new-password") is True

    assert store.verify_password("kretov", "brand-new-password") is not None
    assert store.verify_password("kretov", "correct-horse-1") is None


def test_an_unknown_login_is_reported_not_created(users):
    """Опечатка в логине не должна заводить учётку с этим паролем."""
    assert store.set_password("kretoff", "brand-new-password") is False
    assert "kretoff" not in {row["username"] for row in store.list_users()}


def test_a_short_password_is_refused(users):
    with pytest.raises(ValueError):
        store.set_password("kretov", "korotkiy")
    assert store.verify_password("kretov", "correct-horse-1") is not None


# ── И больше ничего не меняется ────────────────────────────────────────
def test_a_rop_keeps_his_departments(users):
    """Потерянный отдел — это «дашборд пустой», а не «пароль не тот»."""
    store.set_password("kretov", "brand-new-password")

    after = _user("kretov")
    assert after["department_ids"] == [60]
    assert after["role"] == store.ROLE_ROP
    assert after["display_name"] == "Антон Кретов"


def test_an_administrator_stays_an_administrator(users):
    """Худший случай: смена пароля разжаловала бы директора."""
    store.set_password("boss", "brand-new-password")

    after = _user("boss")
    assert after["role"] == store.ROLE_ADMIN
    assert after["display_name"] == "Директор"


def test_the_account_stays_enabled_and_keeps_its_history(users):
    """Дата заведения и признак активности к паролю отношения не имеют."""
    before = _user("kretov")
    store.set_password("kretov", "brand-new-password")
    after = _user("kretov")

    assert after["is_active"] == before["is_active"]
    assert after["created_at"] == before["created_at"]


def test_nobody_else_is_touched(users):
    store.set_password("kretov", "brand-new-password")

    assert store.verify_password("boss", "correct-horse-2") is not None
    assert _user("boss")["role"] == store.ROLE_ADMIN


# ── Команда делает то же самое ─────────────────────────────────────────
def test_the_cli_command_preserves_the_role_and_revokes_sessions(users, monkeypatch):
    """`manage.py passwd` — тот же ответ, что и store: одна смена пароля."""
    import manage

    monkeypatch.setattr(manage, "_read_password", lambda confirm=True: "brand-new-password")
    sid = store.create_session("boss", ttl_hours=12)
    assert store.touch_session(sid, idle_hours=12) is not None

    args = type("Args", (), {"username": "boss"})()
    assert manage.cmd_passwd(args) == 0

    assert _user("boss")["role"] == store.ROLE_ADMIN
    assert store.verify_password("boss", "brand-new-password") is not None
    # Старая сессия обязана умереть: иначе тот, ради кого меняли пароль,
    # продолжит сидеть в дашборде по своей куке.
    assert store.touch_session(sid, idle_hours=12) is None


def test_the_cli_refuses_an_unknown_login(users, monkeypatch):
    import manage

    monkeypatch.setattr(manage, "_read_password", lambda confirm=True: "brand-new-password")
    args = type("Args", (), {"username": "kretoff"})()
    with pytest.raises(SystemExit) as failure:
        manage.cmd_passwd(args)
    assert "Нет такого пользователя" in str(failure.value)
