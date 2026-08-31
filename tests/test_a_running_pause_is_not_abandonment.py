"""Названная пауза, которая ещё идёт, — не брошенная карточка.

Прогон 31.08, #11690: «клиент отложила решение вопроса покупки до октября»,
записано тридцать один день назад, дела по карточке нет. Отчёт сказал
«🕸 Карточка брошена (ни звонка, ни комментария брокера 31 дн.)» и предложил
решить, возвращать клиента или закрывать сделку.

Дефект мой: заброшенность я поднял в начало проверок и закрыл только живым
делом на контроле. Но пауза длиннее месяца сама по себе перешагивает порог
заброшенности — значит для ВСЕХ длинных пауз без дела решение агентства от
28.08 («паузу записал, дела нет вовсе — напомнить») переставало работать.

Граница здесь по сроку, а не по факту паузы. Пауза с названным сроком в
будущем — это ответ на вопрос «почему тихо»; спор о ней идёт про дело.
Пауза без срока или пауза, срок которой прошёл, заброшенность не отменяет:
объяснение, к которому брокер не вернулся, перестаёт быть объяснением — это
та самая дыра, ради которой заброшенность и поднимали наверх.
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
    GAP_PAUSE_NO_TASK,
    PROVEN_BY_PAUSE,
    REMINDERS,
    assess_broker_work,
)
from funnel_profiles import PROFILES  # noqa: E402

NOW = datetime(2026, 8, 31, 11, 0, tzinfo=timezone.utc)
BROKER = 7


def _comment(days_ago: float) -> dict[str, Any]:
    return {
        "kind": "comment", "id": 1,
        "created": (NOW - timedelta(days=days_ago)).isoformat(),
        "text": "Клиент отложила решение вопроса покупки до октября",
        "author_id": BROKER,
    }


def _task(deadline: str) -> dict[str, Any]:
    return {
        "kind": "activity", "id": 2,
        "created": (NOW - timedelta(days=31)).isoformat(),
        "subject": "Связаться с клиентом", "completed": "N",
        "deadline": deadline, "author_id": BROKER,
    }


def _assess(events: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "profile": PROFILES["buyers"],
        "stage_id": "C1:NEW",
        "hours_on_stage": 24.0 * 200,
        "claims_messaged": False,
        "comment_informative": False,
        "abandoned_days": 30.0,
        "now": NOW,
    }
    kwargs.update(over)
    return assess_broker_work(events, **kwargs)


def test_a_running_pause_without_a_task_is_a_reminder():
    """#11690: пауза до октября, дела нет — напомнить, а не хоронить."""
    work = _assess(
        [_comment(31)], pause_explained=True, pause_until="2026-10-01",
    )
    assert work["reason"] == GAP_PAUSE_NO_TASK
    assert work["reason"] in REMINDERS


def test_a_running_pause_with_a_task_stays_proven():
    """#15908: та же пауза, но дело стоит — работа подтверждена."""
    work = _assess(
        [_comment(31), _task("2026-10-01T09:00:00+03:00")],
        pause_explained=True, pause_until="2026-10-01",
    )
    assert work["proven"] is True
    assert work["reason"] == PROVEN_BY_PAUSE


def test_a_pause_without_a_named_date_does_not_stop_abandonment():
    """«Клиент подумает» — по такой паузе не видно, что ожидание идёт."""
    work = _assess(
        [_comment(31)], pause_explained=True, pause_until="unknown",
    )
    assert work["reason"] == GAP_ABANDONED


def test_an_expired_pause_does_not_stop_abandonment():
    """Срок паузы прошёл, а к карточке никто не вернулся."""
    work = _assess(
        [_comment(31)], pause_explained=True, pause_until="2026-07-01",
    )
    assert work["reason"] == GAP_ABANDONED


def test_without_a_pause_the_silence_is_still_abandonment():
    work = _assess([_comment(31)])
    assert work["reason"] == GAP_ABANDONED
