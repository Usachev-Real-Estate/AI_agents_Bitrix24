"""Число в «карточка брошена» должно значить то, что написано рядом.

Прогон 31.08, #8870: «🕸 Карточка брошена (ни звонка, ни комментария брокера
31 дн.; срок 2026-05-07, просрочено на 116 дн.)». Две цифры в одной строке
спорят: если следы были 31 день назад, то по делу от 7 мая давно есть
отписка и просрочка бы не печаталась.

Спор мнимый, а вот фраза неточная. Срок заброшенности считается от
последнего следа, а когда следов НЕТ ВОВСЕ — от возраста карточки на этапе.
Во втором случае «ни звонка, ни комментария 31 дн.» звучит так, будто до
этого что-то было; на деле не было ничего никогда, а 31 день — это сколько
карточка стоит на этапе.

Тот же класс, что и «звонки есть у N из M» без названного срока: отсутствие
данных подаётся как измеренная величина.
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
    assess_broker_work,
    next_action,
)
from client_state_report import format_card  # noqa: E402
from funnel_profiles import PROFILES  # noqa: E402

NOW = datetime(2026, 8, 31, 11, 0, tzinfo=timezone.utc)
WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"
BROKER = 7


def _assess(events: list[dict[str, Any]]) -> dict[str, Any]:
    return assess_broker_work(
        events,
        profile=PROFILES["buyers"],
        stage_id="C1:NEW",
        hours_on_stage=24.0 * 31,
        claims_messaged=False,
        comment_informative=False,
        abandoned_days=30.0,
        now=NOW,
    )


def _old_comment() -> dict[str, Any]:
    return {
        "kind": "comment", "id": 1,
        "created": (NOW - timedelta(days=45)).isoformat(),
        "text": "Клиент думает", "author_id": BROKER,
    }


def _card(work: dict[str, Any]) -> str:
    return format_card(
        {"deal_id": 8870, "skipped": False, "state": {
            "temperature": "warm", "verdict": "poor", "recoverable": True,
            "next_step": {}, "work_evidence": work,
        }},
        "Сделка",
        WEBHOOK,
    )


def test_an_empty_card_says_there_was_never_a_trace():
    """#8870: следов нет вовсе — число считается от возраста на этапе."""
    work = _assess([])
    assert work["reason"] == GAP_ABANDONED
    assert work["no_trace_at_all"] is True
    card = _card(work)
    assert "следов брокера нет вовсе, карточка на этапе 31 дн." in card
    assert "ни звонка, ни комментария" not in card


def test_a_card_with_an_old_trace_still_counts_the_silence():
    """След был сорок пять дней назад — тогда фраза про молчание верна."""
    work = _assess([_old_comment()])
    assert work["reason"] == GAP_ABANDONED
    assert work["no_trace_at_all"] is False
    card = _card(work)
    assert "ни звонка, ни комментария брокера 45 дн." in card


def test_the_advice_says_the_same_thing_as_the_card():
    """Строка карточки и совет не должны расходиться в том же числе."""
    work = _assess([])
    advice = next_action(
        {"work_evidence": work, "next_step": {}}, [], now=NOW,
    )
    assert advice.startswith("Следов работы по карточке нет вовсе, на этапе 31 дн.")
    assert "возвращать клиента в работу или закрывать сделку" in advice


def test_with_a_trace_the_advice_keeps_the_old_wording():
    work = _assess([_old_comment()])
    advice = next_action(
        {"work_evidence": work, "next_step": {}}, [_old_comment()], now=NOW,
    )
    assert advice.startswith("Карточка брошена 45 дн.")
