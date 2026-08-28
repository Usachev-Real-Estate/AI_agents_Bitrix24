"""«Шаг не назначен» и «дело стоит на 01.09» — про одну и ту же карточку.

#13520 (прогон 28.08 11:44):

    Шаг: шаг не назначен
    ➡️ Дело стоит на 2026-09-01, но следов работы за норму этапа (3 дн.) нет

Правило проекта уже гласит, что дело с датой и есть следующий шаг, и
доказательство это лучше пересказа: его видно в CRM, а не только на словах
брокера (NEXT_STEP_FACT — вердикт так и считает). Отчёт про это не знал.

Пересказ брокера, если он есть, остаётся главным: он говорит, ЧТО будет
сделано, а дело — только когда.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state_report import (  # noqa: E402
    NO_STEP_RU,
    describe_next_step,
    format_card,
    format_sections,
)

WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"
NO_STEP = {"what": "unknown", "when": "unknown", "who": "unknown"}


def _state(**over) -> dict:
    state = {
        "temperature": "warm", "verdict": "poor", "next_step": dict(NO_STEP),
        "scheduled_task_at": "",
        "work_evidence": {
            "proven": False, "reason": "no_trace_in_window", "window_days": 3,
        },
    }
    state.update(over)
    return state


def test_a_standing_task_answers_for_the_unnamed_step():
    assert describe_next_step(_state(scheduled_task_at="2026-09-01")) == (
        "в карточке не описан, но в Битриксе стоит дело на 2026-09-01"
    )


def test_without_a_task_the_step_is_still_unnamed():
    assert describe_next_step(_state()) == NO_STEP_RU


def test_a_named_step_is_never_replaced_by_the_task():
    """Дело говорит когда, пересказ — что. Подменять одно другим нельзя."""
    state = _state(
        next_step={"what": "Показ объекта", "when": "unknown", "who": "broker"},
        scheduled_task_at="2026-09-01",
    )
    assert describe_next_step(state) == "Показ объекта (не указано, брокер)"


def test_an_old_cached_state_without_the_key_reads_as_before():
    state = _state()
    del state["scheduled_task_at"]
    assert describe_next_step(state) == NO_STEP_RU


def test_the_card_no_longer_denies_its_own_task():
    card = format_card(
        {"deal_id": 13520, "skipped": False,
         "state": _state(scheduled_task_at="2026-09-01")},
        "Самвэл земля Рассказовка",
        WEBHOOK,
    )
    assert "Шаг: в карточке не описан, но в Битриксе стоит дело на 2026-09-01" in card
    assert f"Шаг: {NO_STEP_RU}" not in card


def test_the_one_liner_says_it_too():
    """Раздел «в работе» печатается строкой — там та же беда была бы."""
    fine = {
        "deal_id": 16204, "skipped": False,
        "state": _state(
            verdict="good",
            scheduled_task_at="2026-09-02",
            work_evidence={"proven": True, "reason": "call", "window_days": 3},
        ),
    }
    text = format_sections([fine], {16204: "Марьина роща"}, WEBHOOK)
    assert "стоит дело на 2026-09-02" in text
