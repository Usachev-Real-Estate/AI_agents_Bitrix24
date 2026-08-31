"""Цифру из шапки надо уметь найти в теле отчёта.

Прогон 28.08 12:58, покупатели:

    👤 Карточек с агентом, а не клиентом: 6

В теле метка стоит на одной карточке — остальные пять в «рано судить»,
где печатается однострочник, а он про агента молчал. РОП читает «шесть»
и не может показать ни одной.

Метка тут не украшение: агент судится другим правилом температуры
(горизонт покупки на него не распространяется), и знать, что перед тобой
агент, нужно до чтения оценки, а не после.

С 31.08 у правила есть названная граница. Разделы ⏳ и ✅ из отчёта убраны
решением агентства: отчёт нужен РОПу, чтобы контролировать работу брокеров,
и карточка без вопросов места в нём не занимает. Значит цифры шапки,
считающие ВСЕ карточки — про агента, про звонки, про неинформативность, —
в теле проверяются только по тем, к которым есть вопрос. Остальные сходятся
строкой «✅ Без вопросов: N …»: имя карточки в теле не найти, но и
потерянной она не выглядит.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state_report import format_sections  # noqa: E402

WEBHOOK = "https://example.bitrix24.ru/rest/1/token/"


def _card(
    deal_id: int, *, agent: bool, verdict: str = "poor",
    proven: bool = False, reason: str = "no_trace_in_window",
) -> dict:
    """Карточка с вопросом к работе — только такие и печатаются с 31.08."""
    state = {
        "temperature": "warm", "verdict": verdict,
        "next_step": {"what": "Связаться", "when": "2026-08-31", "who": "broker"},
        "work_evidence": {
            "proven": proven, "reason": reason, "window_days": 3,
            "days_quiet": 9.0,
        },
    }
    if agent:
        state["counterparty"] = {"who": "agent", "why": "тип контакта «Агент»"}
    return {"deal_id": deal_id, "skipped": False, "state": state}


def _lines(results: list[dict]) -> str:
    return format_sections(results, {}, WEBHOOK)


def test_an_agent_card_with_a_claim_is_marked():
    assert "Контрагент: агент" in _lines([_card(16988, agent=True)])


def test_a_questionless_agent_card_is_counted_not_printed():
    """Граница правила, названная 31.08.

    К карточке вопросов нет — в тело она не идёт, и метки на ней не найти.
    Но и «потерялась» про неё сказать нельзя: строка учёта её называет.
    """
    text = _lines([_card(16966, agent=True, verdict="good",
                         proven=True, reason="call")])
    assert "👤" not in text
    assert "✅ Без вопросов: 1 в работе" in text


def test_a_client_card_carries_no_marker():
    assert "👤" not in _lines([_card(17152, agent=False)])


def test_every_agent_card_with_a_claim_can_be_found():
    """Шесть в шапке — шесть в теле, пока к ним есть вопрос."""
    results = [_card(i, agent=True) for i in range(1, 7)]
    results += [_card(i, agent=False) for i in range(7, 11)]
    assert _lines(results).count("Контрагент: агент") == 6


def test_the_markers_do_not_crowd_each_other_out():
    """На полном разборе метки стоят все и не вытесняют друг друга."""
    card = _card(15232, agent=True, verdict="poor")
    card["state"]["no_call"] = True
    text = _lines([card])
    assert "Контрагент: агент" in text
    assert "📵" in text
    assert "Оценка карточки: плохо" in text


def test_a_malformed_counterparty_is_not_an_agent():
    card = _card(17040, agent=False)
    card["state"]["counterparty"] = "агент"
    assert "👤" not in _lines([card])
