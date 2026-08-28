"""Дата шага — одна на карточку, и цифра под вердиктом должна сходиться.

Прогон 28.08 10:41, два разных механизма одной беды — отчёт спорит сам с
собой на той же карточке.

#14362: «🔥 горячий — есть согласованный шаг с датой», строкой ниже «Шаг:
… (не указано, брокер)», а советом — «Запланировать дело с датой». Дату
модель заполняет дважды: signals.next_step_date смотрит температура,
next_step.when печатает отчёт. Поля независимы, и ответы разошлись.

#15594: «этап моложе отсрочки (24 ч < 24 ч)» — неравенство, которое само
себя опровергает: карточке было 23.6 часа, округление съело разницу, на
которой стоит вердикт.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state import (  # noqa: E402
    compute_completeness_verdict,
    compute_temperature,
    grace_period_for,
    reconcile_step_date,
)
from client_state_report import format_next_step  # noqa: E402
from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE  # noqa: E402

STAGE = "NEW"


def _state(signal_date: str, when: str) -> dict:
    return {
        "signals": {"next_step_date": signal_date},
        "next_step": {"what": "показ", "when": when, "who": "broker"},
    }


# ── одна дата на карточку ──────────────────────────────────────────────
def test_the_date_the_temperature_used_becomes_visible():
    """#14362: температура опиралась на дату, которой в отчёте не было."""
    state = _state("2026-08-31", "unknown")
    assert reconcile_step_date(state) == "2026-08-31"
    assert state["next_step"]["when"] == "2026-08-31"
    assert "2026-08-31" in format_next_step(state["next_step"])


def test_a_date_named_only_in_the_step_reaches_the_temperature():
    state = _state("unknown", "2026-09-02")
    assert reconcile_step_date(state) == "2026-09-02"
    assert state["signals"]["next_step_date"] == "2026-09-02"


def test_a_phrase_without_a_date_stays_a_phrase():
    """«в пятницу» — это и есть «даты нет»: совет как раз просит уточнить."""
    state = _state("unknown", "в пятницу")
    assert reconcile_step_date(state) == ""
    assert state["next_step"]["when"] == "в пятницу"
    assert state["signals"]["next_step_date"] == "unknown"


def test_two_named_dates_are_left_alone():
    state = _state("2026-08-31", "2026-09-02")
    reconcile_step_date(state)
    assert state["signals"]["next_step_date"] == "2026-08-31"
    assert state["next_step"]["when"] == "2026-09-02"


def test_a_date_inside_a_phrase_is_found():
    state = _state("unknown", "показ 02.09.2026 в 11:00")
    assert reconcile_step_date(state) == "2026-09-02"


def test_missing_halves_do_not_crash():
    assert reconcile_step_date({}) == ""
    assert reconcile_step_date({"signals": {"next_step_date": "2026-08-31"}}) == (
        "2026-08-31"
    )
    assert reconcile_step_date({"next_step": {"when": "unknown"}}) == ""


def test_hot_needs_a_date_the_report_shows():
    """После сведения «горячий» и напечатанный шаг говорят об одном."""
    state = _state("2026-08-31", "unknown")
    state["signals"].update({
        "client_responsive": True, "next_step_agreed": True,
        "budget_named": True, "timeline_named": True,
    })
    reconcile_step_date(state)
    level, why = compute_temperature(state["signals"], profile=BUYER_PROFILE)
    assert level == "hot"
    assert "шаг с датой" in why
    assert "2026-08-31" in format_next_step(state["next_step"])


# ── цифра под вердиктом сходится ───────────────────────────────────────
def _grace_reason(hours: float) -> str:
    _verdict, why = compute_completeness_verdict(
        STAGE, {"recoverable": True, "stage_facts": {}}, SELLER_PROFILE,
        hours_on_stage=hours,
    )
    return why


def test_the_inequality_at_the_boundary_does_not_refute_itself():
    grace = grace_period_for(SELLER_PROFILE, STAGE)
    why = _grace_reason(grace - 0.4)
    assert f"{grace} ч < {grace} ч" not in why
    assert f"{grace - 0.4:.1f} ч < {grace} ч" in why


def test_a_hair_below_the_boundary_still_reads_as_less():
    grace = grace_period_for(SELLER_PROFILE, STAGE)
    why = _grace_reason(grace - 0.05)
    shown = float(why.split("(")[1].split(" ч")[0])
    assert shown < grace


def test_far_from_the_boundary_stays_a_whole_number():
    grace = grace_period_for(SELLER_PROFILE, STAGE)
    assert f"{grace // 2:.0f} ч < {grace} ч" in _grace_reason(grace / 2)
