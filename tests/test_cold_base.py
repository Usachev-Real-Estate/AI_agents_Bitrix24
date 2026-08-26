"""Холодная база: терять некого, пока клиента не было."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state_report import (  # noqa: E402
    format_card,
    format_source_mix,
    set_source_names,
    source_name,
    split_sections,
)

WEBHOOK = "https://b24-po7frr.bitrix24.ru/rest/1/token/"


def _res(deal_id: int, **over: Any) -> dict[str, Any]:
    state = {
        "client_goal": "", "situation": "Выгрузка из реестра, контакта нет",
        "next_step": {"what": "Связаться с клиентом", "when": "unknown",
                      "who": "broker"},
        "risk": "medium", "confidence": 0.2, "recoverable": False,
        "temperature": "unknown", "temperature_reason": "нельзя восстановить",
        "verdict": "poor", "verdict_reason": "нельзя восстановить",
        "missing": [], "contradictions": [],
        "cold_base": True, "source_id": "26",
        "work_evidence": {"proven": False, "reason": "comment_says_nothing",
                          "window_days": 1, "days_quiet": 0.2},
    }
    state.update(over)
    return {"deal_id": deal_id, "skipped": False, "reason": "", "state": state}


# ── Раздел «теряем клиента» ────────────────────────────────────────────
def test_cold_base_never_enters_the_loss_section():
    """Шесть таких карточек забивали раздел, где терять было некого."""
    losing, neglect, _fine = split_sections([_res(16306)])
    assert losing == []
    assert [r["deal_id"] for r in neglect] == [16306]


def test_cold_base_is_still_checked_for_broker_work():
    """Взял в работу — позвони. Это претензия остаётся."""
    _losing, neglect, _fine = split_sections([_res(16310)])
    assert len(neglect) == 1


def test_a_cold_base_card_that_went_cold_is_still_a_loss():
    """Контакт был и клиент отказался — это уже потеря, а не холодная база."""
    losing, _n, _f = split_sections([_res(16254, temperature="cold")])
    assert [r["deal_id"] for r in losing] == [16254]


def test_a_contradiction_on_cold_base_is_still_a_loss():
    lying = _res(16300, contradictions=[{
        "what": "цена", "in_card": "30", "in_call": "20", "severity": "high",
    }])
    losing, _n, _f = split_sections([lying])
    assert [r["deal_id"] for r in losing] == [16300]


def test_a_warm_source_card_is_unaffected():
    """Обычная сделка с пустой карточкой — по-прежнему потеря."""
    normal = _res(15858, cold_base=False, source_id="1")
    losing, _n, _f = split_sections([normal])
    assert [r["deal_id"] for r in losing] == [15858]


# ── Отметка в карточке ─────────────────────────────────────────────────
def test_the_card_says_it_is_cold_base_and_names_the_source():
    card = format_card(_res(16306), "диспозл excel Lucky", WEBHOOK)
    assert "🧊 Холодная база" in card
    assert "Диспозл 5%" in card


def test_a_normal_card_carries_no_cold_base_mark():
    card = format_card(_res(1, cold_base=False), "Покупка", WEBHOOK)
    assert "Холодная база" not in card


# ── Диагностика источников ─────────────────────────────────────────────
def test_source_mix_lets_the_setting_be_verified():
    """Если «Диспозл» в списке есть, а холодной базы 0 — настройка мимо."""
    line = format_source_mix({"26": 5, "1": 3, "25": 2}, cold_base=7)
    assert line.startswith("Источники выборки: ")
    assert "Диспозл 5% 5" in line
    assert "Диспозл 10% 2" in line
    assert "из них холодная база: 7" in line


def test_unknown_source_code_is_shown_as_is():
    assert "SOMETHING 4" in format_source_mix({"SOMETHING": 4}, cold_base=0)


def test_source_mix_omits_the_tail_when_nothing_is_cold():
    assert "холодная база" not in format_source_mix({"1": 3}, cold_base=0)


def test_live_names_from_bitrix_win_over_the_fallback():
    set_source_names({"99": "Выгрузка реестра"})
    assert source_name("99") == "Выгрузка реестра"


def test_the_default_cold_sources_are_the_disposal_ones():
    """Значение по умолчанию должно совпадать с кодами «Диспозл»."""
    from config import get_settings
    from tools import SELLERS_PAID_SOURCE_NAMES

    cold = get_settings().cold_base_sources
    assert cold == {"25", "26"}
    for code in cold:
        assert "Диспозл" in SELLERS_PAID_SOURCE_NAMES[code]


def test_a_deal_without_a_source_is_labelled_not_left_blank():
    line = format_source_mix({"": 2, "1": 1}, cold_base=0)
    assert "без источника 2" in line
