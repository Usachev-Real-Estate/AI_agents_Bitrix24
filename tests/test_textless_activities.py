"""Звонок без темы — всё равно звонок.

Битрикс заводит звонки и задачи с пустой темой. Такое дело выбрасывалось
из доказательств целиком, и вместе с ним пропадали факты, на которых
держится половина правил: был ли звонок, стоит ли дело, когда последний
след по карточке.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import has_any_call, has_open_future_task  # noqa: E402
from client_state import build_evidence_events  # noqa: E402
from masking import build_mask_map  # noqa: E402

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
MASK = build_mask_map({})


def _activity(**over) -> dict:
    base = {
        "ID": 1, "CREATED": NOW.isoformat(), "COMPLETED": "Y",
        "SUBJECT": "", "DESCRIPTION": "",
    }
    base.update(over)
    return base


def _events(*activities) -> list[dict]:
    return build_evidence_events([], list(activities), [], MASK)


def test_a_call_without_a_subject_is_still_a_call():
    events = _events(_activity(TYPE_ID=2, DIRECTION=2))
    assert len(events) == 1
    assert has_any_call(events) is True
    assert "Исходящий звонок" in events[0]["text"]


def test_an_incoming_call_without_a_subject_is_named_incoming():
    events = _events(_activity(TYPE_ID=2, DIRECTION=1))
    assert "Входящий звонок" in events[0]["text"]


def test_a_call_of_unknown_direction_is_still_a_call():
    events = _events(_activity(TYPE_ID=2))
    assert has_any_call(events) is True
    assert events[0]["text"].startswith("Звонок")


def test_a_task_without_a_subject_keeps_its_deadline():
    events = _events(_activity(
        TYPE_ID=1, COMPLETED="N",
        DEADLINE=(NOW + timedelta(days=2)).isoformat(),
    ))
    assert len(events) == 1
    assert has_open_future_task(events, NOW) is True


def test_an_activity_with_neither_text_nor_deadline_is_dropped():
    """Пустое дело без срока — действительно ничто."""
    assert _events(_activity(TYPE_ID=6)) == []


def test_a_described_activity_keeps_its_own_words():
    """Придуманный текст не подменяет настоящий."""
    events = _events(_activity(TYPE_ID=2, SUBJECT="Звонок Марине по подборке"))
    assert events[0]["text"] == "Звонок Марине по подборке"


def test_a_textless_call_moves_the_last_trace():
    """«Брошена 102 дн.» не должна считаться мимо звонков без описания."""
    from broker_work import assess_broker_work
    from funnel_profiles import BUYER_PROFILE

    old = {"kind": "comment", "created": (NOW - timedelta(days=100)).isoformat(),
           "text": "в работе"}
    fresh = _events(_activity(
        TYPE_ID=2, DIRECTION=2, CREATED=(NOW - timedelta(days=1)).isoformat(),
    ))
    result = assess_broker_work(
        [old, *fresh], profile=BUYER_PROFILE, stage_id="C18:UC_DVW1P9",
        hours_on_stage=24 * 365, claims_messaged=False,
        comment_informative=True, abandoned_days=30.0, now=NOW,
    )
    assert result["days_quiet"] == 1.0
    assert result["proven"] is True
