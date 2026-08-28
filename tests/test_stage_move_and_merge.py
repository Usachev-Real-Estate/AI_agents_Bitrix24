"""Переезд на другой этап и дозапрос не должны стирать найденные факты.

Аудит 27.08, тот же класс: «модель об этом не сказала» выдавалось за
«в карточке этого нет».

1. Набор фактов задаётся этапом. Сделка, переехавшая на другой этап без
   единого нового события, отдавалась из кэша — и её судили по требованиям
   нового этапа теми фактами, которых у старого никто не спрашивал.
2. При дозапросе модель видит только новые события. Её молчание о факте,
   найденном месяц назад в старом комментарии, стирало этот факт.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state import compute_content_hash, merge_stage_facts  # noqa: E402
from funnel_profiles import (  # noqa: E402
    BUYER_PROFILE,
    all_facts_for_stage,
)

EVENTS = [{"kind": "comment", "id": 1, "created": "2026-08-20", "text": "поговорили"}]


def _stages_with_different_facts() -> tuple[str, str]:
    """Два этапа воронки покупателей с разным набором спрашиваемых фактов."""
    from funnel_profiles import BUYER_STAGE_REQUIREMENTS

    seen: dict[tuple[str, ...], str] = {}
    for stage in BUYER_STAGE_REQUIREMENTS:
        keys = tuple(sorted(k for k, _n, _r in all_facts_for_stage("buyers", stage)))
        if keys and keys not in seen:
            seen[keys] = stage
    stages = list(seen.values())
    assert len(stages) >= 2, "нужны два этапа с разными наборами фактов"
    return stages[0], stages[1]


def test_moving_between_stages_invalidates_the_cache():
    first, second = _stages_with_different_facts()
    assert compute_content_hash(EVENTS, BUYER_PROFILE, first) != compute_content_hash(
        EVENTS, BUYER_PROFILE, second,
    )


def test_the_same_stage_keeps_the_same_hash():
    first, _ = _stages_with_different_facts()
    assert compute_content_hash(EVENTS, BUYER_PROFILE, first) == compute_content_hash(
        EVENTS, BUYER_PROFILE, first,
    )


def test_the_hash_still_works_without_a_stage():
    assert compute_content_hash(EVENTS, BUYER_PROFILE)
    assert compute_content_hash(EVENTS)


# --- дозапрос ------------------------------------------------------------


def test_a_previously_found_fact_survives_the_model_saying_nothing():
    merged = merge_stage_facts(
        {"budget": {"present": True, "quote": "до 12 млн"}},
        {"district": {"present": True, "quote": "смотрит Химки"}},
    )
    assert merged["budget"]["present"] is True
    assert merged["district"] == {"present": True, "quote": "смотрит Химки"}


def test_a_previously_found_fact_survives_present_false():
    merged = merge_stage_facts(
        {"district": {"present": False, "quote": ""}},
        {"district": {"present": True, "quote": "смотрит Химки"}},
    )
    assert merged["district"] == {"present": True, "quote": "смотрит Химки"}


def test_a_fresh_finding_wins_over_the_old_one():
    merged = merge_stage_facts(
        {"budget": {"present": True, "quote": "до 15 млн"}},
        {"budget": {"present": True, "quote": "до 12 млн"}},
    )
    assert merged["budget"]["quote"] == "до 15 млн"


def test_an_absent_fact_stays_absent():
    merged = merge_stage_facts(
        {"budget": {"present": False, "quote": ""}},
        {"budget": {"present": False, "quote": ""}},
    )
    assert merged["budget"]["present"] is False


def test_a_previous_fact_without_a_quote_is_not_carried_over():
    """Без цитаты факт нечем проверить — тащить его дальше нельзя."""
    merged = merge_stage_facts({}, {"budget": {"present": True, "quote": "  "}})
    assert "budget" not in merged


def test_no_previous_state_changes_nothing():
    merged = merge_stage_facts({"budget": {"present": True, "quote": "до 12 млн"}}, None)
    assert merged["budget"]["present"] is True
