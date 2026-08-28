"""Пауза объяснена и дело стоит — но позже, чем норма этапа после её конца.

#16756 (прогон 28.08 10:41): «Клиенты улетели в отпуск до 01.09», дело на
07.09, норма этапа 3 дня. Карточка получала общее «за норму этапа ни
звонка, ни комментария», хотя брокер и паузу записал, и дело поставил.
Претензия по сути верна — вернуться к разговору он собрался через неделю
после возвращения клиента, — но называлась она не тем.

Установлено по артефакту прогона, а не предположено: model_flags и
verified_flags по #16756 обе дают pause_explained=True, pause_until
2026-09-01, work_reason no_trace_in_window.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_NO_TRACE_IN_WINDOW,
    GAP_PAUSE_NO_TASK,
    GAP_PAUSE_TASK_TOO_LATE,
    PROVEN_BY_PAUSE,
    assess_broker_work,
    next_action,
)
from client_state_report import format_card  # noqa: E402
from funnel_profiles import BUYER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 28, 10, 41, tzinfo=timezone.utc)
STAGE = "C18:UC_DVW1P9"   # Повторный показ, окно 3 дня
WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"


def _comment(days_ago: float) -> dict:
    return {
        "kind": "comment",
        "created": (NOW - timedelta(days=days_ago)).isoformat(),
        "text": "Провели показ трёх квартир, клиенты в отпуске до 01.09",
        "has_files": False,
    }


def _task(day: str) -> dict:
    return {
        "kind": "activity",
        "created": (NOW - timedelta(days=4)).isoformat(),
        "deadline": f"{day}T12:00:00+03:00",
        "completed": "N",
        "text": "Связаться после отпуска",
    }


def _assess(events: list[dict], **over: Any) -> dict:
    params: dict[str, Any] = {
        "profile": BUYER_PROFILE,
        "stage_id": STAGE,
        "hours_on_stage": 1000.0,
        "claims_messaged": False,
        "claims_no_answer": False,
        "comment_informative": True,
        "pause_explained": True,
        "pause_until": "2026-09-01",
        "now": NOW,
    }
    params.update(over)
    return assess_broker_work(events, **params)


def test_a_task_a_week_after_the_pause_is_named_for_what_it_is():
    work = _assess([_comment(3.9), _task("2026-09-07")])
    assert work["reason"] == GAP_PAUSE_TASK_TOO_LATE
    assert work["reason"] != GAP_NO_TRACE_IN_WINDOW
    assert work["proven"] is False
    assert work["pause_until"] == "2026-09-01"
    assert work["task_deadline"] == "2026-09-07"


def test_a_task_inside_the_norm_after_the_pause_still_counts_as_waiting():
    work = _assess([_comment(3.9), _task("2026-09-03")])
    assert work["reason"] == PROVEN_BY_PAUSE
    assert work["proven"] is True


def test_no_task_at_all_stays_the_old_reminder():
    work = _assess([_comment(3.9)])
    assert work["reason"] == GAP_PAUSE_NO_TASK


def test_without_a_pause_nothing_changes():
    work = _assess([_comment(3.9), _task("2026-09-07")], pause_explained=False)
    assert work["reason"] == GAP_NO_TRACE_IN_WINDOW


def test_the_advice_names_both_dates():
    work = _assess([_comment(3.9), _task("2026-09-07")])
    advice = next_action({"work_evidence": work, "next_step": {}}, [], NOW)
    assert "2026-09-07" in advice
    assert "2026-09-01" in advice
    assert "перенести" in advice.lower()


def test_the_card_argues_with_dates_not_with_the_stage_norm():
    """Норма этапа тут не довод: брокер молчал, потому что клиент в отпуске."""
    work = _assess([_comment(3.9), _task("2026-09-07")])
    card = format_card(
        {
            "deal_id": 16756, "skipped": False,
            "state": {
                "temperature": "warm", "verdict": "poor", "work_evidence": work,
            },
        },
        "ЖК «Доминион»",
        WEBHOOK,
    )
    assert "клиент возвращается 2026-09-01, дело на 2026-09-07" in card
    assert "ни звонка, ни комментария" not in card
    assert "последний след" not in card


def test_an_unnamed_pause_end_never_reaches_this_branch():
    """Срок паузы не назван — проверять нечем, и ожидание засчитывается."""
    work = _assess([_comment(3.9), _task("2026-09-07")], pause_until="")
    assert work["reason"] == PROVEN_BY_PAUSE


def test_setting_a_task_is_never_worse_than_not_setting_one():
    """Лестница не должна быть перевёрнутой (решение агентства).

    Паузу записал, дела нет — напоминание. Паузу записал и дело поставил,
    пусть и поздно, — тоже напоминание: он сделал больше, а не меньше.
    Иначе правило учит не ставить дел.
    """
    from broker_work import REMINDERS

    assert GAP_PAUSE_NO_TASK in REMINDERS
    assert GAP_PAUSE_TASK_TOO_LATE in REMINDERS


def test_the_card_lands_in_reminders_not_in_shortfalls():
    from client_state_report import split_sections

    work = _assess([_comment(3.9), _task("2026-09-07")])
    card = {
        "deal_id": 16756, "skipped": False,
        "state": {"temperature": "warm", "verdict": "poor", "work_evidence": work},
    }
    losing, _ab, neglected, reminders, _w, _f = split_sections([card])
    assert losing == []
    assert neglected == []
    assert [r["deal_id"] for r in reminders] == [16756]


def test_a_reminder_keeps_the_dates_that_prove_it():
    """Напоминания печатаются без скобок — но не это: даты и есть довод."""
    work = _assess([_comment(3.9), _task("2026-09-07")])
    card = format_card(
        {
            "deal_id": 16756, "skipped": False,
            "state": {
                "temperature": "warm", "verdict": "poor", "work_evidence": work,
            },
        },
        "ЖК «Доминион»",
        WEBHOOK,
    )
    assert "🔔 Напоминание" in card
    assert "Работа не подтверждена" not in card
    assert "(клиент возвращается 2026-09-01, дело на 2026-09-07)" in card


def test_a_plain_reminder_still_prints_without_brackets():
    work = _assess([_comment(3.9)])
    card = format_card(
        {
            "deal_id": 14052, "skipped": False,
            "state": {
                "temperature": "warm", "verdict": "poor", "work_evidence": work,
            },
        },
        "ЖК «Садовые кварталы»",
        WEBHOOK,
    )
    assert "🔔 Напоминание" in card
    assert "норма" not in card
    assert "последний след" not in card
