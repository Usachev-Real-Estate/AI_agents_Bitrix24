"""Ежедневная выгрузка — это не весь портфель, а повод посмотреть.

Полный прогон идёт раз в неделю и стоит дорого: тысяча карточек, тысячи
запросов, минуты времени. Ежедневно нужна не копия портфеля, а короткий
список карточек, по которым со вчера что-то изменилось или, наоборот,
подозрительно ничего не изменилось.

Отбор держится на четырёх условиях, и каждое из них закрывает свой случай.
Ошибка в любую сторону дорогая: слишком узкий отбор прячет карточки, ради
которых выгрузка и заводилась, слишком широкий превращает её в тот же
полный прогон, только каждый день.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import dossier  # noqa: E402

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
ACTIVE_STAGE = "C18:UC_UFPFKK"   # Первый показ
CLOSED_STAGE = "C18:UC_RUCRAH"   # Задаток: торг кончился


def _ago(**kwargs) -> str:
    return (NOW - timedelta(**kwargs)).isoformat()


def _row(**over) -> dict:
    row = {
        "id": 1,
        "stage_id": ACTIVE_STAGE,
        "days_since_last_activity": 1.0,
        "comments": [],
        "activities": [],
        "calls": [],
        "stage_history": [],
        "reassignments": [],
    }
    row.update(over)
    return row


# ── Свежее событие ─────────────────────────────────────────────────────
def test_a_comment_from_today_brings_the_card_in():
    row = _row(comments=[{"created": _ago(hours=3)}])
    assert dossier.delta_reasons(row, NOW) == ["свежее событие за сутки"]


def test_a_call_from_today_brings_the_card_in():
    row = _row(calls=[{"date": _ago(hours=5)}])
    assert "свежее событие за сутки" in dossier.delta_reasons(row, NOW)


def test_a_stage_move_from_today_brings_the_card_in():
    """Перенос стадии работой не считается, но посмотреть на него надо.

    Это разные вопросы: «была ли работа» (счётчик молчания) и «стоит ли
    смотреть» (отбор). Шесть карточек, переведённых в одну минуту, — самое
    интересное, что может случиться за сутки.
    """
    row = _row(stage_history=[{"date": _ago(hours=2)}])
    assert "свежее событие за сутки" in dossier.delta_reasons(row, NOW)


def test_yesterdays_event_is_already_too_old():
    row = _row(comments=[{"created": _ago(days=2)}], days_since_last_activity=2.0)
    assert dossier.delta_reasons(row, NOW) == []


# ── Молчание ───────────────────────────────────────────────────────────
def test_a_week_of_silence_on_an_active_stage_brings_the_card_in():
    row = _row(days_since_last_activity=9.0)
    assert dossier.delta_reasons(row, NOW) == ["молчит 9 дней на активной стадии"]


def test_six_days_of_silence_is_not_yet_a_question():
    assert dossier.delta_reasons(_row(days_since_last_activity=6.0), NOW) == []


def test_silence_on_a_settled_stage_is_not_a_question():
    """На «Задатке» торг кончился: ускорять там нечего, и тишина нормальна."""
    row = _row(stage_id=CLOSED_STAGE, days_since_last_activity=40.0)
    assert dossier.delta_reasons(row, NOW) == []


def test_a_card_without_any_work_at_all_is_not_called_silent():
    """None — это «следов работы не нашли», а не «молчит ноль дней».

    Подставить сюда ноль значит тихо объявить такую карточку свежей.
    """
    assert dossier.delta_reasons(_row(days_since_last_activity=None), NOW) == []


# ── Просроченное дело ──────────────────────────────────────────────────
def test_an_open_task_past_its_deadline_brings_the_card_in():
    row = _row(activities=[
        {"created": _ago(days=10), "deadline": _ago(days=3), "completed": False},
    ])
    assert "просроченное дело" in dossier.delta_reasons(row, NOW)


def test_a_finished_task_past_its_deadline_is_not_a_question():
    row = _row(activities=[
        {"created": _ago(days=10), "deadline": _ago(days=3), "completed": True},
    ])
    assert dossier.delta_reasons(row, NOW) == []


def test_a_task_without_a_deadline_is_never_overdue():
    """Дня нет — значит и просрочки нет, и брокер прав, если возразит."""
    row = _row(activities=[
        {"created": _ago(days=10), "deadline": "", "completed": False},
    ])
    assert dossier.delta_reasons(row, NOW) == []


def test_a_task_due_tomorrow_is_not_overdue():
    # Дело поставлено давно: иначе сработало бы правило «свежее событие», и
    # тест проверял бы не то, о чём говорит его название.
    row = _row(activities=[
        {"created": _ago(days=10),
         "deadline": (NOW + timedelta(days=1)).isoformat(),
         "completed": False},
    ])
    assert dossier.delta_reasons(row, NOW) == []


# ── Смена ответственного ───────────────────────────────────────────────
def test_a_fresh_reassignment_brings_the_card_in():
    row = _row(reassignments=[{"detected_at": _ago(hours=4), "from_id": 7, "to_id": 9}])
    assert "сменился ответственный" in dossier.delta_reasons(row, NOW)


def test_an_old_reassignment_does_not_bring_it_in_every_day():
    """Журнал копится вечно. Без окна карточка всплывала бы каждый день."""
    row = _row(reassignments=[{"detected_at": _ago(days=30), "from_id": 7, "to_id": 9}])
    assert dossier.delta_reasons(row, NOW) == []


# ── Ничего ─────────────────────────────────────────────────────────────
def test_a_quiet_healthy_card_stays_out():
    row = _row(
        days_since_last_activity=2.0,
        comments=[{"created": _ago(days=2)}],
        activities=[{"created": _ago(days=2), "deadline": "", "completed": True}],
    )
    assert dossier.delta_reasons(row, NOW) == []


def test_the_reasons_are_listed_not_summed():
    """По причинам видно, какое правило раздувает выгрузку, когда она распухнет."""
    row = _row(
        days_since_last_activity=10.0,
        activities=[
            {"created": _ago(days=10), "deadline": _ago(days=2), "completed": False},
        ],
    )
    reasons = dossier.delta_reasons(row, NOW)
    assert len(reasons) == 2
    assert "просроченное дело" in reasons
