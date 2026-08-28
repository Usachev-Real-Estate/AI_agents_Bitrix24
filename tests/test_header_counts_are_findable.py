"""Цифру из шапки надо уметь найти в теле отчёта.

Прогон 28.08 12:58, покупатели:

    👤 Карточек с агентом, а не клиентом: 6

В теле метка стоит на одной карточке — остальные пять в «рано судить»,
где печатается однострочник, а он про агента молчал. РОП читает «шесть»
и не может показать ни одной.

Метка тут не украшение: агент судится другим правилом температуры
(горизонт покупки на него не распространяется), и знать, что перед тобой
агент, нужно до чтения оценки, а не после.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state_report import format_sections  # noqa: E402

WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"


def _card(deal_id: int, *, agent: bool, verdict: str = "too_early") -> dict:
    state = {
        "temperature": "warm", "verdict": verdict,
        "next_step": {"what": "Связаться", "when": "2026-08-31", "who": "broker"},
        "work_evidence": {
            "proven": True, "reason": "window_not_started", "window_days": 3,
        },
    }
    if agent:
        state["counterparty"] = {"who": "agent", "why": "тип контакта «Агент»"}
    return {"deal_id": deal_id, "skipped": False, "state": state}


def _lines(results: list[dict]) -> str:
    return format_sections(results, {}, WEBHOOK)


def test_an_agent_card_is_marked_in_the_too_early_list():
    text = _lines([_card(16988, agent=True)])
    assert "👤 агент" in text


def test_an_agent_card_is_marked_in_the_working_list():
    text = _lines([_card(16966, agent=True, verdict="good")])
    assert "👤 агент" in text


def test_a_client_card_carries_no_marker():
    assert "👤" not in _lines([_card(17152, agent=False)])


def test_every_agent_card_can_be_found():
    """Шесть в шапке — шесть в теле."""
    results = [_card(i, agent=True) for i in range(1, 7)]
    results += [_card(i, agent=False) for i in range(7, 11)]
    assert _lines(results).count("👤 агент") == 6


def test_the_marker_does_not_crowd_out_the_others():
    card = _card(15232, agent=True, verdict="poor")
    card["state"]["no_call"] = True
    line = [
        row for row in _lines([card]).split("\n") if row.startswith("🌤 #15232")
    ][0]
    assert "👤 агент" in line
    assert "📵 без звонка" in line
    assert "карточка заполнена плохо" in line


def test_a_malformed_counterparty_is_not_an_agent():
    card = _card(17040, agent=False)
    card["state"]["counterparty"] = "агент"
    assert "👤" not in _lines([card])
