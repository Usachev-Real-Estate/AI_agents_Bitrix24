"""Дело со сроком — повод напомнить, а не потерять клиента.

#16736 (прогон 28.08 13:51): горячая карточка, бюджет и сроки названы,
уверенность 0.95, риск низкий, клиент вернётся в Москву к сентябрю — и
она одна стоит в разделе «🚨 ТЕРЯЕМ КЛИЕНТА». Причина: дело назначено на
сегодня, а отчёт собран в 13:51. День не кончился; терять пока нечего.

Сначала эту карточку вывели из тревоги отдельным исключением — «срок
наступил сегодня, тревога подождёт до завтра». Решение агентства от 28.08
сняло вопрос шире: дело со сроком — флажок «напомнить». Брокер,
поставивший дело и не успевший его закрыть, сделал больше, чем брокер, не
поставивший ничего.

Дело на сегодня так и осталось напоминанием. А вот просроченное дело без
результата с 01.09 — недоработка: раз живое дело на контроле снимает
претензию к темпу, дело с прошедшим сроком и пустым таймлайном под ним —
это тот случай, где контроль оказался фикцией.

Что не изменилось и ради чего этот файл написан: ни то, ни другое не
поднимает 🚨. Тревогу поднимает то, чего в карточке нет вовсе.
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
    GAP_NO_TRACE,
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


def test_an_overdue_task_is_a_reproach_but_not_a_loss():
    """Решение агентства от 01.09: просроченное дело — недоработка.

    Раньше просрочка в один день заводила горячую карточку в 🚨, и ради
    этого файл и написан: 🚨 она не поднимает и теперь. Но и в 🔔 больше не
    уходит — раздел сменился на 🔧, когда живое дело на контроле стало
    снимать претензию к темпу.
    """
    losing, _ab, neglected, reminders, _w, _f = split_sections(
        [_hot(16736, GAP_DUE_TASK_NO_RESULT, overdue=1)],
    )
    assert losing == []
    assert reminders == []
    assert [r["deal_id"] for r in neglected] == [16736]


def test_eight_days_of_silence_still_raises_it():
    """#16066, ради которой правило и написано.

    Разрыв здесь no_trace: с 31.08 в тревогу ведёт «нет вообще ничего» и
    «брошена», а отставание за норму этапа осталось недоработкой. Смысл
    теста от этого не меняется — он про то, что дело на сегодня спасает
    карточку, а тишина нет.
    """
    losing, _ab, _n, _rem, _w, _f = split_sections(
        [_hot(16066, GAP_NO_TRACE)],
    )
    assert [r["deal_id"] for r in losing] == [16066]


def test_an_abandoned_hot_card_still_raises_it():
    card = _hot(10122, GAP_ABANDONED)
    card["state"]["work_evidence"]["abandoned_days"] = 108.0
    losing, abandoned, _n, _rem, _w, _f = split_sections([card])
    assert [r["deal_id"] for r in losing] == [10122]
    assert [r["deal_id"] for r in abandoned] == [10122]


def test_a_due_task_without_details_still_does_not_raise_the_alarm():
    """Раздел выбирается по коду разрыва, а не по подробностям о просрочке.

    Раньше отсутствие days_overdue решало судьбу карточки: поля нет —
    считаем срок вышедшим и поднимаем тревогу. Теперь поле влияет только на
    текст в скобках, и «не знаем, на сколько просрочено» перестало быть
    поводом сказать о клиенте больше, чем мы знаем.
    """
    losing, _ab, neglected, _rem, _w, _f = split_sections(
        [_hot(16736, GAP_DUE_TASK_NO_RESULT)],
    )
    assert losing == []
    assert [r["deal_id"] for r in neglected] == [16736]


def test_the_temperature_does_not_change_the_section():
    """Тёплая, горячая, холодная — напоминание остаётся напоминанием."""
    for temperature in ("warm", "hot", "cold"):
        card = _hot(16736, GAP_TASK_DUE_TODAY, overdue=0)
        card["state"]["temperature"] = temperature
        losing, _ab, neglected, reminders, _w, _f = split_sections([card])
        assert losing == [], temperature
        assert neglected == [], temperature
        assert [r["deal_id"] for r in reminders] == [16736], temperature
