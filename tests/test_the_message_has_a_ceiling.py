"""В одно сообщение уходит двадцать худших карточек, остальные — числом.

Решение агентства от 01.09. Отчёт задумывался на десять карточек, а с
переходом на «весь отдел РОПа» уперся в объём: у Кретова 367 карточек, из
них с вопросами около 330 — при 853 символах на карточку это 71 сообщение
подряд в полдень. Семьдесят одно сообщение не читают, их выключают.

Режется СООБЩЕНИЕ, а не аудит: разбираются по-прежнему все карточки отдела,
и полный разбор остаётся в JSON прогона.

Потолок ДЕЛИТСЯ между разделами (01.09), а не тратится сверху вниз одним
котлом. Прогон 01.09 14:30: у Кретова 358 карточек, 247 с вопросами — и
🆕 съедал остаток целиком, так что в 🔧 НЕДОРАБОТКА не попадало ни одного
разбора. Отчёт, заведённый чтобы РОП контролировал работу брокеров, раздел
про работу брокеров не показывал. Теперь доля считается от остатка потолка
и числа ещё не напечатанных разделов: лишнее перетекает вниз, последний
раздел забирает всё, что осталось.

Остаток называется числом по разделам. «Ещё 310» без разбивки не говорит,
тревога это или напоминания, — а РОП решает, открывать ли JSON, именно по
этому. Промолчать было бы тем же отсутствием, выданным за результат.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_ABANDONED,
    GAP_NO_TRACE_IN_WINDOW,
    GAP_TASK_DUE_TODAY,
)
from client_state_report import (  # noqa: E402
    CARDS_PER_MESSAGE,
    cards_genitive,
    format_sections,
)

WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"


def _card(deal_id: int, reason: str, **work: Any) -> dict[str, Any]:
    evidence = {
        "proven": False, "reason": reason, "window_days": 3, "days_quiet": 9.0,
    }
    evidence.update(work)
    return {"deal_id": deal_id, "skipped": False, "state": {
        "temperature": "warm", "verdict": "poor", "recoverable": True,
        "situation": "Показ прошёл, ждём обратную связь", "next_step": {},
        "work_evidence": evidence,
    }}


def _many(count: int, reason: str, base: int, **work: Any) -> list[dict]:
    return [_card(base + i, reason, **work) for i in range(count)]


def _body(rows: list[dict[str, Any]]) -> str:
    return format_sections(
        rows, {r["deal_id"]: "Сделка" for r in rows}, WEBHOOK,
    )


def test_only_twenty_write_ups_reach_the_message():
    body = _body(_many(40, GAP_NO_TRACE_IN_WINDOW, 2000))
    assert body.count("Ситуация:") == CARDS_PER_MESSAGE


def test_the_rest_are_counted_by_section():
    body = _body(_many(28, GAP_NO_TRACE_IN_WINDOW, 2000))
    assert (
        "… ещё 8 карточек с вопросами не поместились: 🔧 недоработка 8 "
        "— весь разбор в JSON прогона."
    ) in body


def test_no_section_starves_the_ones_below_it():
    """Каждый раздел получает свою долю потолка — дефект прогона 14:30.

    Три брошенных, двадцать пять недоработок, восемь напоминаний при
    потолке в двадцать. Раньше 🔧 забирал весь остаток (семнадцать), а 🔔
    не получал ни одного разбора и уходил в остаток числом: раздел, в
    котором есть карточки, читался как пустой.

    Теперь доля считается от остатка и числа оставшихся разделов. Худшее
    по-прежнему первое — порядок разделов не менялся, — но первый раздел
    больше не съедает место у тех, что ниже.
    """
    rows = (
        _many(3, GAP_ABANDONED, 1000, abandoned_days=40.0)
        + _many(25, GAP_NO_TRACE_IN_WINDOW, 2000)
        + _many(8, GAP_TASK_DUE_TODAY, 3000, due_task={
            "deadline": "2026-09-01", "subject": "Связаться",
            "days_overdue": 0, "due_today": True,
        })
    )
    body = _body(rows)
    # Брошенные печатаются все три: их мало, и доля им нужна не вся.
    assert body.count("Карточка брошена") == 3
    # Напоминания теперь видны разбором, а не числом в остатке.
    assert "🔔 напоминание" not in body
    assert body.count("дело стоит на сегодня") == 8
    # Недоработки поделились остатком, а не забрали его целиком.
    assert "🔧 недоработка 17" in body
    assert body.count("Ситуация:") <= CARDS_PER_MESSAGE


def test_an_unused_share_flows_down_and_is_not_lost():
    """Раздел, которому доля не нужна, отдаёт её следующим.

    Один брошенный при потолке двадцать: доля 🚨 — пять, использована одна.
    Остальные девятнадцать не пропадают, а достаются недоработке.
    """
    rows = (
        _many(1, GAP_ABANDONED, 1000, abandoned_days=40.0)
        + _many(40, GAP_NO_TRACE_IN_WINDOW, 2000)
    )
    body = _body(rows)
    assert body.count("Карточка брошена") == 1
    assert body.count("Ситуация:") == CARDS_PER_MESSAGE


def test_the_section_header_still_counts_everything():
    """Число в заголовке — правда про отдел, а не про то, что поместилось."""
    body = _body(_many(28, GAP_NO_TRACE_IN_WINDOW, 2000))
    assert "🔧 НЕДОРАБОТКА БРОКЕРА — 28" in body


def test_a_report_within_the_limit_says_nothing_about_a_remainder():
    body = _body(_many(5, GAP_NO_TRACE_IN_WINDOW, 2000))
    assert "не поместил" not in body
    assert body.count("Ситуация:") == 5


def test_one_card_over_the_limit_reads_in_the_singular():
    body = _body(_many(CARDS_PER_MESSAGE + 1, GAP_NO_TRACE_IN_WINDOW, 2000))
    assert "… ещё 1 карточка с вопросами не поместилась" in body


def test_the_message_stays_readable():
    """Ради этого всё и делалось: 330 карточек — это 71 сообщение."""
    body = _body(_many(330, GAP_NO_TRACE_IN_WINDOW, 2000))
    assert len(body) < 4000 * 6, len(body)


def test_the_genitive_of_a_card_declines():
    assert cards_genitive(1) == "карточка"
    assert cards_genitive(3) == "карточки"
    assert cards_genitive(8) == "карточек"
    assert cards_genitive(11) == "карточек"
    assert cards_genitive(21) == "карточка"
    assert cards_genitive(112) == "карточек"
