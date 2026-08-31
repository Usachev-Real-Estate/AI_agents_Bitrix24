"""Клиент отказался — спрашивать с брокера темп больше не за что.

Прогон 31.08 11:57, #16818 «Собственник — ЖК «Настоящее» не продает».
Ситуация в карточке: «Собственник сообщила по телефону, что не продает
квартиру». Отчёт сказал «Работа не подтверждена: за норму этапа ни звонка,
ни комментария (норма 1 дн.)» и посоветовал «записать в карточке, что сейчас
с клиентом». Что сейчас с клиентом, уже записано: он ушёл.

Решение агентства от 31.08: напомнить брокеру перенести сделку на этап
«Сделка проиграна». Работать не с чем, но и молчать нельзя — сделка висит
в воронке и портит РОПу картину.

Граница проведена узко: отказ — это конец сделки как таковой, а не «объект
не подошёл», «дорого» и не пауза. Флаг снимает с карточки все требования к
темпу, поэтому ошибка в нём выдаёт живую сделку за похороненную — и, как
все утверждения такого веса в этом проекте, он требует дословной цитаты.
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
    GAP_CLIENT_REFUSED,
    GAP_NO_TRACE_IN_WINDOW,
    REMINDERS,
    assess_broker_work,
    next_action,
)
from client_state_report import format_card, split_sections  # noqa: E402
from funnel_profiles import PROFILES  # noqa: E402

NOW = datetime(2026, 8, 31, 11, 0, tzinfo=timezone.utc)
WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"
BROKER = 7


def _comment(days_ago: float) -> dict[str, Any]:
    return {
        "kind": "comment", "id": 1,
        "created": (NOW - timedelta(days=days_ago)).isoformat(),
        "text": "Собственник сообщила, что не продает квартиру",
        "author_id": BROKER,
    }


def _assess(events: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "profile": PROFILES["sellers"],
        "stage_id": "NEW",
        "hours_on_stage": 24.0 * 200,
        "claims_messaged": False,
        "comment_informative": False,
        "abandoned_days": 30.0,
        "now": NOW,
    }
    kwargs.update(over)
    return assess_broker_work(events, **kwargs)


def test_a_refusal_replaces_the_cadence_claim():
    """#16818: без флага — претензия к темпу, с флагом — напоминание."""
    assert _assess([_comment(4)])["reason"] == GAP_NO_TRACE_IN_WINDOW
    work = _assess([_comment(4)], client_refused=True)
    assert work["reason"] == GAP_CLIENT_REFUSED
    assert work["proven"] is False


def test_a_refusal_outranks_abandonment():
    """«Решить, возвращать или закрывать» — вопрос там, где ответа нет."""
    work = _assess([_comment(120)], client_refused=True)
    assert work["reason"] == GAP_CLIENT_REFUSED
    assert _assess([_comment(120)])["reason"] == GAP_ABANDONED


def test_the_advice_names_the_one_thing_left_to_do():
    work = _assess([_comment(4)], client_refused=True)
    advice = next_action({"work_evidence": work, "next_step": {}}, [], now=NOW)
    assert advice == (
        "Клиент отказался — перенести сделку на этап «Сделка проиграна»"
    )


def test_a_refused_card_is_a_reminder_not_a_loss():
    """Претензии брокеру нет: он не виноват, что клиент передумал."""
    assert GAP_CLIENT_REFUSED in REMINDERS
    result = {"deal_id": 16818, "skipped": False, "state": {
        "temperature": "warm", "verdict": "poor", "recoverable": True,
        "next_step": {}, "work_evidence": {
            "proven": False, "reason": GAP_CLIENT_REFUSED,
            "window_days": 1, "days_quiet": 4.0,
        },
    }}
    losing, abandoned, neglected, reminders, _w, _f = split_sections([result])
    assert losing == [] and abandoned == [] and neglected == []
    assert [r["deal_id"] for r in reminders] == [16818]


def test_the_card_does_not_demand_a_pace():
    card = format_card(
        {"deal_id": 16818, "skipped": False, "state": {
            "temperature": "warm", "verdict": "poor", "recoverable": True,
            "next_step": {}, "work_evidence": {
                "proven": False, "reason": GAP_CLIENT_REFUSED,
                "window_days": 1, "days_quiet": 4.0,
            },
        }},
        "Собственник не продает",
        WEBHOOK,
    )
    assert "клиент отказался от сделки, а сделка висит в работе" in card
    assert "Работа не подтверждена" not in card
    # Норма этапа тут не довод: спор не о сроках.
    assert "норма 1 дн." not in card


# --- планка цитаты -------------------------------------------------------


REFUSAL = "не продает квартиру"


def _card_events(text: str) -> list[dict[str, Any]]:
    return [{
        "kind": "comment",
        "created": (NOW - timedelta(days=4)).isoformat(),
        "text": text,
    }]


def _refusal_state(quote: str) -> dict[str, Any]:
    return {
        "next_step": {"what": "связаться", "when": "2026-09-01",
                      "who": "broker"},
        "client_refused": True,
        "client_refused_quote": quote,
        "broker_work": {"comment_informative": False},
    }


def _record() -> dict[str, Any]:
    return {
        "ID": 16818, "TITLE": "Собственник не продает",
        "STAGE_ID": "NEW",
        "MOVED_TIME": (NOW - timedelta(days=40)).isoformat(),
        "contacts": [],
    }


def test_a_refusal_quoted_from_the_card_counts():
    from client_state import apply_derived_verdict

    state = _refusal_state(REFUSAL)
    apply_derived_verdict(
        state, _record(), PROFILES["sellers"], {}, _card_events(REFUSAL),
    )
    assert state["work_evidence"]["reason"] == GAP_CLIENT_REFUSED


def test_an_invented_refusal_does_not_bury_a_live_deal():
    """Модель сослалась на фразу, которой в карточке нет."""
    from client_state import apply_derived_verdict

    state = _refusal_state("клиент передумал продавать")
    apply_derived_verdict(
        state, _record(), PROFILES["sellers"], {}, _card_events(REFUSAL),
    )
    assert state["work_evidence"]["reason"] != GAP_CLIENT_REFUSED


def test_a_refusal_without_a_quote_does_not_count():
    from client_state import apply_derived_verdict

    state = _refusal_state("")
    apply_derived_verdict(
        state, _record(), PROFILES["sellers"], {}, _card_events(REFUSAL),
    )
    assert state["work_evidence"]["reason"] != GAP_CLIENT_REFUSED
