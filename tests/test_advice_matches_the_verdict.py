"""Совет не должен спорить с вердиктом на той же карточке.

Прогон 28.08, #16032: карточка стоит в разделе «НЕДОРАБОТКА БРОКЕРА»,
строкой выше — «работа не подтверждена: за норму этапа ни звонка, ни
комментария», а совет: «Дело стоит на 2026-08-31 — ждём». Два вердикта на
одной карточке, противоположных по смыслу: РОП читает «не работают» и тут
же «ничего не делать».

Незакрытое дело — это план брокера, а не работа с клиентом: ровно поэтому
оно и не сняло претензию. Отвечать претензии ожиданием нельзя.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_NO_TRACE_IN_WINDOW,
    GAP_OUT_OF_WINDOW,
    PROVEN_BY_CALL,
    next_action,
)

NOW = datetime(2026, 8, 28, 10, 21, tzinfo=timezone.utc)


def _task(days_ahead: float) -> dict:
    return {
        "kind": "activity",
        "created": (NOW - timedelta(days=3)).isoformat(),
        "deadline": (NOW + timedelta(days=days_ahead)).isoformat(),
        "completed": "N",
        "text": "Связаться с клиентом",
    }


def _state(reason: str, proven: bool, window: float | None = 1) -> dict:
    work = {"proven": proven, "reason": reason}
    if window is not None:
        work["window_days"] = window
    return {"work_evidence": work, "next_step": {}}


def test_a_standing_task_does_not_answer_an_unproven_card():
    advice = next_action(
        _state(GAP_NO_TRACE_IN_WINDOW, proven=False), [_task(3)], NOW,
    )
    assert "ждём" not in advice
    assert "Дело стоит на 2026-08-31" in advice
    assert "следов работы за норму этапа (1 дн.) нет" in advice
    assert "записать в карточке" in advice


def test_the_same_holds_when_the_task_is_due_today():
    """Дело на сегодня отвечает напоминанием, а не ожиданием.

    Решение агентства от 28.08: «если дело запланировано на тот же день
    когда идёт аудит, то тоже надо просто напомнить что необходимо его
    выполнить». Совет «ждать результата» на карточке, по которой работы не
    видно, — это совет не делать ничего.
    """
    advice = next_action(
        _state(GAP_NO_TRACE_IN_WINDOW, proven=False), [_task(0.4)], NOW,
    )
    assert "ждать результата" not in advice
    assert "на сегодня (2026-08-28)" in advice
    assert "выполнить" in advice


def test_without_the_window_the_reproach_still_reads():
    advice = next_action(
        _state(GAP_NO_TRACE_IN_WINDOW, proven=False, window=None), [_task(3)], NOW,
    )
    assert "следов работы за норму этапа нет" in advice


def test_proven_work_still_just_waits():
    advice = next_action(_state(PROVEN_BY_CALL, proven=True), [_task(3)], NOW)
    assert advice == "Дело стоит на 2026-08-31 — ждём"


def test_too_early_still_just_waits():
    """Отсрочка этапа — «ещё не спрашиваем», упрекать не за что."""
    advice = next_action(_state(GAP_OUT_OF_WINDOW, proven=True), [_task(3)], NOW)
    assert advice == "Дело стоит на 2026-08-31 — ждём"


def test_a_card_without_an_assessment_keeps_the_old_wording():
    advice = next_action({"next_step": {}}, [_task(3)], NOW)
    assert advice == "Дело стоит на 2026-08-31 — ждём"


def test_a_task_due_today_is_a_reminder_even_when_work_was_proven():
    """Звонок вчера не отменяет дела, поставленного на сегодня.

    Раньше такая карточка получала «ждать результата»: дело со сроком в
    19:57, прочитанное в 10:21, вообще не рассматривалось. По решению
    агентства день срока — повод напомнить, а не ждать. Напоминание при
    этом остаётся напоминанием: в отчёте карточка уходит в 🔔, а не в
    претензии.
    """
    advice = next_action(_state(PROVEN_BY_CALL, proven=True), [_task(0.4)], NOW)
    assert advice == (
        "Дело стоит на сегодня (2026-08-28) — "
        "выполнить и написать в карточке результат; "
        "если уже сделано, закрыть дело"
    )


def test_the_wait_wording_survives_for_a_task_on_another_day():
    """Дело на послезавтра — ждём: срок не сегодня, напоминать не о чем."""
    advice = next_action(_state(PROVEN_BY_CALL, proven=True), [_task(3)], NOW)
    assert advice == "Дело стоит на 2026-08-31 — ждём"
