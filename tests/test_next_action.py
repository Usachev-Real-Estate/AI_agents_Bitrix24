"""Рецепт вместо диагноза: какое дело поставить по карточке."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import has_open_future_task, next_action  # noqa: E402
from client_state_report import format_card  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
WEBHOOK = "https://b24-po7frr.bitrix24.ru/rest/1/token/"


def _step(what: str, when: str = "unknown", who: str = "broker") -> dict[str, Any]:
    return {"next_step": {"what": what, "when": when, "who": who}}


def _task(days: float, completed: str = "N") -> dict[str, Any]:
    return {
        "kind": "activity", "completed": completed,
        "created": (NOW - timedelta(days=3)).isoformat(),
        "deadline": (NOW + timedelta(days=days)).isoformat(),
    }


# ── Случай #14900: ход за клиентом ─────────────────────────────────────
def test_waiting_on_the_client_without_a_date_asks_to_agree_one():
    """«Собственник вывезет мусор, потом фотосессия» — брокер ждёт, но
    ожидание без дела ничем не отличается от забытья."""
    action = next_action(
        _step("Вывоз мусора собственником и организация фотосессии",
              who="client"),
        [], NOW,
    )
    assert action.startswith("Запланировать дело: связаться с клиентом")
    assert "согласовать срок" in action
    assert "Вывоз мусора" in action


def test_waiting_on_the_client_with_a_date_asks_to_check_on_it():
    action = next_action(
        _step("Вывезет мусор", when="2026-09-03", who="client"), [], NOW,
    )
    assert action == (
        "Запланировать дело на 2026-09-03: связаться и проверить, выполнено ли — "
        "Вывезет мусор"
    )


def test_a_vague_client_date_asks_to_pin_it_down():
    """«Начало сентября» — не дата, по ней дело не поставишь."""
    action = next_action(
        _step("Показ после отпуска", when="начало сентября 2026", who="client"),
        [], NOW,
    )
    assert "подтвердить точную дату" in action
    assert "начало сентября 2026" in action


# ── Ход за брокером ────────────────────────────────────────────────────
def test_a_broker_step_without_a_date_asks_for_one():
    action = next_action(_step("Отправить подборку"), [], NOW)
    assert action == "Запланировать дело с датой: Отправить подборку"


def test_a_broker_step_with_a_date_is_scheduled_as_is():
    action = next_action(_step("Показ", when="2026-08-28"), [], NOW)
    assert action == "Запланировать дело на 2026-08-28: Показ"


def test_a_vague_broker_date_asks_to_pin_it_down():
    action = next_action(_step("Созвон", when="в пятницу"), [], NOW)
    assert "уточнить дату" in action
    assert "в пятницу" in action


def test_no_step_at_all_asks_to_agree_one():
    action = next_action(_step("unknown"), [], NOW)
    assert action == "Запланировать дело: согласовать с клиентом следующий шаг и срок"


# ── Уже запланированное дело: вместо совета — состояние ────────────────
def test_an_open_future_task_reports_the_state_instead_of_advice():
    """Дело стоит — советовать «запланируйте дело» бессмысленно.

    Но и молчать нельзя: пустая строка в тревожном разделе оставляла РОПа
    с сигналом «теряем клиента» и без ответа, что делать.
    """
    action = next_action(_step("Показ"), [_task(days=2)], NOW)
    assert action.startswith("Дело стоит на ")
    assert action.endswith(" — ждём")
    assert "Запланировать" not in action


def test_the_state_line_names_the_nearest_deadline():
    action = next_action(_step("Показ"), [_task(days=9), _task(days=3)], NOW)
    assert (NOW + timedelta(days=3)).date().isoformat() in action


def test_a_completed_task_does_not_count():
    action = next_action(_step("Показ"), [_task(days=2, completed="Y")], NOW)
    assert action.startswith("Запланировать")


def test_a_task_whose_deadline_has_passed_does_not_count():
    action = next_action(_step("Показ"), [_task(days=-2)], NOW)
    assert not action.startswith("Дело стоит на ")


def test_has_open_future_task_ignores_comments():
    comment = {"kind": "comment", "created": NOW.isoformat(), "text": "в работе"}
    assert has_open_future_task([comment], NOW) is False


# ── В отчёте ───────────────────────────────────────────────────────────
def test_the_card_prints_the_recommendation():
    state = {
        "client_goal": "Продать квартиру", "situation": "Ждём вывоз мусора",
        "next_step": {"what": "Вывоз мусора", "when": "unknown", "who": "client"},
        "risk": "medium", "confidence": 0.85, "recoverable": True,
        "temperature": "warm", "temperature_reason": "нет шага с датой",
        "verdict": "poor", "verdict_reason": "нет фактов", "missing": [],
        "contradictions": [],
        "next_action": "Запланировать дело: связаться с клиентом и согласовать срок",
    }
    card = format_card(
        {"deal_id": 14900, "skipped": False, "reason": "", "state": state},
        "Воробьевы горы 890", WEBHOOK,
    )
    assert "➡️ Запланировать дело" in card


def test_a_card_without_a_recommendation_stays_quiet():
    state = {
        "client_goal": "x", "situation": "y",
        "next_step": {"what": "Показ", "when": "2026-08-28", "who": "broker"},
        "risk": "low", "confidence": 0.9, "recoverable": True,
        "temperature": "warm", "temperature_reason": "", "verdict": "good",
        "verdict_reason": "", "missing": [], "contradictions": [],
        "next_action": "",
    }
    card = format_card(
        {"deal_id": 1, "skipped": False, "reason": "", "state": state},
        "Сделка", WEBHOOK,
    )
    assert "➡️" not in card


# ── Прошедший срок ─────────────────────────────────────────────────────
def test_a_past_client_date_is_not_offered_as_a_plan():
    """«Запланировать дело на 19 августа», когда сегодня 26-е, — издёвка.

    Три карточки прогона 26.08 получили ровно такой совет: #15408 (19.08),
    #14370 (18.08), #15110 (06.08).
    """
    action = next_action(
        _step("Вывезет мусор", when="2026-08-19", who="client"), [], NOW,
    )
    assert action.startswith("Срок 2026-08-19 прошёл")
    assert "ответа нет" in action
    assert "назначить новый" in action


def test_a_past_broker_date_says_the_deadline_was_missed():
    action = next_action(_step("Связаться с клиентом", when="2026-08-06"), [], NOW)
    assert action.startswith("Срок 2026-08-06 прошёл")
    assert "дела нет" in action


def test_a_future_date_is_still_planned_normally():
    assert next_action(_step("Показ", when="2026-08-28"), [], NOW) == (
        "Запланировать дело на 2026-08-28: Показ"
    )


def test_todays_date_counts_as_past_not_future():
    """Срок «сегодня» без дела — уже просрочен к моменту прогона."""
    action = next_action(_step("Созвон", when="2026-08-26"), [], NOW)
    assert action.startswith("Срок 2026-08-26 прошёл")
