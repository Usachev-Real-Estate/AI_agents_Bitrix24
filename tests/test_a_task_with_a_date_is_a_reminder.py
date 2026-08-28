"""Дело со сроком — напоминание, а не обвинение.

Два решения агентства от 28.08:
  «Если дело запланировано на тот же день когда идёт аудит, то тоже надо
   просто напомнить что необходимо его выполнить».
  «Если дело просрочено, то надо поставить флажок напомнить».

До них дело со сроком в 18:00, прочитанное в 10 утра, не рассматривалось
вовсе: обвинять брокера до наступления срока нельзя. Напоминание обвинением
не является, поэтому час срока решает не «спрашивать или нет», а как об
этом сказать.
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
    GAP_ABANDONED,
    GAP_DUE_TASK_NO_RESULT,
    GAP_TASK_DUE_TODAY,
    PORTAL_TZ,
    REASON_RU,
    REMINDERS,
    SELF_ARGUED_GAPS,
    assess_broker_work,
    due_task_without_result,
)
from funnel_profiles import BUYER_PROFILE  # noqa: E402

# 09:00 по Москве: у сегодняшних дел есть и «до», и «после» внутри суток.
NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)
STAGE = "C18:UC_DVW1P9"


def _at(hour: int, day_offset: int = 0) -> str:
    portal = NOW.astimezone(PORTAL_TZ) + timedelta(days=day_offset)
    return portal.replace(hour=hour, minute=0).isoformat()


def _task(deadline: str, days_ago: float = 1.0) -> dict[str, Any]:
    return {
        "kind": "activity", "type_id": 2, "completed": "N",
        "created": (NOW - timedelta(days=days_ago)).isoformat(),
        "deadline": deadline, "subject": "Позвонить",
    }


def _comment(days_ago: float, text: str = "созвонились") -> dict[str, Any]:
    return {
        "kind": "comment", "text": text,
        "created": (NOW - timedelta(days=days_ago)).isoformat(),
    }


def _assess(events: list[dict], **over: Any) -> dict:
    params: dict[str, Any] = {
        "profile": BUYER_PROFILE, "stage_id": STAGE,
        "hours_on_stage": 24 * 10, "claims_messaged": False,
        "comment_informative": True, "abandoned_days": 30.0, "now": NOW,
    }
    params.update(over)
    return assess_broker_work(events, **params)


# ── Дело на сегодня ────────────────────────────────────────────────────
def test_a_task_due_later_today_is_already_a_reminder():
    """Срок в 18:00, сейчас девять утра — напомнить можно, упрекать нельзя."""
    result = _assess([_task(_at(18))])
    assert result["reason"] == GAP_TASK_DUE_TODAY
    assert result["due_task"]["due_today"] is True
    assert result["due_task"]["days_overdue"] == 0


def test_the_wording_does_not_claim_the_deadline_has_passed():
    """«Срок наступил» про дело на вечер — неправда, а не строгость."""
    assert "прошёл" not in REASON_RU[GAP_TASK_DUE_TODAY]
    assert "прошёл" in REASON_RU[GAP_DUE_TASK_NO_RESULT]


def test_a_task_due_earlier_today_is_named_differently():
    result = _assess([_task(_at(7))])
    assert result["reason"] == GAP_DUE_TASK_NO_RESULT
    assert result["due_task"]["due_today"] is False
    assert result["due_task"]["days_overdue"] == 0


def test_a_task_on_another_day_is_not_touched():
    assert due_task_without_result([_task(_at(12, 1))], NOW) is None


def test_a_result_written_today_clears_it():
    """Брокер позвонил в восемь и поставил дело на двенадцать — он работал."""
    events = [_task(_at(12)), _comment(0.02, "дозвонился, показ в четверг")]
    assert due_task_without_result(events, NOW) is None


def test_the_earliest_deadline_wins():
    """Одно дело просрочено, другое на сегодня — долг считается от первого."""
    result = _assess([_task(_at(18)), _task(_at(12, -3))])
    assert result["reason"] == GAP_DUE_TASK_NO_RESULT
    assert result["due_task"]["days_overdue"] == 3


def test_a_task_without_a_deadline_is_not_due():
    forever = _task("9999-12-31T00:00:00+03:00")
    assert due_task_without_result([forever], NOW) is None


# ── Место в лестнице ───────────────────────────────────────────────────
def test_both_codes_are_reminders():
    assert GAP_TASK_DUE_TODAY in REMINDERS
    assert GAP_DUE_TASK_NO_RESULT in REMINDERS


def test_both_codes_keep_their_own_argument():
    """Напоминание без даты дела РОПу нечем проверить."""
    assert GAP_TASK_DUE_TODAY in SELF_ARGUED_GAPS
    assert GAP_DUE_TASK_NO_RESULT in SELF_ARGUED_GAPS


def test_abandonment_still_outranks_the_reminder():
    """Самый мягкий вердикт не должен перебивать самый тяжёлый.

    Карточка молчит сто дней, и в ней висит дело, просроченное на
    девяносто. Разговор тут не о том, что дело пора закрыть, а о том,
    ведём ли мы эту сделку вообще.
    """
    events = [_comment(100.0), _task(_at(12, -90), days_ago=100.0)]
    assert _assess(events)["reason"] == GAP_ABANDONED


def test_a_young_card_still_gets_the_reminder():
    """Обязательство брокер назначил себе сам — отсрочка этапа его не снимает."""
    result = _assess([_task(_at(18))], hours_on_stage=2.0)
    assert result["reason"] == GAP_TASK_DUE_TODAY
