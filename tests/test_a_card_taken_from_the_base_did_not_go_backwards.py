"""Перенос в другую воронку — не откат назад.

Карточку берут из общей базы в рабочую воронку, и на экране РОПа это
читалось как «🔴 Вернулись назад»:

    Андрей 4 солнца 300 млн, «Первый показ» → «C26:NEW»
    Ольга White Khamovniki, «Отложенный спрос» → «C26:PREPARATION»

Сырые идентификаторы вместо названий — первый признак: название стадии
ищется в воронке КАРТОЧКИ, а стадия осталась от прежней, и не нашлось
ничего. Вторая половина беды глубже: «назад» определяется сравнением
порядковых номеров стадий, а номера у разных воронок свои. «Первый показ»
в одной и «NEW» в другой — числа из двух не связанных между собой рядов,
и разность их не значит ничего.

Поэтому переход между воронками не считается ни откатом, ни движением
вперёд. Это отдельное событие — карточку перевели, — и мерить его шкалой
одной воронки нельзя. Врать в эту сторону дороже, чем промолчать: РОП
получает красную строку про сделку, с которой всё в порядке, и следующую
красную строку читает уже недоверчиво.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import events as funnel  # noqa: E402
import metrics  # noqa: E402
from schema import analytics_session  # noqa: E402
from scope import Scope, scoped_session  # noqa: E402

HOME, OTHER = 18, 26          # рабочая воронка и общая база
SINCE = "2026-09-01T00:00:00+00:00"
UNTIL = "2026-10-01T00:00:00+00:00"
MOVED_AT = "2026-09-10T09:00:00+00:00"

CROSS, BACK, FORWARD = 501, 502, 503


def _stage(conn, stage_id, cat, name, sort):
    conn.execute(
        "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
        " synced_at) VALUES (?, ?, ?, ?, 'in_progress', 'x')",
        (stage_id, cat, name, sort))


def _deal(conn, deal_id, cat, stage_id):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
            assigned_by_id, source_id, opportunity, currency_id, date_create,
            date_modify, closedate, is_closed, is_won, is_lost, is_deleted,
            synced_at)
        VALUES (?, ?, ?, ?, 7, 'CALL', 300000000, 'RUB',
                '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
                NULL, 0, 0, 0, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", cat, stage_id))


def _move(conn, deal_id, first, second):
    """Два события подряд: (категория, стадия) → (категория, стадия)."""
    for seq, (cat, stage, entered, left) in enumerate((
        (*first, "2026-09-02T09:00:00+00:00", MOVED_AT),
        (*second, MOVED_AT, None),
    )):
        conn.execute(
            "INSERT INTO fact_stage_event(entity_type, entity_id, category_id,"
            " stage_id, entered_at, left_at, duration_sec, seq)"
            " VALUES ('deal', ?, ?, ?, ?, ?, 1, ?)",
            (deal_id, cat, stage, entered, left, seq))


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        for cat, name in ((HOME, 'Покупатели'), (OTHER, 'Общая база')):
            conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active,"
                         " sort, synced_at) VALUES (?, ?, 1, 10, 'x')", (cat, name))
        _stage(conn, "C18:NEW", HOME, "Подбор", 10)
        _stage(conn, "C18:SHOW", HOME, "Первый показ", 20)
        _stage(conn, "C18:DEAL", HOME, "Переговоры", 30)
        _stage(conn, "C26:NEW", OTHER, "Новая", 10)
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name,"
            " is_active, synced_at) VALUES (7, 'Анна Костанчук', 44, 'Отдел', 1, 'x')")

        # Взяли из общей базы: воронка сменилась.
        _deal(conn, CROSS, HOME, "C18:SHOW")
        _move(conn, CROSS, (OTHER, "C26:NEW"), (HOME, "C18:SHOW"))
        # Настоящий откат внутри воронки.
        _deal(conn, BACK, HOME, "C18:NEW")
        _move(conn, BACK, (HOME, "C18:SHOW"), (HOME, "C18:NEW"))
        # Настоящее движение вперёд.
        _deal(conn, FORWARD, HOME, "C18:DEAL")
        _move(conn, FORWARD, (HOME, "C18:SHOW"), (HOME, "C18:DEAL"))
    return analytics_db


def _events():
    with scoped_session(Scope.everything()) as conn:
        return funnel.funnel_events(conn, SINCE, UNTIL, categories=[HOME])


# ── Утреннее сообщение и «Пульс» ───────────────────────────────────────
def test_a_card_taken_from_the_base_is_not_a_step_back(mart):
    """Ровно та строка, что была на экране: 300 млн и «→ C26:NEW»."""
    returned = _events()["returned"]

    assert CROSS not in {row["deal_id"] for row in returned["top"]}
    assert returned["deals"] == 1, "остаётся только настоящий откат"


def test_it_is_not_a_step_forward_either(mart):
    """Смена воронки — отдельное событие, а не движение по шкале."""
    advanced = _events()["advanced"]

    assert CROSS not in {row["deal_id"] for row in advanced["top"]}
    assert advanced["deals"] == 1


def test_a_real_step_back_inside_the_funnel_stays(mart):
    """Опора проверки: иначе «починка» просто выключила бы правило."""
    assert [row["deal_id"] for row in _events()["returned"]["top"]] == [BACK]


# ── Тепловая карта переходов ───────────────────────────────────────────
def test_the_transition_matrix_ignores_the_funnel_change(mart):
    """Клетка «из чужой воронки» в матрице одной воронки — бессмыслица."""
    with scoped_session(Scope.everything()) as conn:
        result = metrics.stage_transitions(conn, HOME, SINCE, UNTIL)

    pairs = {(row["from_stage"], row["to_stage"]) for row in result["transitions"]}
    assert ("C26:NEW", "C18:SHOW") not in pairs
    assert result["backwards_total"] == 1


# ── Список «Кто куда двинул» ───────────────────────────────────────────
def test_the_movement_list_does_not_mark_it_backwards(mart):
    with scoped_session(Scope.everything()) as conn:
        rows = metrics.stage_moves(conn, HOME, SINCE, UNTIL)["rows"]

    marked = {row["deal_id"] for row in rows if row["backwards"]}
    assert CROSS not in marked
    assert BACK in marked
