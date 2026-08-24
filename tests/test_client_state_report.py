"""Отчёт РОПу: русские значения и вывод прошлого разбора вместо пропуска."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state_report import (  # noqa: E402
    format_card,
    format_next_step,
    format_summary,
    humanize,
    ru,
    RISK_RU,
    TEMPERATURE_RU,
    VERDICT_RU,
    WHO_RU,
)

WEBHOOK = "https://b24-po7frr.bitrix24.ru/rest/1/token/"


def _state(**over: Any) -> dict[str, Any]:
    state = {
        "client_goal": "Покупка квартиры в ЖК «Will Towers»",
        "situation": "Обращение с Циан, разговор не зафиксирован",
        "next_step": {"what": "Позвонить клиенту", "when": "unknown", "who": "broker"},
        "risk": "medium",
        "confidence": 0.8,
        "recoverable": True,
        "missing": ["сроки покупки"],
        "temperature": "warm",
        "temperature_reason": "не названы сроки",
        "verdict": "poor",
        "verdict_reason": "не хватает обязательных фактов: budget",
        "contradictions": [],
    }
    state.update(over)
    return state


def _result(**over: Any) -> dict[str, Any]:
    result = {"deal_id": 16858, "skipped": False, "reason": "", "state": _state()}
    result.update(over)
    return result


# ── Перевод значений ───────────────────────────────────────────────────
def test_no_service_codes_leak_into_the_report():
    card = format_card(_result(), "ЖК «Will Towers»", WEBHOOK)
    for code in ("warm", "medium", "broker", "poor", "unknown"):
        assert code not in card, f"код {code!r} остался непереведённым"


def test_translations_are_used():
    card = format_card(_result(), "ЖК «Will Towers»", WEBHOOK)
    assert "Температура: тёплый" in card
    assert "Риск: средний" in card
    assert "Оценка карточки: плохо" in card
    assert "брокер" in card


def test_every_enum_value_has_a_translation():
    """Новое значение в коде без перевода — служебный код в отчёте у РОПа."""
    from client_state import compute_temperature  # noqa: F401
    from funnel_profiles import BUYER_PROFILE  # noqa: F401

    assert set(TEMPERATURE_RU) == {"hot", "warm", "cold", "unknown"}
    assert set(RISK_RU) == {"low", "medium", "high"}
    assert set(WHO_RU) == {"broker", "client", "unknown"}
    assert set(VERDICT_RU) == {
        "good", "tolerable", "poor", "too_early", "out_of_qc",
    }


def test_unknown_values_are_written_out():
    assert humanize("unknown") == "не указано"
    assert humanize("") == "не указано"
    assert humanize("  Покупка  ") == "Покупка"
    assert format_next_step({"what": "unknown", "when": "unknown", "who": "unknown"}) == (
        "не указано (не указано, не определён)"
    )
    assert format_next_step(None) == "не указано"


def test_unknown_code_is_shown_as_is_not_swallowed():
    """Незнакомый код лучше показать, чем молча заменить на «не указано»."""
    assert ru("teplyy", TEMPERATURE_RU) == "teplyy"


# ── Кэш вместо «Пропуск» ───────────────────────────────────────────────
def test_cached_card_shows_the_previous_analysis():
    card = format_card(
        _result(skipped=True, reason="no_new_events"), "Михаил Лужники", WEBHOOK,
    )
    assert "Пропуск" not in card
    assert "Температура: тёплый" in card
    assert "Цель: Покупка квартиры" in card
    assert "↻ без изменений с прошлого разбора" in card


def test_unchanged_card_is_rendered_the_same_way():
    card = format_card(
        _result(skipped=True, reason="unchanged"), "ЖК «Dominion»", WEBHOOK,
    )
    assert "Ситуация:" in card
    assert "↻ без изменений с прошлого разбора" in card


def test_card_without_a_stored_state_says_why():
    card = format_card(
        {"deal_id": 1, "skipped": True, "reason": "stage_out_of_qc", "state": None},
        "Задаток",
        WEBHOOK,
    )
    assert "⏭ Не разбиралась: этап вне контроля качества" in card
    assert "stage_out_of_qc" not in card


def test_analyzed_card_carries_no_cache_marker():
    assert "↻" not in format_card(_result(), "ЖК «Will Towers»", WEBHOOK)


# ── Прочее ─────────────────────────────────────────────────────────────
def test_uninformative_card_is_flagged():
    card = format_card(
        _result(state=_state(recoverable=False)), "Сделка #16808", WEBHOOK,
    )
    assert "неинформативна" in card


def test_contradiction_is_printed_with_both_quotes():
    card = format_card(
        _result(state=_state(contradictions=[{
            "what": "бюджет",
            "in_card": "до 30 млн",
            "in_call": "максимум 20 млн",
            "severity": "high",
        }])),
        "Сделка",
        WEBHOOK,
    )
    assert "⚡ Расхождение (грубое): бюджет" in card
    assert "в карточке: «до 30 млн»" in card
    assert "в разговоре: «максимум 20 млн»" in card


def test_card_links_to_the_portal():
    card = format_card(_result(), "ЖК «Will Towers»", WEBHOOK)
    assert "https://b24-po7frr.bitrix24.ru/crm/deal/details/16858/" in card


def test_summary_is_fully_russian():
    summary = format_summary({
        "funnel_label": "Покупатели",
        "total": 10, "analyzed": 4,
        "skipped_unchanged": 6,
        "temperature": {"hot": 0, "warm": 9, "cold": 1, "unknown": 0},
        "verdicts": {"good": 2, "tolerable": 3, "poor": 4, "too_early": 1,
                     "out_of_qc": 0},
        "cost_rub": 1.9,
    })
    assert "Покупатели: разобрано 4 из 10" in summary
    assert "тёплый 9" in summary
    assert "плохо 4" in summary
    assert "Из кэша без изменений: 6" in summary
    assert "1.90 ₽" in summary
    for code in ("warm", "poor", "good", "hot"):
        assert code not in summary
