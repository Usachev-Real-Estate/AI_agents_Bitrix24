"""Фильтр «Отдел» обязан отвечать так же, как область видимости.

Отдел человека в проекте считается по ростеру: портал сплошь и рядом
числит его не там, где он работает, — руководитель отдела продаж сидит в
служебном подразделении «Битрикс». Ростер это перекрывает, и на нём
построено ВСЁ: какие сделки попадают РОПу, чей человек для плана, кто
руководит отделом.

Всё, кроме фильтра «Отдел» на страницах. Он спрашивал карточку портала — и
поэтому на одном экране числа расходились сами с собой: «Все отделы»
показывали карточку, а выбранный отдел того же человека — уже нет. Строка
не исчезала с шумом, она просто переставала попадать в выборку, и объяснить
это было нельзя.

Здесь проверяется одно: выбранный отдел и область видимости дают один и тот
же ответ про одного и того же человека.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import metrics  # noqa: E402
import work  # noqa: E402
from schema import analytics_session  # noqa: E402
from scope import Scope, scoped_session  # noqa: E402

CAT = 18
HOME, PORTAL = 50, 1          # отдел по ростеру и служебный по карточке
MOVED = 148                   # тот, кого ростер перенёс
DEAL = 700
SINCE, UNTIL = "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"


def _ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat() + "T09:00:00+00:00"


@pytest.fixture
def mart(analytics_db):
    """Человек, которого ростер перенёс из служебного отдела в продающий."""
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active,"
                     " sort, synced_at) VALUES (18, 'Покупатели', 1, 10, 'x')")
        for stage, name, sort in (("C18:NEW", "Подбор", 10), ("C18:SHOW", "Показ", 20)):
            conn.execute(
                "INSERT INTO dim_stage(stage_id, category_id, name, sort,"
                " semantic, synced_at) VALUES (?, 18, ?, ?, 'in_progress', 'x')",
                (stage, name, sort))
        conn.execute(
            "INSERT INTO dim_user(user_id, name, last_name, department_id,"
            " department_name, is_active, synced_at)"
            " VALUES (?, 'Вера Волкова', 'Волкова', ?, 'Битрикс', 1, 'x')",
            (MOVED, PORTAL))
        conn.execute(
            "INSERT INTO plan_roster(period_code, user_id, department_id,"
            " plan_role, note, updated_at)"
            " VALUES ('*', ?, ?, 'rop', 'числится в служебном', 'x')",
            (MOVED, HOME))
        conn.execute(
            """
            INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
                assigned_by_id, source_id, opportunity, currency_id,
                date_create, date_modify, closedate, is_closed, is_won,
                is_lost, contact_id, is_deleted, synced_at)
            VALUES (?, 'Квартира на Ленина', 18, 'C18:SHOW', ?, 'CALL', 900000,
                    'RUB', ?, ?, NULL, 0, 0, 0, 5000, 0, 'x')
            """,
            (DEAL, MOVED, _ago(40), _ago(40)))
        for seq, stage, entered, left in (
            (0, "C18:NEW", "2026-08-01T00:00:00+00:00", "2026-08-10T09:00:00+00:00"),
            (1, "C18:SHOW", "2026-08-10T09:00:00+00:00", None),
        ):
            conn.execute(
                "INSERT INTO fact_stage_event(entity_type, entity_id,"
                " category_id, stage_id, entered_at, left_at, duration_sec, seq)"
                " VALUES ('deal', ?, 18, ?, ?, ?, 1, ?)",
                (DEAL, stage, entered, left, seq))
        # Обещание, отказ и молчание — по ним строятся списки «Плана на день».
        conn.execute(
            "INSERT INTO fact_comment(comment_id, entity_type, entity_id,"
            " author_id, body, is_auto, created_at, synced_at)"
            " VALUES (?, 'deal', ?, ?, 'Позвонить в пятницу', 0, ?, 'x')",
            (DEAL * 10, DEAL, MOVED, _ago(30)))
        conn.execute(
            """
            INSERT INTO fact_comment_read(entity_type, entity_id, source_hash,
                promised, promised_at, wait_until, refused, refused_why, ready,
                terms, read_at, prompt_version)
            VALUES ('deal', ?, 'h', 'Позвонить в пятницу', ?, NULL, 1,
                    'дорого', '', '', ?, '5')
            """,
            (DEAL, (date.today() - timedelta(days=9)).isoformat(), _ago(1)))
    return analytics_db


# Каждый вызов — «дай мне этот отдел». Ответ обязан быть одинаковым во всех.
CALLS = {
    "зависшие сделки": lambda conn, dept: [
        row["deal_id"] for row in metrics.stuck_deals(conn, CAT, department_id=dept)
    ],
    "таблица": lambda conn, dept: [
        row["id"] for row in
        metrics.entity_table(conn, entity="deal", department_id=dept)["rows"]
    ],
    "движение карточек": lambda conn, dept: [
        row["deal_id"] for row in
        metrics.stage_moves(conn, CAT, SINCE, UNTIL, dept)["rows"]
    ],
    # card_work отдаёт не список карточек, а разрезы по ним: берём тот,
    # где человек виден поимённо.
    "работа по карточкам": lambda conn, dept: [
        DEAL for row in work.card_work(conn, [CAT], department_id=dept)["by_user"]
        if row["key"] == MOVED
    ],
    "просроченные обещания": lambda conn, dept: [
        row["deal_id"] for row in work.promises(conn, [CAT], department_id=dept)
    ],
    "отказы в работе": lambda conn, dept: [
        row["deal_id"] for row in work.refused_in_work(conn, [CAT], department_id=dept)
    ],
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_the_chosen_department_matches_what_the_scope_shows(mart, name):
    """Выбрал свой отдел — карточка осталась. Раньше она исчезала."""
    call = CALLS[name]
    with scoped_session(Scope.departments([HOME])) as conn:
        visible = call(conn, None)
        filtered = call(conn, HOME)

    assert DEAL in visible, f"{name}: карточка не видна и без фильтра"
    assert DEAL in filtered, f"{name}: выбор своего отдела спрятал карточку"


@pytest.mark.parametrize("name", sorted(CALLS))
def test_the_service_department_no_longer_owns_him(mart, name):
    """Служебное подразделение из карточки портала отделом не считается."""
    with scoped_session(Scope.everything()) as conn:
        assert CALLS[name](conn, PORTAL) == [], name


def test_the_stage_transition_counters_agree_too(mart):
    """Тепловая карта считает те же переходы, что показывает список."""
    with scoped_session(Scope.everything()) as conn:
        home = metrics.stage_transitions(conn, CAT, SINCE, UNTIL, HOME)
        portal = metrics.stage_transitions(conn, CAT, SINCE, UNTIL, PORTAL)

    assert sum(row["moves"] for row in home["transitions"]) == 1
    assert portal["transitions"] == []
