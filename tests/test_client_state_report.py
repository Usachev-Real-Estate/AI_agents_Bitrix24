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
    assert "Температура: [B]тёплый[/B]" in card
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
        "good", "tolerable", "poor", "too_early", "out_of_qc", "no_rules",
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
    assert "Температура: [B]тёплый[/B]" in card
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
        "contradictions_material": 2, "contradictions_minor": 5,
        "skipped_out_of_qc": 3,
        "cost_rub": 1.9, "cost_rub_per_card": 0.475,
    })
    assert "ПОКУПАТЕЛИ" in summary
    assert "Карточек: 10 · разобрано моделью: 4" in summary
    assert "тёплый 9" in summary
    assert "плохо 4" in summary
    assert "без изменений 6" in summary
    assert "существенных 2, мелких 5" in summary
    assert "этап вне контроля 3" in summary
    assert "1.90 ₽" in summary
    for code in ("warm", "poor", "good", "hot"):
        assert code not in summary


def test_summary_separates_empty_cards_from_uninformative_ones():
    """«Пусто» и «написано, но бессодержательно» — разные претензии к брокеру."""
    summary = format_summary({
        "funnel_label": "Продавцы",
        "total": 10, "analyzed": 10,
        "temperature": {"hot": 0, "warm": 2, "cold": 0, "unknown": 8},
        "verdicts": {"good": 0, "tolerable": 0, "poor": 2, "too_early": 6,
                     "out_of_qc": 2},
        "unrecoverable": 8, "empty_cards": 5,
        "cost_rub": 3.08, "cost_rub_per_card": 0.308,
    })
    assert "Неинформативных карточек: 8 (из них полностью пустых: 5)" in summary


def test_summary_omits_the_empty_note_when_there_are_none():
    summary = format_summary({
        "funnel_label": "Покупатели",
        "total": 1, "analyzed": 1,
        "temperature": {"hot": 0, "warm": 1, "cold": 0, "unknown": 0},
        "verdicts": {"good": 1, "tolerable": 0, "poor": 0, "too_early": 0,
                     "out_of_qc": 0},
        "unrecoverable": 1, "empty_cards": 0,
        "cost_rub": 0.5, "cost_rub_per_card": 0.5,
    })
    assert "Неинформативных карточек: 1" in summary
    assert "полностью пустых" not in summary


# ── Состав выборки: перекос должен быть виден сразу ────────────────────
def test_summary_shows_the_stage_mix_of_the_sample():
    from client_state_report import format_stage_mix

    line = format_stage_mix({"UC_KEOOG8": 7, "NEW": 2, "C18:NEW": 1})
    assert line.startswith("Этапы выборки: ")
    # Самый частый этап первым — перекос видно с первого взгляда.
    assert line.index("Переговоры 7") < line.index("Назначение встречи 2")
    assert "Подбор" in line or "C18:NEW 1" in line


def test_unknown_stage_code_is_shown_as_is():
    from client_state_report import format_stage_mix

    assert "UC_NEWSTAGE 3" in format_stage_mix({"UC_NEWSTAGE": 3})


def test_stage_mix_is_omitted_when_empty():
    from client_state_report import format_stage_mix

    assert format_stage_mix({}) == ""


def test_stage_mix_reaches_the_summary():
    summary = format_summary({
        "funnel_label": "Продавцы",
        "total": 10, "analyzed": 0,
        "temperature": {"hot": 0, "warm": 0, "cold": 0, "unknown": 0},
        "verdicts": {"good": 0, "tolerable": 0, "poor": 0, "too_early": 0,
                     "out_of_qc": 10},
        "stages": {"UC_KEOOG8": 6, "UC_FADPBF": 4},
        "cost_rub": 0.0, "cost_rub_per_card": 0.0,
    })
    assert "Переговоры 6" in summary
    assert "Поиск клиента 4" in summary


# ── Два раздела по зоне ответственности ────────────────────────────────
def _res(deal_id: int, **state_over: Any) -> dict[str, Any]:
    state = _state(**state_over)
    state.setdefault("temperature", "warm")
    return {"deal_id": deal_id, "skipped": False, "reason": "", "state": state}


def _work(proven: bool, reason: str = "no_trace") -> dict[str, Any]:
    return {
        "proven": proven, "reason": reason, "window_days": 3, "days_quiet": 9.0,
    }


def test_losing_and_neglected_are_separate_lists():
    from client_state_report import split_sections

    cold = _res(1, temperature="cold")
    cold["state"]["work_evidence"] = _work(True, "call")
    neglected = _res(2, temperature="warm")
    neglected["state"]["work_evidence"] = _work(False)
    ok = _res(3, temperature="warm")
    ok["state"]["work_evidence"] = _work(True, "call")

    losing, neglect, fine = split_sections([cold, neglected, ok])
    assert [r["deal_id"] for r in losing] == [1]
    assert [r["deal_id"] for r in neglect] == [2]
    assert [r["deal_id"] for r in fine] == [3]


def test_a_card_can_be_in_both_sections():
    """Клиент часто остывает именно потому, что с ним не работают."""
    from client_state_report import split_sections

    both = _res(4, temperature="cold")
    both["state"]["work_evidence"] = _work(False)
    losing, neglect, fine = split_sections([both])
    assert [r["deal_id"] for r in losing] == [4]
    assert [r["deal_id"] for r in neglect] == [4]
    assert fine == []


def test_an_uninformative_card_counts_as_losing_the_client():
    from client_state_report import split_sections

    blind = _res(5, recoverable=False)
    blind["state"]["work_evidence"] = _work(True, "call")
    losing, _neglect, _fine = split_sections([blind])
    assert [r["deal_id"] for r in losing] == [5]


def test_a_contradiction_counts_as_losing_the_client():
    from client_state_report import split_sections

    lying = _res(6, contradictions=[{
        "what": "бюджет", "in_card": "30", "in_call": "20", "severity": "high",
    }])
    lying["state"]["work_evidence"] = _work(True, "call")
    losing, _n, _f = split_sections([lying])
    assert [r["deal_id"] for r in losing] == [6]


def test_unproven_work_is_spelled_out_on_the_card():
    result = _res(7)
    result["state"]["work_evidence"] = {
        "proven": False, "reason": "claimed_message_no_proof",
        "window_days": 3, "days_quiet": 5.0,
    }
    card = format_card(result, "ЖК «Will Towers»", WEBHOOK)
    assert "Работа не подтверждена" in card
    assert "скриншота переписки нет" in card
    assert "норма этапа 3 дн." in card
    assert "claimed_message_no_proof" not in card


def test_proven_work_adds_no_noise():
    result = _res(8)
    result["state"]["work_evidence"] = _work(True, "call")
    assert "Работа не подтверждена" not in format_card(result, "X", WEBHOOK)


def test_empty_sections_say_so_rather_than_vanish():
    from client_state_report import format_sections

    ok = _res(9)
    ok["state"]["work_evidence"] = _work(True, "call")
    body = format_sections([ok], {9: "Сделка"}, WEBHOOK)
    assert "ТЕРЯЕМ КЛИЕНТА — 0" in body
    assert "Ни одной карточки с признаками потери" in body
    assert "НЕДОРАБОТКА БРОКЕРА — 0" in body


def test_a_card_in_both_sections_is_printed_once():
    from client_state_report import format_sections

    both = _res(10, temperature="cold")
    both["state"]["work_evidence"] = _work(False)
    body = format_sections([both], {10: "ЖК «Hide»"}, WEBHOOK)
    assert body.count("Ситуация:") == 1
    assert "#10 ЖК «Hide» — см. выше" in body
    assert "ТЕРЯЕМ КЛИЕНТА — 1" in body
    assert "НЕДОРАБОТКА БРОКЕРА — 1" in body


# ── Пробел в правилах ≠ решение агентства ──────────────────────────────
def test_a_stage_without_rules_is_not_called_out_of_quality_control():
    """«Сняли с контроля» — решение; «правил нет» — наша недоделка."""
    result = _res(11)
    result["state"]["verdict"] = "no_rules"
    result["state"]["verdict_reason"] = "правила полноты для этапа не заданы"
    card = format_card(result, "Закрытая продажа", WEBHOOK)
    assert "полнота не оценивалась" in card
    assert "вне контроля качества" not in card
    assert "no_rules" not in card


def test_summary_names_the_stages_that_have_no_rules():
    summary = format_summary({
        "funnel_label": "Продавцы",
        "total": 10, "analyzed": 10,
        "temperature": {"hot": 1, "warm": 0, "cold": 4, "unknown": 5},
        "verdicts": {"good": 0, "tolerable": 0, "poor": 5, "too_early": 1,
                     "out_of_qc": 0, "no_rules": 4},
        "stages_without_rules": {"UC_A94BGF": 4},
        "cost_rub": 3.92, "cost_rub_per_card": 0.392,
    })
    assert "Правила полноты не заданы для этапов: Закрытая продажа (На сайт) 4" in summary
    assert "полнота не оценивалась 4" in summary


def test_summary_stays_quiet_when_every_stage_has_rules():
    summary = format_summary({
        "funnel_label": "Покупатели",
        "total": 1, "analyzed": 1,
        "temperature": {"hot": 0, "warm": 1, "cold": 0, "unknown": 0},
        "verdicts": {"good": 1, "tolerable": 0, "poor": 0, "too_early": 0,
                     "out_of_qc": 0, "no_rules": 0},
        "stages_without_rules": {},
        "cost_rub": 0.5, "cost_rub_per_card": 0.5,
    })
    assert "Правила полноты не заданы" not in summary


def test_a_trace_from_today_is_not_called_zero_days_ago():
    result = _res(12)
    result["state"]["work_evidence"] = {
        "proven": False, "reason": "comment_says_nothing",
        "window_days": 1, "days_quiet": 0.3,
    }
    card = format_card(result, "диспозл excel", WEBHOOK)
    assert "последний след сегодня" in card
    assert "0 дн. назад" not in card


# ── Этап без квалификации клиента ──────────────────────────────────────
