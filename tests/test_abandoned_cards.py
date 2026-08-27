"""Брошенная карточка — не отставание от каденса, а отдельный разговор.

Нормы этапов измеряются днями, и сделка со ста днями тишины среди них
тонет: «последний след 107 дн. назад» стоит в одном списке с «3 дн. назад»
и читается так же. #8990 — 72 дня, #10306 — 107 дней.
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
    GAP_NO_TRACE_IN_WINDOW,
    PROVEN_BY_PAUSE,
    assess_broker_work,
    next_action,
)
from funnel_profiles import BUYER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
STAGE = "C18:UC_DVW1P9"   # Повторный показ, окно 3 дня


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def _comment(days: float, text: str = "созвонились") -> dict:
    return {"kind": "comment", "created": _ago(days), "text": text}


def _task(days_ago: float, deadline: str) -> dict:
    return {
        "kind": "activity", "created": _ago(days_ago), "type_id": 2,
        "completed": "N", "deadline": deadline, "subject": "Позвонить",
    }


def _assess(events: list[dict], **over: Any) -> dict:
    params: dict[str, Any] = {
        "profile": BUYER_PROFILE, "stage_id": STAGE,
        "hours_on_stage": 24 * 365, "claims_messaged": False,
        "comment_informative": True, "abandoned_days": 30.0, "now": NOW,
    }
    params.update(over)
    return assess_broker_work(events, **params)


def test_a_card_silent_for_months_is_abandoned():
    """#10306: последний след 107 дней назад."""
    result = _assess([_comment(107.0)])
    assert result["proven"] is False
    assert result["reason"] == GAP_ABANDONED
    assert result["abandoned_days"] == 107.0


def test_a_card_below_the_threshold_is_ordinary_neglect():
    """#12954: 13 дней — это отставание, а не забвение."""
    assert _assess([_comment(13.0)])["reason"] == GAP_NO_TRACE_IN_WINDOW


def test_the_threshold_is_configurable():
    assert _assess([_comment(40.0)], abandoned_days=90.0)["reason"] != GAP_ABANDONED
    assert _assess([_comment(40.0)], abandoned_days=30.0)["reason"] == GAP_ABANDONED


def test_zero_threshold_switches_the_rule_off():
    assert _assess([_comment(400.0)], abandoned_days=0.0)["reason"] != GAP_ABANDONED


def test_an_empty_card_counts_from_its_age_on_the_stage():
    """Событий нет вовсе — считаем от возраста карточки."""
    result = _assess([], hours_on_stage=24 * 200)
    assert result["reason"] == GAP_ABANDONED
    assert result["abandoned_days"] == 200.0


def test_an_explained_pause_is_not_abandonment():
    """Объяснённое молчание — не забвение, даже долгое."""
    events = [
        _comment(40.0, "клиент вернётся из-за границы в ноябре"),
        _task(40.0, (NOW + timedelta(days=20)).isoformat()),
    ]
    result = _assess(
        events, pause_explained=True,
        pause_until=(NOW + timedelta(days=19)).date().isoformat(),
    )
    assert result["reason"] == PROVEN_BY_PAUSE


def test_a_due_task_still_names_its_date():
    """Просроченное дело называет дату — это конкретнее, чем «брошена»."""
    events = [_comment(60.0), _task(60.0, _ago(50.0))]
    assert _assess(events)["reason"] == GAP_DUE_TASK_NO_RESULT


def test_the_advice_is_a_decision_not_a_reminder():
    """По забытой карточке решают, ведём ли мы её, а не «поставьте дело»."""
    state = {
        "next_step": {"what": "показ", "who": "broker"},
        "work_evidence": {
            "proven": False, "reason": GAP_ABANDONED, "abandoned_days": 107.0,
        },
    }
    advice = next_action(state, [], NOW)
    assert "брошена 107 дн." in advice
    assert "закрывать сделку" in advice
    assert "Запланировать" not in advice
