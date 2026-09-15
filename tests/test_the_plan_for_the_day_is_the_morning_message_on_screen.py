"""«План на день»: то же, что приходит утром в Битрикс, но на экране.

Страница и сообщение отвечают на один вопрос — «что делать сегодня», — и
обязаны отвечать одинаково. Поэтому у страницы нет ни одного собственного
правила: советы собирает advice_rules, отбирает advice.select, разбор
воронки считает events, списки карточек даёт work. Второй набор правил
однажды разошёлся бы с первым молча: сообщение говорило бы одно, экран
другое, и оба выглядели бы правдоподобно.

Отличие ровно одно, и оно сознательное. Сообщение показывает сегодняшнее:
по одному совету на место, остальные придержаны памятью, чтобы не
повторяться каждое утро. Страницу открывают, когда хотят разобраться,
поэтому под сегодняшним лежит полный список — и видно, почему остальные
молчат.

Страница ничего не запоминает. Если бы её открытие считалось «я об этом
сказал», совет исчезал бы из утреннего сообщения оттого, что кто-то открыл
вкладку.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import store
from app import create_app
from config import get_settings
from schema import analytics_session

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

BASE = "/dashboard"
PASSWORD = "correct-horse-battery"
DEPT, OTHER = 44, 50
BROKER, STRANGER = 11, 22


def _ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat() + "T09:00:00+00:00"


def _card(conn, deal_id, *, user, title, promised="Позвонить в пятницу",
          overdue=9):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
            assigned_by_id, source_id, opportunity, currency_id, date_create,
            date_modify, closedate, is_closed, is_won, is_lost, contact_id,
            is_deleted, synced_at)
        VALUES (?, ?, 18, 'C18:SHOW', ?, 'CALL', 700000, 'RUB', ?, ?, NULL,
                0, 0, 0, NULL, 0, 'x')
        """,
        (deal_id, title, user, _ago(60), _ago(60)))
    conn.execute(
        "INSERT INTO fact_comment(comment_id, entity_type, entity_id,"
        " author_id, body, is_auto, created_at, synced_at)"
        " VALUES (?, 'deal', ?, ?, ?, 0, ?, 'x')",
        (deal_id * 10, deal_id, user, promised, _ago(overdue + 2)))
    conn.execute(
        """
        INSERT INTO fact_comment_read(entity_type, entity_id, source_hash,
            promised, promised_at, wait_until, refused, refused_why, ready,
            terms, read_at, prompt_version)
        VALUES ('deal', ?, 'h', ?, ?, NULL, 0, '', '', '', ?, '5')
        """,
        (deal_id, promised, (date.today() - timedelta(days=overdue)).isoformat(),
         _ago(1)))


@pytest.fixture
def app(analytics_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort,"
            " synced_at) VALUES (18, 'Покупатели', 1, 10, 'x')")
        conn.execute(
            "INSERT INTO dim_stage(stage_id, category_id, name, sort,"
            " semantic, synced_at)"
            " VALUES ('C18:SHOW', 18, 'Показ', 20, 'in_progress', 'x')")
        for user_id, name, dept in ((BROKER, "Марат Абзалилов", DEPT),
                                    (STRANGER, "Чужой Брокер", OTHER)):
            conn.execute(
                "INSERT INTO dim_user(user_id, name, department_id,"
                " department_name, is_active, synced_at)"
                " VALUES (?, ?, ?, ?, 1, 'x')",
                (user_id, name, dept, f"Отдел {dept}"))
        _card(conn, 101, user=BROKER, title="Квартира на Ленина")
        _card(conn, 102, user=BROKER, title="Квартира на Мира")
        _card(conn, 201, user=STRANGER, title="Чужая квартира")

    # Пустая, но существующая память советов: страница читает её строго на
    # чтение, и без файла отбор молча выключился бы — тесты про «сегодня»
    # проходили бы, ничего не проверяя.
    from db import init_db

    init_db()

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
def admin(app):
    return _login(app, "boss")


@pytest.fixture
def rop(app):
    return _login(app, "rop")


# ── Что на странице ────────────────────────────────────────────────────
def _advice_block(session) -> str:
    """Только блок «Что делать сегодня».

    Фамилия брокера есть и в списках карточек ниже, и проверка по всей
    странице проходила бы, даже если бы совет не появился вовсе.
    """
    body = session.get(f"{BASE}/today").text
    start = body.index("Что делать сегодня")
    return body[start:body.index("Разбор воронки ·", start)]


def test_the_page_opens_with_what_to_do_today(admin):
    head = _advice_block(admin)
    assert "Марат Абзалилов" in head, "совет обязан назвать человека"
    assert "обещания" in head, "и то, о чём речь"
    assert "Сегодня советов нет" not in head


def test_the_funnel_breakdown_is_there(admin):
    """Второе, о чём просили: разбор воронки рядом с планом на день."""
    body = admin.get(f"{BASE}/today").text
    assert "Разбор воронки" in body
    assert "Сдвинулись вперёд" in body and "Ушли из работы" in body


def test_what_the_ai_read_is_listed_in_full(admin):
    """Списки карточек полные, а не первые несколько.

    Утреннее сообщение показывает только верх — оно короткое нарочно.
    Страницу открывают, чтобы разобрать всё, и обрезанный там список
    выглядел бы полным.
    """
    body = admin.get(f"{BASE}/today").text
    assert "Что вынул ИИ из переписки" in body
    assert "Квартира на Ленина" in body and "Квартира на Мира" in body
    assert "Позвонить в пятницу" in body


def test_the_full_list_of_findings_is_under_the_advice(admin):
    """«Сегодняшние сверху, полный ниже» — то, что просили."""
    body = admin.get(f"{BASE}/today").text
    assert "Все находки правил" in body
    assert body.index("Что делать сегодня") < body.index("Все находки правил")


# ── Область видимости ──────────────────────────────────────────────────
def test_a_rop_sees_only_his_own_department(rop):
    """Страница собирается на суженном соединении, а не фильтруется потом."""
    body = rop.get(f"{BASE}/today").text
    assert "Квартира на Ленина" in body
    assert "Чужая квартира" not in body
    assert "Чужой Брокер" not in body


def test_the_section_is_open_to_a_rop(rop):
    """Раздел заводился в первую очередь для РОПа — закрывать его нечем."""
    assert rop.get(f"{BASE}/today").status_code == 200
    assert "План на день" in rop.get(f"{BASE}/today").text


# ── Хвалить может только тот, кто говорил ──────────────────────────────
def _remember(rule, subject, label, value, days_ago=1):
    """Строка памяти, какую пишет РАССЫЛКА — по всей компании."""
    from datetime import datetime, timezone

    from db import db_session, init_db

    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    init_db()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO advice_log(rule, subject, first_sent_at, last_sent_at,"
            " sent_count, first_value, last_value, label, closed_at)"
            " VALUES (?, ?, ?, ?, 1, ?, ?, ?, NULL)",
            (rule, subject, stamp, stamp, value, value, label),
        )


def test_the_page_does_not_praise_for_what_it_cannot_see(rop, agent_db):
    """«Сработало» на странице РОПа — чужие имена и чужие деньги.

    Память пишет РАССЫЛКА, и пишет по всей компании. Страница же считает
    кандидатов на СУЖЕННОМ соединении: совета про чужой отдел она увидеть
    не может — и принимала это за «проблема решена, стало ноль».

    Дальше хуже: имя для похвалы берётся из памяти, когда кандидата нет.
    То есть РОП читал «Отдел Кретов: было 35 400 250, стало 0» — чужой
    отдел, чужие деньги, и всё это ещё и неправда.

    Правило простое: хвалить может только тот, кто говорил. Рассылка
    сказала — рассылка и судит, сработало ли. Экран читает память, чтобы
    знать, о чём уже говорили, и ничего больше.
    """
    _remember("dept_behind_pace", "dept:99", "Отдел Кретов", 35_400_250)
    _remember("breakeven_gap", "company", "Квартал", 8_858_953)
    _remember("promise_overdue", "user:777", "Чужой Брокер", 29)

    body = rop.get(f"{BASE}/today").text

    assert "Сработало" not in body
    assert "Отдел Кретов" not in body
    assert "Чужой Брокер" not in body
    assert "35 400 250" not in body and "35400250" not in body


def test_what_was_already_said_is_still_remembered(rop, agent_db):
    """Память не выключается целиком: пауза на повтор обязана работать.

    Совет, сказанный вчера, сегодня молчит — это и есть смысл памяти.
    Выключив её вместе с похвалой, экран начал бы каждый день повторять
    одно и то же.
    """
    body_before = _advice_block(rop)
    assert "Марат Абзалилов" in body_before

    _remember("promise_overdue", f"user:{BROKER}", "Марат Абзалилов", 2)

    assert "Марат Абзалилов" not in _advice_block(rop)


# ── Страница ничего не запоминает ──────────────────────────────────────
def test_opening_the_page_does_not_spend_the_advice(admin, agent_db):
    """Открытая вкладка не должна лишать утреннее сообщение совета.

    Память советов пишет только рассылка. Если бы её писала и страница,
    совет исчезал бы из утреннего сообщения оттого, что кто-то заглянул на
    дашборд, — и понять, почему сегодня молчат, было бы невозможно.
    """
    import sqlite3

    assert "Марат Абзалилов" in _advice_block(admin)

    conn = sqlite3.connect(agent_db)
    try:
        said = conn.execute("SELECT COUNT(*) FROM advice_log").fetchone()[0]
    finally:
        conn.close()
    assert said == 0, "страница записала совет в память рассылки"


def test_a_missing_memory_costs_only_the_advice_block(admin, agent_db):
    """База памяти недоступна — числа всё равно верны.

    Она своя, маленькая и живёт у агента: её может не быть после переноса,
    её может держать соседняя задача. Терять из-за неё всю страницу
    неправильно — теряется отбор «на сегодня», а разбор воронки и списки
    карточек посчитаны и никуда не делись.
    """
    agent_db.unlink()

    body = admin.get(f"{BASE}/today").text
    assert "Сегодня советов нет" in body
    assert "Квартира на Ленина" in body
    assert "Разбор воронки" in body
