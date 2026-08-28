"""Дело со сроком — повод напомнить, а не потерять клиента.

#16736 (прогон 28.08 13:51): горячая карточка, бюджет и сроки названы,
уверенность 0.95, риск низкий, клиент вернётся в Москву к сентябрю — и
она одна стоит в разделе «🚨 ТЕРЯЕМ КЛИЕНТА». Причина: дело назначено на
сегодня, а отчёт собран в 13:51. День не кончился; терять пока нечего.

Сначала эту карточку вывели из тревоги отдельным исключением — «срок
наступил сегодня, тревога подождёт до завтра». Решение агентства от 28.08
сняло вопрос шире: просроченное дело — тоже флажок «напомнить». Брокер,
поставивший дело и не успевший его закрыть, сделал больше, чем брокер, не
поставивший ничего, и упрёка получать не должен.

Поэтому карточка с делом уходит в 🔔 и не попадает ни в тревогу, ни в
недоработки. Тревогу поднимает то, чего в карточке нет вовсе.
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
    GAP_TASK_DUE_TODAY,
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


def test_a_task_due_today_is_a_reminder():
    losing, _ab, neglected, reminders, _w, _f = split_sections(
        [_hot(16736, GAP_TASK_DUE_TODAY, overdue=0)],
    )
    assert losing == []
    assert neglected == []
    assert [r["deal_id"] for r in reminders] == [16736]


def test_an_overdue_task_is_a_reminder_too():
    """Решение агентства от 28.08: «если дело просрочено — флажок напомнить».

    Раньше просрочка в один день заводила горячую карточку в 🚨. Разница
    между «дело стоит на сегодня» и «срок вчера прошёл» — это разница в
    тексте напоминания, а не в том, теряем ли мы клиента.
    """
    losing, _ab, neglected, reminders, _w, _f = split_sections(
        [_hot(16736, GAP_DUE_TASK_NO_RESULT, overdue=1)],
    )
    assert losing == []
    assert neglected == []
    assert [r["deal_id"] for r in reminders] == [16736]


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


def test_a_due_task_without_details_is_still_a_reminder():
    """Раздел выбирается по коду разрыва, а не по подробностям о просрочке.

    Раньше отсутствие days_overdue решало судьбу карточки: поля нет —
    считаем срок вышедшим и поднимаем тревогу. Теперь поле влияет только на
    текст в скобках, и «не знаем, на сколько просрочено» перестало быть
    поводом сказать о клиенте больше, чем мы знаем.
    """
    losing, _ab, _n, reminders, _w, _f = split_sections(
        [_hot(16736, GAP_DUE_TASK_NO_RESULT)],
    )
    assert losing == []
    assert [r["deal_id"] for r in reminders] == [16736]


def test_the_temperature_does_not_change_the_section():
    """Тёплая, горячая, холодная — напоминание остаётся напоминанием."""
    for temperature in ("warm", "hot", "cold"):
        card = _hot(16736, GAP_TASK_DUE_TODAY, overdue=0)
        card["state"]["temperature"] = temperature
        losing, _ab, neglected, reminders, _w, _f = split_sections([card])
        assert losing == [], temperature
        assert neglected == [], temperature
        assert [r["deal_id"] for r in reminders] == [16736], temperature
