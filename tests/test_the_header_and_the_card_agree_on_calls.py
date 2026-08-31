"""Шапка и карточка отвечают на один вопрос одинаково.

Прогон 31.08: шапка говорила «📞 Звонки есть у 1 из 10 карточек», а #10994
внутри — «🕸 Карточка брошена (ни звонка, ни комментария брокера 106 дн.)».
По карточке шли пропущенные входящие звонки: контакт был, разговора не
было. Счётчик шапки считал их звонками, строка карточки — нет.

Вопросов на самом деле два, и они разные:

* состоялся ли РАЗГОВОР — по нему карточка получает PROVEN_BY_CALL, и его
  же обещает шапка («главное доказательство работы — сам факт разговора»);
* была ли ПОПЫТКА связи — по ней проверяется утверждение «клиент не
  отвечает», и непринятый вызов там как раз довод, а не его отсутствие.

Смешивать их нельзя ни в ту, ни в другую сторону.
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
    PROVEN_BY_CALL,
    assess_broker_work,
    has_a_conversation,
    has_any_call,
)
from funnel_profiles import BUYER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 31, 10, 52, tzinfo=timezone.utc)
STAGE = "C18:UC_DVW1P9"


def _call(completed: str, ago: float = 1.0) -> dict[str, Any]:
    return {
        "kind": "activity", "type_id": 2, "completed": completed,
        "created": (NOW - timedelta(days=ago)).isoformat(),
        "subject": "Входящий звонок",
    }


def _transcript(ago: float = 1.0) -> dict[str, Any]:
    return {
        "kind": "transcript", "text": "",
        "created": (NOW - timedelta(days=ago)).isoformat(),
    }


def _assess(events: list[dict]) -> str:
    return assess_broker_work(
        events, profile=BUYER_PROFILE, stage_id=STAGE,
        hours_on_stage=24 * 10, claims_messaged=False,
        comment_informative=True, abandoned_days=30.0, now=NOW,
    )["reason"]


def test_a_missed_call_is_not_a_conversation():
    """Счётчик шапки не должен обещать разговор, которого не было."""
    assert has_a_conversation([_call("N")]) is False


def test_a_missed_call_is_still_a_contact_attempt():
    """А вот «клиент не отвечает» непринятым вызовом как раз доказывается."""
    assert has_any_call([_call("N")]) is True


def test_an_answered_call_is_both():
    assert has_a_conversation([_call("Y")]) is True
    assert has_any_call([_call("Y")]) is True


def test_a_recording_counts_as_a_conversation_even_before_it_is_typed():
    """Расшифровка ещё не готова — но разговор уже был."""
    assert has_a_conversation([_transcript()]) is True


def test_the_card_says_the_same_as_the_header():
    """Тот же ответ, что даёт счётчик, должен стоять и в карточке."""
    answered = [_call("Y")]
    assert has_a_conversation(answered) is True
    assert _assess(answered) == PROVEN_BY_CALL

    missed = [_call("N")]
    assert has_a_conversation(missed) is False
    assert _assess(missed) != PROVEN_BY_CALL


def test_the_run_counter_uses_the_conversation_test():
    """Счётчик берётся из envelope['has_call'] — проверяем сам источник."""
    import client_state as cs

    assert cs.has_a_conversation is has_a_conversation
