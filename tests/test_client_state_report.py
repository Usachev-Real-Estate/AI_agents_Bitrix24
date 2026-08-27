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
        "skipped_out_of_qc": 3,
        "cost_rub": 1.9, "cost_rub_per_card": 0.475,
    })
    assert "ПОКУПАТЕЛИ" in summary
    assert "Карточек: 10 · разобрано моделью: 4" in summary
    assert "тёплый 9" in summary
    assert "плохо 4" in summary
    assert "без изменений 6" in summary
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

    losing, _aband, neglect, _rem, _wait, fine = split_sections([cold, neglected, ok])
    assert [r["deal_id"] for r in losing] == [1]
    assert [r["deal_id"] for r in neglect] == [2]
    assert [r["deal_id"] for r in fine] == [3]


def test_a_card_can_be_in_both_sections():
    """Клиент часто остывает именно потому, что с ним не работают."""
    from client_state_report import split_sections

    both = _res(4, temperature="cold")
    both["state"]["work_evidence"] = _work(False)
    losing, _aband, neglect, _rem, _wait, fine = split_sections([both])
    assert [r["deal_id"] for r in losing] == [4]
    assert [r["deal_id"] for r in neglect] == [4]
    assert fine == []


def test_an_uninformative_card_counts_as_losing_the_client():
    from client_state_report import split_sections

    blind = _res(5, recoverable=False)
    blind["state"]["work_evidence"] = _work(True, "call")
    losing, _aband, _neglect, _rem, _w, _f = split_sections([blind])
    assert [r["deal_id"] for r in losing] == [5]


def test_unproven_work_is_spelled_out_on_the_card():
    result = _res(7)
    result["state"]["work_evidence"] = {
        "proven": False, "reason": "claimed_message_no_proof",
        "window_days": 3, "days_quiet": 5.0,
    }
    card = format_card(result, "ЖК «Will Towers»", WEBHOOK)
    assert "Работа не подтверждена" in card
    assert "скриншота переписки нет" in card
    assert "claimed_message_no_proof" not in card
    # Норма этапа к отсутствию скриншота отношения не имеет: цифры
    # приводим только там, где они и есть довод.
    assert "норма" not in card


def test_a_timing_gap_still_shows_the_norm():
    result = _res(70)
    result["state"]["work_evidence"] = {
        "proven": False, "reason": "no_trace_in_window",
        "window_days": 3, "days_quiet": 9.0,
    }
    card = format_card(result, "ЖК «Will Towers»", WEBHOOK)
    assert "норма 3 дн., последний след 9 дн. назад" in card


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
        "proven": False, "reason": "no_trace_in_window",
        "window_days": 1, "days_quiet": 0.3,
    }
    card = format_card(result, "диспозл excel", WEBHOOK)
    assert "последний след сегодня" in card
    assert "0 дн. назад" not in card


# ── Этап без квалификации клиента ──────────────────────────────────────


# ── Отсрочка и раздел «теряем клиента» не должны спорить ───────────────


def test_a_card_inside_its_grace_period_is_not_called_a_loss():
    """«Рано судить» и «теряем клиента» на одной карточке — противоречие."""
    from client_state_report import split_sections

    fresh = _res(20, recoverable=False, temperature="unknown")
    fresh["state"]["verdict"] = "too_early"
    fresh["state"]["verdict_reason"] = "этап моложе отсрочки (23 ч < 72 ч)"
    losing, _aband, _neglect, _rem, waiting, fine = split_sections([fresh])
    assert losing == []
    # И не «в работе»: работа по ней ещё не начиналась, галочка ✅ здесь лжёт.
    assert fine == []
    assert [r["deal_id"] for r in waiting] == [20]


def test_a_cold_client_inside_grace_is_still_a_loss():
    """Клиент сказал «нет» — срок этому не оправдание."""
    from client_state_report import split_sections

    refused = _res(21, temperature="cold")
    refused["state"]["verdict"] = "too_early"
    losing, _aband, _n, _rem, _w, _f = split_sections([refused])
    assert [r["deal_id"] for r in losing] == [21]


def test_an_empty_card_past_its_grace_is_still_a_loss():
    """После отсрочки пустая карточка — уже претензия, а не ожидание."""
    from client_state_report import split_sections

    stale = _res(23, recoverable=False, temperature="unknown")
    stale["state"]["verdict"] = "poor"
    losing, _aband, _n, _rem, _w, _f = split_sections([stale])
    assert [r["deal_id"] for r in losing] == [23]


def test_an_empty_fresh_card_is_never_marked_as_in_progress():
    """#16918 — ни комментария, ни дела; ✅ по ней читается как «всё хорошо»."""
    from client_state_report import format_sections

    fresh = _res(16918, recoverable=False, temperature="unknown")
    fresh["state"]["verdict"] = "too_early"
    body = format_sections([fresh], {16918: "Анна СК"}, WEBHOOK)
    assert "РАНО СУДИТЬ — 1" in body
    assert "В РАБОТЕ" not in body


def test_the_waiting_section_comes_before_the_healthy_one():
    from client_state_report import format_sections

    waiting = _res(1, temperature="unknown")
    waiting["state"]["verdict"] = "too_early"
    waiting["state"]["work_evidence"] = _work(True, "call")
    healthy = _res(2, temperature="warm")
    healthy["state"]["verdict"] = "good"
    healthy["state"]["work_evidence"] = _work(True, "call")
    body = format_sections([waiting, healthy], {1: "Свежий", 2: "Живой"}, WEBHOOK)
    assert body.index("РАНО СУДИТЬ") < body.index("В РАБОТЕ")


def test_the_two_sections_stop_being_identical_lists():
    """Прошлый прогон: 10 карточек и там, и там. Разделение не разделяло."""
    from client_state_report import split_sections

    unworked = [
        _res(i, recoverable=False, temperature="unknown") for i in range(1, 11)
    ]
    for card in unworked:
        card["state"]["verdict"] = "poor"
        card["state"]["work_evidence"] = _work(False)
    losing, _aband, neglect, _rem, _w, _f = split_sections(unworked)
    assert losing == []
    assert len(neglect) == 10


def test_a_hot_client_nobody_works_is_a_loss():
    """#16066: бюджет 130 млн, согласован шаг, 8 дней тишины при норме 3.

    Такая карточка лежала вторым пунктом среди восьми недоработок.
    """
    from client_state_report import split_sections

    hot = _res(16066, temperature="hot")
    hot["state"]["work_evidence"] = _work(False)
    losing, _aband, neglect, _rem, _w, _f = split_sections([hot])
    assert [r["deal_id"] for r in losing] == [16066]
    assert [r["deal_id"] for r in neglect] == [16066]


def test_a_hot_client_being_worked_is_not_a_loss():
    from client_state_report import split_sections

    hot = _res(1, temperature="hot")
    hot["state"]["work_evidence"] = _work(True, "call")
    losing, _aband, _n, _rem, _w, fine = split_sections([hot])
    assert losing == []
    assert [r["deal_id"] for r in fine] == [1]


def test_a_warm_client_unworked_stays_a_broker_matter():
    """Правило добавлено только для горячих — иначе разделы снова сольются."""
    from client_state_report import split_sections

    warm = _res(2, temperature="warm")
    warm["state"]["work_evidence"] = _work(False)
    losing, _aband, neglect, _rem, _w, _f = split_sections([warm])
    assert losing == []
    assert [r["deal_id"] for r in neglect] == [2]


def test_due_task_line_shows_the_deadline_not_the_stage_norm():
    """У наступившего срока своя арифметика: спрашивают за конкретное дело."""
    from broker_work import GAP_DUE_TASK_NO_RESULT
    from client_state_report import format_card

    res = _res(6)
    res["state"]["work_evidence"] = {
        "proven": False, "reason": GAP_DUE_TASK_NO_RESULT,
        "window_days": 3, "days_quiet": 9.0,
        "due_task": {"deadline": "2026-08-26", "subject": "Позвонить", "days_overdue": 0},
    }
    card = format_card(res, "ЖК «Will Towers»", WEBHOOK)
    assert "срок 2026-08-26, срок сегодня" in card
    assert "норма" not in card


def test_overdue_task_line_counts_the_days():
    from broker_work import GAP_DUE_TASK_NO_RESULT
    from client_state_report import format_card

    res = _res(7)
    res["state"]["work_evidence"] = {
        "proven": False, "reason": GAP_DUE_TASK_NO_RESULT,
        "window_days": 3, "days_quiet": 9.0,
        "due_task": {"deadline": "2026-08-20", "subject": "", "days_overdue": 6},
    }
    assert "просрочено на 6 дн." in format_card(res, "ЖК «Will Towers»", WEBHOOK)


def test_agent_card_is_marked():
    from client_state_report import format_card

    res = _res(8)
    res["state"]["counterparty"] = {"who": "agent", "why": "тип контакта «Агент»"}
    card = format_card(res, "Лариса агент", WEBHOOK)
    assert "👤 Контрагент: агент — тип контакта «Агент»" in card


def test_client_card_is_not_marked():
    """Клиент — норма; строка на каждой карточке была бы шумом."""
    from client_state_report import format_card

    res = _res(9)
    res["state"]["counterparty"] = {"who": "client", "why": ""}
    assert "Контрагент" not in format_card(res, "ЖК «Will Towers»", WEBHOOK)


def test_a_waiting_card_is_a_reminder_not_an_accusation():
    """#16798: агент сам сказал, что наберёт в сентябре."""
    from broker_work import GAP_WAITING_NO_TASK
    from client_state_report import format_card, split_sections

    res = _res(10)
    res["state"]["work_evidence"] = {
        "proven": False, "reason": GAP_WAITING_NO_TASK,
        "window_days": 2, "days_quiet": 5.0,
    }
    res["state"]["counterparty"] = {"who": "agent", "why": "тип контакта «Агент»"}
    losing, _aband, neglect, reminders, _wait, fine = split_sections([res])
    assert [r["deal_id"] for r in reminders] == [10]
    assert neglect == [] and fine == [] and losing == []

    card = format_card(res, "ЖК «Hide»", WEBHOOK)
    assert "🔔 Напоминание" in card
    assert "Работа не подтверждена" not in card
    assert "норма" not in card


def test_a_gap_at_the_norm_boundary_shows_the_decimal():
    """#14190: «норма 7 дн., последний след 7 дн. назад» — вычитание даёт ноль.

    У самой границы округление до целого превращает верную претензию в
    арифметическую ошибку на глазах у читателя.
    """
    from broker_work import GAP_NO_TRACE_IN_WINDOW
    from client_state_report import format_card

    res = _res(11)
    res["state"]["work_evidence"] = {
        "proven": False, "reason": GAP_NO_TRACE_IN_WINDOW,
        "window_days": 7, "days_quiet": 7.2,
    }
    card = format_card(res, "ЖК «Воробьев дом»", WEBHOOK)
    assert "последний след 7.2 дн. назад" in card


def test_a_gap_far_from_the_norm_stays_whole():
    from broker_work import GAP_NO_TRACE_IN_WINDOW
    from client_state_report import format_card

    res = _res(12)
    res["state"]["work_evidence"] = {
        "proven": False, "reason": GAP_NO_TRACE_IN_WINDOW,
        "window_days": 2, "days_quiet": 16.4,
    }
    assert "последний след 16 дн. назад" in format_card(res, "ЖК «Hide»", WEBHOOK)


def test_field_codes_from_the_model_are_shown_in_russian():
    """#13340: «Не хватает: budget, district, timeline» в русском отчёте."""
    from client_state_report import format_card

    res = _res(13)
    res["state"]["missing"] = ["budget", "district", "timeline"]
    card = format_card(res, "ЖК «Воробьевы Горы»", WEBHOOK)
    assert "Не хватает: бюджет, район, сроки покупки" in card


def test_an_unknown_code_is_left_as_the_model_wrote_it():
    """Своя догадка хуже чужого текста."""
    from client_state_report import format_card

    res = _res(14)
    res["state"]["missing"] = ["точная дата приезда"]
    assert "Не хватает: точная дата приезда" in format_card(res, "х", WEBHOOK)


def test_a_poor_card_in_the_fine_list_says_so():
    """#15342: ✅ на карточке, которую тот же отчёт назвал «плохо»."""
    from client_state_report import format_sections

    res = _res(15, temperature="warm")
    res["state"]["verdict"] = "poor"
    res["state"]["work_evidence"] = {"proven": True, "reason": "call"}
    body = format_sections([res], {15: "Виктори парк"}, WEBHOOK)
    assert "✅ В РАБОТЕ" in body
    assert "карточка заполнена плохо" in body


def test_an_unrecoverable_card_says_why_it_is_losing():
    """#16886 стояла в «теряем клиента» без единой строки о причине."""
    from client_state_report import format_card, split_sections

    res = _res(16, recoverable=False)
    res["state"]["work_evidence"] = {"proven": True, "reason": "call"}
    losing, _aband, _n, _rem, _w, _f = split_sections([res])
    assert [r["deal_id"] for r in losing] == [16]
    assert "так и теряют молча" in format_card(res, "Пентхаус", WEBHOOK)


def test_an_unproven_card_does_not_repeat_the_note():
    """Там, где есть 🔧, причина уже названа."""
    from client_state_report import format_card

    res = _res(17, recoverable=False)
    res["state"]["work_evidence"] = {
        "proven": False, "reason": "no_trace_in_window",
        "window_days": 3, "days_quiet": 9.0,
    }
    assert "так и теряют молча" not in format_card(res, "Пентхаус", WEBHOOK)


def test_the_summary_shows_what_the_bill_is_made_of():
    """Обе цифры лежали только в JSON прогона."""
    from client_state_report import format_summary

    text = format_summary({
        "funnel": "buyers", "funnel_label": "Покупатели", "total": 10,
        "analyzed": 8, "cost_rub": 3.92, "cost_rub_per_card": 0.49,
        "usage": {"input_tokens": 23489, "output_tokens": 14734,
                  "cached_tokens": 0, "reasoning_tokens": 8960},
    })
    assert "размышления 8960 из 14734 ток. ответа (61 %)" in text
    assert "кэш входа 0 %" in text


def test_a_run_without_llm_calls_has_no_breakdown():
    from client_state_report import cost_breakdown

    assert cost_breakdown({"usage": {}}) == ""


def test_the_summary_says_how_many_cards_have_a_readable_call():
    """Карточка без разговора разобрана по одному пересказу брокера."""
    from client_state_report import format_summary

    base = {
        "funnel": "sellers", "funnel_label": "Продавцы", "total": 10,
        "analyzed": 8, "cost_rub": 3.0, "cost_rub_per_card": 0.3,
    }
    blind = format_summary({**base, "cards_with_transcript": 0,
                            "transcripts_pending": 4})
    assert "🎧 Разговор читается у 0 из 10 карточек" in blind
    assert "ещё 4 расшифровок не готово" in blind

    seeing = format_summary({**base, "cards_with_transcript": 7,
                             "transcripts_pending": 0})
    assert "🎧 Разговор читается у 7 из 10 карточек" in seeing
    assert "не готово" not in seeing


def test_a_failed_fetch_is_not_reported_as_a_bitrix_delay():
    from client_state_report import format_summary

    text = format_summary({
        "funnel": "buyers", "funnel_label": "Покупатели", "total": 10,
        "analyzed": 8, "cost_rub": 1.0, "cost_rub_per_card": 0.1,
        "cards_with_transcript": 2, "transcripts_pending": 10,
        "transcripts_failed": 3,
    })
    assert "ещё 10 расшифровок не готово, 3 не загрузилось)" in text


def test_the_marker_shows_on_the_card_and_in_the_short_list():
    from client_state_report import format_card, format_sections

    res = _res(30, temperature="warm")
    res["state"]["no_outgoing_call"] = True
    res["state"]["work_evidence"] = {"proven": False, "reason": "comment"}
    assert "📵 Работа описана комментарием" in format_card(res, "х", WEBHOOK)

    fine = _res(31, temperature="warm")
    fine["state"]["no_outgoing_call"] = True
    fine["state"]["work_evidence"] = {"proven": True, "reason": "comment"}
    body = format_sections([fine], {31: "х"}, WEBHOOK)
    assert "✅ В РАБОТЕ" in body
    assert "📵 без исходящего звонка" in body


def test_the_summary_counts_cards_without_an_outgoing_call():
    from client_state_report import format_summary

    text = format_summary({
        "funnel": "buyers", "funnel_label": "Покупатели", "total": 10,
        "analyzed": 8, "cost_rub": 1.0, "cost_rub_per_card": 0.1,
        "cards_without_outgoing_call": 6,
    })
    assert "нет исходящего звонка): 6" in text


def test_a_multiline_title_is_flattened():
    """#16422 названа целым объявлением с Циан: три строки и пустая между."""
    from client_state_report import card_title

    raw = (
        "диспозл excel Гагаринский пер.\n"
        "Продаётся 3-комнатная квартира за 250 000 000 руб., 170 м.кв.\n\n"
        "Гагаринский пер., 24/7С2, Москва м. Смоленская"
    )
    flat = card_title(raw)
    assert "\n" not in flat
    assert flat.startswith("диспозл excel Гагаринский пер. Продаётся")
    assert flat.endswith("…")
    assert len(flat) <= 91


def test_a_normal_title_is_left_alone():
    from client_state_report import card_title

    assert card_title("ЖК «Hide»") == "ЖК «Hide»"
    assert card_title(None) == ""


def test_the_card_keeps_the_link_on_the_second_line():
    """Ссылка уезжала на четвёртую строку, и карточка переставала читаться."""
    from client_state_report import format_card

    card = format_card(_res(40), "первая\nвторая\nтретья", WEBHOOK)
    assert card.split("\n")[1].startswith("[URL]")


def test_abandoned_cards_get_their_own_section_worst_first():
    """Сто дней тишины не должны стоять в одном списке с тремя."""
    from broker_work import GAP_ABANDONED
    from client_state_report import format_sections, split_sections

    def _card(deal_id: int, quiet: float) -> dict:
        res = _res(deal_id, temperature="warm")
        res["state"]["work_evidence"] = {
            "proven": False, "reason": GAP_ABANDONED,
            "window_days": 3, "days_quiet": quiet, "abandoned_days": quiet,
        }
        return res

    rows = [_card(1, 41.0), _card(2, 107.0)]
    losing, abandoned, neglect, _rem, _wait, fine = split_sections(rows)
    assert [r["deal_id"] for r in abandoned] == [2, 1]
    assert neglect == [] and fine == [] and losing == []

    body = format_sections(rows, {1: "х", 2: "у"}, WEBHOOK)
    assert "🕸 БРОШЕНЫ — 2" in body
    assert body.index("🕸 БРОШЕНЫ") < body.index("🔧 НЕДОРАБОТКА")
    assert "🕸 Карточка брошена (ни звонка, ни комментария 107 дн.)" in body
    assert "Работа не подтверждена" not in body


def test_the_abandoned_section_is_hidden_when_empty():
    from client_state_report import format_sections

    body = format_sections([_res(3, temperature="warm")], {3: "х"}, WEBHOOK)
    assert "БРОШЕНЫ" not in body
