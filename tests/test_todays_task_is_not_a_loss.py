"""Дело, назначенное на сегодня, — не потеря клиента.

#16736 (прогон 28.08 13:51): горячая карточка, бюджет и сроки названы,
уверенность 0.95, риск низкий, клиент вернётся в Москву к сентябрю — и
она одна стоит в разделе «🚨 ТЕРЯЕМ КЛИЕНТА». Причина: дело назначено на
сегодня, а отчёт собран в 13:51. День не кончился; терять пока нечего.

Правило «горячий и работа не подтверждена» писалось под #16066 — восемь
дней тишины при норме три. Наступивший сегодня срок такой тревоги не
заслуживает: не отпишется брокер к вечеру — следующий прогон поднимет её
сам, уже с просрочкой.

Претензия к работе при этом остаётся: карточка по-прежнему в
«недоработке», совет прежний.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_ABANDONED,
    GAP_DUE_TASK_NO_RESULT,
    GAP_NO_TRACE_IN_WINDOW,
)
from client_state_report import split_sections  # noqa: E402

WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"


def _hot(deal_id: int, reason: str, *, overdue: int | None = None) -> dict:
    work = {"proven": False, "reason": reason, "window_days": 3}
    if overdue is not None:
        work["due_task"] = {
            "deadline": "2026-08-28", "days_overdue": overdue,
            "subject": "Связаться с клиентом",
        }
    return {
        "deal_id": deal_id, "skipped": False,
        "state": {
            "temperature": "hot", "verdict": "tolerable",
            "next_step": {"what": "Связаться", "when": "2026-08-28",
                          "who": "broker"},
            "work_evidence": work,
        },
    }


def test_a_task_due_today_does_not_raise_the_alarm():
    losing, _ab, neglected, _rem, _w, _f = split_sections(
        [_hot(16736, GAP_DUE_TASK_NO_RESULT, overdue=0)],
    )
    assert losing == []
    # Претензия к работе остаётся — ушла только тревога.
    assert [r["deal_id"] for r in neglected] == [16736]


def test_a_task_overdue_by_a_day_does():
    losing, _ab, _n, _rem, _w, _f = split_sections(
        [_hot(16736, GAP_DUE_TASK_NO_RESULT, overdue=1)],
    )
    assert [r["deal_id"] for r in losing] == [16736]


def test_eight_days_of_silence_still_raises_it():
    """#16066, ради которой правило и написано."""
    losing, _ab, _n, _rem, _w, _f = split_sections(
        [_hot(16066, GAP_NO_TRACE_IN_WINDOW)],
    )
    assert [r["deal_id"] for r in losing] == [16066]


def test_an_abandoned_hot_card_still_raises_it():
    card = _hot(10122, GAP_ABANDONED)
    card["state"]["work_evidence"]["abandoned_days"] = 108.0
    losing, abandoned, _n, _rem, _w, _f = split_sections([card])
    assert [r["deal_id"] for r in losing] == [10122]
    assert [r["deal_id"] for r in abandoned] == [10122]


def test_a_due_task_without_details_is_treated_as_elapsed():
    """Подробностей о просрочке нет — молчать о тревоге не станем."""
    losing, _ab, _n, _rem, _w, _f = split_sections(
        [_hot(16736, GAP_DUE_TASK_NO_RESULT)],
    )
    assert [r["deal_id"] for r in losing] == [16736]


def test_a_warm_card_due_today_was_never_a_loss_anyway():
    card = _hot(16736, GAP_DUE_TASK_NO_RESULT, overdue=0)
    card["state"]["temperature"] = "warm"
    losing, _ab, neglected, _rem, _w, _f = split_sections([card])
    assert losing == []
    assert [r["deal_id"] for r in neglected] == [16736]
