"""Работа описана комментарием, а звонил ли брокер клиенту — не видно.

Сверку пересказа с разговором сняли: расшифровка есть у одной карточки из
семи, и проверка работала вхолостую. Взамен смотрим на то, что видно
всегда: есть ли в таймлайне исходящий звонок клиенту.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    comment_without_outgoing_call,
    has_outgoing_call,
)
from funnel_profiles import BUYER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
STAGE = "C18:UC_DVW1P9"   # Повторный показ, окно 3 дня


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def _comment(hours: float, text: str = "поговорили, клиент думает") -> dict:
    return {"kind": "comment", "created": _ago(hours), "text": text}


def _call(hours: float, direction: int) -> dict:
    return {
        "kind": "activity", "created": _ago(hours), "type_id": 2,
        "completed": "Y", "direction": direction, "text": "Звонок",
    }


def _mark(events: list[dict], informative: bool = True) -> bool:
    return comment_without_outgoing_call(
        events, profile=BUYER_PROFILE, stage_id=STAGE,
        comment_informative=informative, now=NOW,
    )


def test_a_comment_without_any_call_is_marked():
    assert _mark([_comment(10.0)]) is True


def test_an_outgoing_call_clears_the_mark():
    assert _mark([_comment(10.0), _call(10.0, direction=2)]) is False


def test_an_incoming_call_does_not_clear_the_mark():
    """Входящий звонок — контакт, но инициатива в нём не брокера."""
    assert _mark([_comment(10.0), _call(10.0, direction=1)]) is True


def test_an_old_outgoing_call_does_not_clear_the_mark():
    """Звонок месячной давности не подтверждает работу этой недели."""
    assert _mark([_comment(10.0), _call(24 * 30, direction=2)]) is True


def test_an_empty_comment_is_not_marked():
    """Про пустой комментарий отчёт говорит отдельной строкой."""
    assert _mark([_comment(10.0, "в работе")], informative=False) is False


def test_a_card_without_comments_is_not_marked():
    """Метка про слова брокера, а не про их отсутствие."""
    assert _mark([_call(10.0, direction=1)]) is False
    assert _mark([]) is False


def test_has_outgoing_call_ignores_tasks_and_comments():
    assert has_outgoing_call([_comment(1.0)]) is False
    assert has_outgoing_call([{"kind": "activity", "type_id": 1, "direction": 2}]) is False
    assert has_outgoing_call([_call(1.0, direction=2)]) is True


def test_the_mark_is_silent_inside_the_stage_grace():
    """#16976 стояла в «рано судить» — и тут же получала пометку."""
    events = [_comment(2.0)]
    assert comment_without_outgoing_call(
        events, profile=BUYER_PROFILE, stage_id=STAGE,
        comment_informative=True, hours_on_stage=10.0, now=NOW,
    ) is False


def test_after_the_grace_the_same_card_is_marked():
    events = [_comment(2.0)]
    assert comment_without_outgoing_call(
        events, profile=BUYER_PROFILE, stage_id=STAGE,
        comment_informative=True, hours_on_stage=500.0, now=NOW,
    ) is True
