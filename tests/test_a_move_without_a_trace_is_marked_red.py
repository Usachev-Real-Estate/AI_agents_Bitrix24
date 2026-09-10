"""Перевод стадии — один клик; работа — то, что после него.

Стадию двигают, когда просят «подтянуть воронку»: клик стоит секунду, а
на дашборде выглядит движением. Отличить его от работы можно только по
следу — записи о разговоре или поставленному делу. Переход, за которым нет
ни того ни другого, и есть строка, ради которой блок заводился.

След ищется НЕ «после перехода вообще», а внутри стояния на новой стадии:
от входа до выхода. Иначе комментарий, написанный через две стадии и
месяц, оправдывал бы давно забытый переход — и красных строк на экране не
осталось бы вовсе, а блок выглядел бы работающим.

Кто именно перевёл карточку, портал не хранит: crm.stagehistory.list
автора не отдаёт. Поэтому колонка называет ТЕКУЩЕГО ответственного, и
страница говорит об этом вслух. Автор записи при этом настоящий.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import metrics  # noqa: E402
from schema import analytics_session  # noqa: E402
from scope import Scope, scoped_session  # noqa: E402

CAT = 18
DEPT, OTHER_DEPT = 44, 50
BROKER = 7
SINCE, UNTIL = "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"

ENTERED = "2026-08-10T09:00:00+00:00"
LEFT = "2026-08-20T09:00:00+00:00"


def _stage(conn, stage_id, name, sort):
    conn.execute(
        "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
        " synced_at) VALUES (?, ?, ?, ?, 'in_progress', 'x')",
        (stage_id, CAT, name, sort))


def _deal(conn, deal_id, *, user=BROKER, amount=500000, contact=None):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
            assigned_by_id, source_id, opportunity, currency_id, date_create,
            date_modify, closedate, is_closed, is_won, is_lost, contact_id,
            is_deleted, synced_at)
        VALUES (?, ?, 18, 'C18:SHOW', ?, 'CALL', ?, 'RUB',
                '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
                NULL, 0, 0, 0, ?, 0, 'x')
        """,
        (deal_id, f"Квартира {deal_id}", user, amount, contact))
    # Переход «Подбор» → «Показ» внутри окна.
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id,"
        " stage_id, entered_at, left_at, duration_sec, seq)"
        " VALUES ('deal', ?, 18, 'C18:NEW', '2026-08-01T00:00:00+00:00', ?, 1, 0)",
        (deal_id, ENTERED))
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id,"
        " stage_id, entered_at, left_at, duration_sec, seq)"
        " VALUES ('deal', ?, 18, 'C18:SHOW', ?, ?, 1, 1)",
        (deal_id, ENTERED, LEFT))


def _note(conn, deal_id, at, body="Созвонились, ждёт подборку", author=BROKER,
          auto=0):
    conn.execute(
        "INSERT INTO fact_comment(comment_id, entity_type, entity_id,"
        " author_id, body, is_auto, created_at, synced_at)"
        " VALUES (?, 'deal', ?, ?, ?, ?, ?, 'x')",
        (deal_id * 1000 + hash(at) % 900, deal_id, author, body, auto, at))


def _task(conn, deal_id, at, *, owner_type=2, owner=None):
    conn.execute(
        "INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,"
        " provider_type_id, direction, subject, responsible_id, created_at,"
        " start_time, end_time, completed, synced_at)"
        " VALUES (?, ?, ?, 'CALL', 2, 'Перезвонить', ?, ?, ?, ?, 0, 'x')",
        (deal_id * 100 + 1, owner_type, owner if owner is not None else deal_id,
         BROKER, at, at, at))


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (18, 'Покупатели', 1, 10, 'x')")
        _stage(conn, "C18:NEW", "Подбор", 10)
        _stage(conn, "C18:SHOW", "Показ", 20)
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name,"
            " is_active, synced_at) VALUES (?, 'Анна Брокер', ?, 'Отдел', 1, 'x')",
            (BROKER, DEPT))
    return analytics_db


def _moves(department_id=None):
    with scoped_session(Scope.everything()) as conn:
        return metrics.stage_moves(conn, CAT, SINCE, UNTIL, department_id)


# ── Красное ────────────────────────────────────────────────────────────
def test_a_move_with_nothing_after_it_is_marked(mart):
    with analytics_session() as conn:
        _deal(conn, 1)

    result = _moves()
    assert result["total"] == 1 and result["silent"] == 1
    row = result["rows"][0]
    assert row["silent"] is True
    assert row["from_name"] == "Подбор" and row["to_name"] == "Показ"
    assert row["assignee"] == "Анна Брокер"


def test_a_note_after_the_move_clears_it(mart):
    with analytics_session() as conn:
        _deal(conn, 1)
        _note(conn, 1, "2026-08-11T10:00:00+00:00")

    row = _moves()["rows"][0]
    assert row["silent"] is False
    assert row["note"] == "Созвонились, ждёт подборку"
    assert row["note_author"] == "Анна Брокер"


def test_a_task_alone_also_clears_it(mart):
    """Поставленное дело — тоже работа, даже если ничего не написали."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _task(conn, 1, "2026-08-12T10:00:00+00:00")

    row = _moves()["rows"][0]
    assert row["silent"] is False
    assert row["note"] == "" and row["task"] == "Перезвонить"


# ── Что следом не считается ────────────────────────────────────────────
def test_a_note_written_before_the_move_does_not_count(mart):
    """Запись до перевода объясняет прошлую стадию, а не эту."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _note(conn, 1, "2026-08-05T10:00:00+00:00")

    assert _moves()["rows"][0]["silent"] is True


def test_a_note_written_after_the_card_left_the_stage_does_not_count(mart):
    """Главная защита блока: иначе красных строк не осталось бы вовсе.

    Комментарий через две стадии и месяц оправдывал бы давно забытый
    переход, и блок выглядел бы работающим, ничего не находя.
    """
    with analytics_session() as conn:
        _deal(conn, 1)
        _note(conn, 1, "2026-08-25T10:00:00+00:00")

    assert _moves()["rows"][0]["silent"] is True


def test_a_robot_comment_is_not_work(mart):
    """«Новое обращение: Звонок с Cian» карточку не отрабатывает."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _note(conn, 1, "2026-08-11T10:00:00+00:00",
              body="Новое обращение: Звонок с Cian", auto=1)

    assert _moves()["rows"][0]["silent"] is True


def test_a_call_on_the_contact_counts(mart):
    """Звонок чаще висит на контакте, а не на сделке, — и это та же работа."""
    with analytics_session() as conn:
        _deal(conn, 2, contact=5000)
        _task(conn, 2, "2026-08-12T10:00:00+00:00", owner_type=3, owner=5000)

    assert _moves()["rows"][0]["silent"] is False


# ── Порядок и границы ──────────────────────────────────────────────────
def test_the_silent_ones_come_first_and_the_dearest_of_them_on_top(mart):
    with analytics_session() as conn:
        _deal(conn, 1, amount=100000)
        _deal(conn, 2, amount=900000)
        _deal(conn, 3, amount=5000000)
        _note(conn, 3, "2026-08-11T10:00:00+00:00")

    result = _moves()
    assert [row["deal_id"] for row in result["rows"]] == [2, 1, 3]
    assert result["silent"] == 2


def test_a_move_outside_the_window_is_not_shown(mart):
    with analytics_session() as conn:
        _deal(conn, 1)
    with scoped_session(Scope.everything()) as conn:
        result = metrics.stage_moves(
            conn, CAT, "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
    assert result["total"] == 0


def test_the_department_filter_narrows_the_list(mart):
    with analytics_session() as conn:
        _deal(conn, 1)
    assert _moves(department_id=DEPT)["total"] == 1
    assert _moves(department_id=OTHER_DEPT)["total"] == 0


def test_the_list_is_capped_but_the_count_is_not(mart):
    """Тысяча строк не читается, но «сколько всего» обязано остаться верным."""
    with analytics_session() as conn:
        for deal_id in range(1, 12):
            _deal(conn, deal_id, amount=deal_id * 1000)

    with scoped_session(Scope.everything()) as conn:
        result = metrics.stage_moves(conn, CAT, SINCE, UNTIL, limit=4)
    assert result["total"] == 11
    assert result["shown"] == 4 and len(result["rows"]) == 4
    assert result["silent"] == 11


# ── Страница ───────────────────────────────────────────────────────────
def test_the_page_actually_paints_the_row_red(mart, monkeypatch):
    """Метрика может считать верно, а подсветка — не доехать до шаблона.

    Проверяется именно то, о чём просили: строка перехода без следа несёт
    класс, который её красит. Без этого теста блок мог бы молча выродиться
    в обычную таблицу — числа сошлись бы, а находка перестала бы бросаться
    в глаза.
    """
    from fastapi.testclient import TestClient

    import store
    from app import create_app
    from config import get_settings

    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    with analytics_session() as conn:
        _deal(conn, 1)

    password = "correct-horse-battery"
    application = create_app()
    store.create_user("boss", password, "Директор", role="admin")
    session = TestClient(application, follow_redirects=False)
    session.get("/dashboard/login")
    session.post("/dashboard/login", data={
        "username": "boss", "password": password,
        "csrf_token": session.cookies.get("dash_csrf"), "next": "",
    })

    response = session.get(
        "/dashboard/movement?start=2026-08-01&end=2026-08-31&category=18")
    assert response.status_code == 200
    body = response.text
    assert "Кто куда двинул" in body
    assert 'class="row-silent"' in body, "строка без следа не подсвечена"
    assert "Подбор → <strong>Показ</strong>" in body
    assert "ни записи, ни дела" in body
