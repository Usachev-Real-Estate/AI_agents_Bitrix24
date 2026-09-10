"""Уволенный уходит из дашборда, его карточки и деньги остаются.

Это два разных вопроса, и дашборд отвечал на них одинаково — потому и
понадобилась правка. «С кем сегодня разговаривать» — вопрос про людей:
строка про уволенного занимает место в рейтинге, но выполнить её нельзя и
спросить не с кого. «Сколько отдел заработал» и «чья это карточка» —
вопросы про деньги и про работу: сделки ушедшего никуда не делись, и
вычесть их значит напечатать неверную сумму и потерять карточки, которые
как раз некому вести.

Разводятся ответы одним местом — двумя представлениями. v_user — состав,
о котором идёт разговор; v_user_all — справочник для привязки. Короткое
имя у суженного нарочно: запрос, написанный не глядя, покажет живых.

Проверяется здесь не список экранов, а само правило: если завтра
представление снова начнут строить по dim_user, эти тесты падают.
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
import pulse as pulse_mod  # noqa: E402
from schema import analytics_session  # noqa: E402
from scope import Scope, scoped_session  # noqa: E402

DEPT = 44
GONE, ALIVE = 61, 62
DEAL_GONE, DEAL_ALIVE = 501, 502
QUARTER = ("2026-07-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (18, 'Покупатели', 1, 10, 'x')")
        for user_id, name, active in (
            (GONE, "Пётр Ушедший", 0),
            (ALIVE, "Анна Работающая", 1),
        ):
            conn.execute(
                "INSERT INTO dim_user(user_id, name, last_name, department_id,"
                " department_name, is_active, synced_at)"
                " VALUES (?, ?, ?, ?, 'Отдел продаж', ?, 'x')",
                (user_id, name, name.split()[1], DEPT, active))
        for deal_id, user_id in ((DEAL_GONE, GONE), (DEAL_ALIVE, ALIVE)):
            conn.execute(
                """
                INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
                    assigned_by_id, source_id, opportunity, currency_id,
                    date_create, date_modify, closedate, is_closed, is_won,
                    is_lost, is_deleted, synced_at)
                VALUES (?, ?, 18, 'C18:WON', ?, 'CALL', 300000, 'RUB',
                        '2026-07-10T00:00:00+00:00', '2026-07-10T00:00:00+00:00',
                        '2026-08-01T00:00:00+00:00', 1, 1, 0, 0, 'x')
                """,
                (deal_id, f"Квартира {deal_id}", user_id))
    return analytics_db


# ── Представления ──────────────────────────────────────────────────────
def test_the_short_name_shows_only_people_who_still_work(mart):
    """v_user — состав. Уволенного в нём нет ни для админа, ни для РОПа."""
    for scope in (Scope.everything(), Scope.departments([DEPT])):
        with scoped_session(scope) as conn:
            seen = {row[0] for row in conn.execute("SELECT user_id FROM v_user")}
        assert GONE not in seen, scope.describe()
        assert ALIVE in seen, scope.describe()


def test_the_full_directory_still_knows_him(mart):
    """v_user_all — справочник: по нему карточка узнаёт своего хозяина."""
    with scoped_session(Scope.everything()) as conn:
        row = conn.execute(
            "SELECT name, department_id FROM v_user_all WHERE user_id = ?",
            (GONE,),
        ).fetchone()
    assert row is not None and row[0] == "Пётр Ушедший"
    assert row[1] == DEPT


# ── Карточки ───────────────────────────────────────────────────────────
def test_his_cards_stay_in_the_table(mart):
    """Карточка уволенного остаётся видимой, и видно, чья она.

    Без справочника здесь был бы либо ряд без имени, либо — если бы
    ответственный отбирался по составу — карточка пропала бы совсем.
    Пропала бы молча: строка, которой нет, ни на что не жалуется.
    """
    with scoped_session(Scope.everything()) as conn:
        page = metrics.entity_table(conn, entity="deal")
    owners = {row["id"]: row.get("assignee") for row in page["rows"]}
    assert DEAL_GONE in owners
    assert owners[DEAL_GONE] == "Пётр Ушедший"


def test_a_department_filter_does_not_swallow_his_cards(mart):
    """Отдел карточки берётся из справочника, а не из состава.

    Иначе РОП перестал бы видеть в своём отделе ровно те карточки, которые
    остались без хозяина, — а это первое, что ему сегодня нужно раздать.
    """
    with scoped_session(Scope.departments([DEPT])) as conn:
        page = metrics.entity_table(conn, entity="deal", department_id=DEPT)
    assert DEAL_GONE in {row["id"] for row in page["rows"]}


# ── Деньги ─────────────────────────────────────────────────────────────
def test_his_money_still_counts_for_the_department(mart):
    """Факт отдела считается по всем закрытым сделкам, включая его."""
    with scoped_session(Scope.everything()) as conn:
        facts = pulse_mod._facts(conn, QUARTER[0], QUARTER[1], [18])
    by_user = {row["user_id"]: row for row in facts}
    assert by_user[GONE]["amount"] == 300000
    assert sum(row["amount"] for row in facts) == 600000


def test_the_brokers_table_adds_up_without_naming_him(mart):
    """Строка «Уволенные» вместо фамилии: итог сходится, спрашивать не с кого."""
    with scoped_session(Scope.everything()) as conn:
        facts = pulse_mod._facts(conn, QUARTER[0], QUARTER[1], [18])
    department = {
        "department_id": DEPT, "name": "Отдел продаж",
        "members": [{"user_id": ALIVE, "name": "Анна Работающая",
                     "role": "broker", "plan": 500000}],
    }
    rows = pulse_mod._brokers(department, facts, elapsed=30, total_days=90)

    assert "Ушедший" not in " ".join(row["name"] for row in rows)
    gone = [row for row in rows if row.get("gone")]
    assert len(gone) == 1
    assert gone[0]["name"] == "Уволенные · 1 человек"
    assert gone[0]["fact"] == 300000
    assert gone[0]["deals"] == 1
    # Ради чего всё и делалось: сумма строк равна факту отдела.
    assert sum(row["fact"] for row in rows) == sum(row["amount"] for row in facts)


# ── Люди ───────────────────────────────────────────────────────────────
def test_the_people_page_has_no_row_about_him(mart):
    """Страница про людей об ушедшем не отчитывается."""
    with scoped_session(Scope.everything()) as conn:
        rows = metrics.people(conn, QUARTER[0], QUARTER[1], 18)
    assert {row["user_id"] for row in rows} == {ALIVE}
