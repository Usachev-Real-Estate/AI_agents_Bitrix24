"""Засчитываем только дело брокера или его РОПа.

Решение агентства от 28.08. До него `assess_broker_work` автора дела не
смотрел вовсе: дело, заведённое бизнес-процессом или роботом, могло нести
длинный типовой текст — и засчитывалось брокеру как его работа, его пауза и
его контроль над карточкой.

Фильтруем только незакрытые дела. Закрытая активность — это запись о
состоявшемся разговоре, и для факта разговора неважно, кто её отметил.

Автора не знаем — засчитываем. Незнание не должно превращаться в претензию:
это то же самое отсутствие данных, выданное за результат.
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
    GAP_NO_TRACE_IN_WINDOW,
    GAP_PAUSE_NO_TASK,
    GAP_WAITING_NO_TASK,
    PROVEN_BY_CALL,
    PROVEN_BY_PAUSE,
    PROVEN_BY_TASK_PLAN,
    assess_broker_work,
    tasks_of_the_broker,
)
from funnel_profiles import BUYER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
STAGE = "C18:UC_DVW1P9"   # Повторный показ, окно 3 дня

BROKER = 11
ROP = 22
ROBOT = 99
OURS = {BROKER, ROP}

PLAN = "Показать на Ленина 5 и Мира 12: клиент отказался от подборки из-за этажа"


def _task(author: int | None, *, ago: float = 1.0, ahead: float = 2.0,
          subject: str = "Позвонить", description: str = "") -> dict[str, Any]:
    event: dict[str, Any] = {
        "kind": "activity", "type_id": 2, "completed": "N",
        "created": (NOW - timedelta(days=ago)).isoformat(),
        "deadline": (NOW + timedelta(days=ahead)).isoformat(),
        "subject": subject, "description": description,
    }
    if author is not None:
        event["author_id"] = author
    return event


def _comment(ago: float, text: str = "созвонились") -> dict[str, Any]:
    return {
        "kind": "comment", "text": text,
        "created": (NOW - timedelta(days=ago)).isoformat(),
    }


def _call(author: int, ago: float = 1.0) -> dict[str, Any]:
    return {
        "kind": "activity", "type_id": 2, "completed": "Y",
        "created": (NOW - timedelta(days=ago)).isoformat(),
        "author_id": author, "subject": "Звонок",
    }


def _assess(events: list[dict], **over: Any) -> dict:
    params: dict[str, Any] = {
        "profile": BUYER_PROFILE, "stage_id": STAGE,
        "hours_on_stage": 24 * 365, "claims_messaged": False,
        "comment_informative": True, "abandoned_days": 30.0,
        "task_authors": OURS, "now": NOW,
    }
    params.update(over)
    return assess_broker_work(events, **params)


# ── Сам отбор ──────────────────────────────────────────────────────────
def test_a_robots_task_is_dropped():
    kept = tasks_of_the_broker([_task(ROBOT)], OURS)
    assert kept == []


def test_the_brokers_and_the_rops_tasks_stay():
    events = [_task(BROKER), _task(ROP)]
    assert tasks_of_the_broker(events, OURS) == events


def test_an_unknown_author_gets_the_benefit_of_the_doubt():
    """Мы не знаем, чьё дело, — и не делаем из этого претензии."""
    events = [_task(None)]
    assert tasks_of_the_broker(events, OURS) == events


def test_an_empty_author_set_switches_the_rule_off():
    """Карты не построились — правило молчит, а не работает наоборот."""
    events = [_task(ROBOT)]
    assert tasks_of_the_broker(events, set()) == events
    assert tasks_of_the_broker(events, None) == events


def test_a_closed_activity_is_kept_whoever_logged_it():
    """Разговор состоялся — для факта разговора автор отметки неважен."""
    events = [_call(ROBOT)]
    assert tasks_of_the_broker(events, OURS) == events


# ── Что это меняет в оценке ────────────────────────────────────────────
def test_a_robots_described_task_is_not_the_brokers_work():
    robot = _task(ROBOT, subject="Показ", description=PLAN)
    assert _assess([robot])["reason"] != PROVEN_BY_TASK_PLAN


def test_the_same_task_from_the_broker_counts():
    mine = _task(BROKER, subject="Показ", description=PLAN)
    assert _assess([mine])["reason"] == PROVEN_BY_TASK_PLAN


def test_a_robots_task_does_not_hold_an_explained_pause():
    """Пауза засчитывается только вместе с делом брокера на контроле."""
    events = [
        _comment(2.0, "клиент в отпуске до 5 сентября"),
        _task(ROBOT, ahead=6.0),
    ]
    result = _assess(
        events, pause_explained=True,
        pause_until=(NOW + timedelta(days=5)).date().isoformat(),
    )
    assert result["reason"] == GAP_PAUSE_NO_TASK


def test_the_brokers_task_does_hold_it():
    events = [
        _comment(2.0, "клиент в отпуске до 5 сентября"),
        _task(BROKER, ahead=6.0),
    ]
    result = _assess(
        events, pause_explained=True,
        pause_until=(NOW + timedelta(days=5)).date().isoformat(),
    )
    assert result["reason"] == PROVEN_BY_PAUSE


def test_a_robots_task_is_not_waiting_either():
    events = [_comment(1.0, "агент обещал набрать"), _task(ROBOT, ahead=4.0)]
    result = _assess(
        events, next_step_who="client",
        next_step_when=(NOW + timedelta(days=4)).date().isoformat(),
    )
    assert result["reason"] == GAP_WAITING_NO_TASK


def test_a_robots_task_does_not_hold_an_abandoned_card():
    """«Карточку держат» — это про брокера, а не про автоматику."""
    events = [_comment(200.0), _task(ROBOT, ago=200.0, ahead=30.0)]
    assert _assess(events)["reason"] == GAP_ABANDONED


def test_the_brokers_task_holds_the_abandoned_card():
    events = [_comment(200.0), _task(BROKER, ago=200.0, ahead=30.0)]
    assert _assess(events)["reason"] != GAP_ABANDONED


def test_a_robots_overdue_task_does_not_nag_the_broker():
    events = [_comment(10.0), _task(ROBOT, ago=10.0, ahead=-5.0)]
    result = _assess(events)
    assert "due_task" not in result
    assert result["reason"] == GAP_NO_TRACE_IN_WINDOW


def test_a_robots_call_still_proves_the_conversation():
    assert _assess([_call(ROBOT)])["reason"] == PROVEN_BY_CALL
