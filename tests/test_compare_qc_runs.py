"""Сравнение прогонов QC-агента: коридор шума и выход за него."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load():
    path = _ROOT / "scripts" / "compare_qc_runs.py"
    spec = importlib.util.spec_from_file_location("compare_qc_runs", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _card(deal_id: int, **over: Any) -> dict[str, Any]:
    card = {
        "deal_id": deal_id, "verdict": "poor", "temperature": "warm",
        "work_reason": "call", "recoverable": True, "counterparty": "client",
        "facts_present": 5, "facts_needed": 7,
    }
    card.update(over)
    return card


def _run(cards: list[dict[str, Any]], **funnel: Any) -> dict[str, Any]:
    base = {
        "cost_rub": 10.0, "llm_calls": len(cards), "evidence_dropped": 0,
        "errors": 0, "unrecoverable": 0, "contradictions_found": 0,
        "contradictions_dropped": 0, "agent_cards": 0,
        "usage": {"input_tokens": 100, "output_tokens": 200,
                  "reasoning_tokens": 120},
    }
    base.update(funnel)
    base["cards"] = cards
    return {"funnels": [base]}


def test_only_common_cards_are_compared():
    """Выборка случайная и между прогонами может разойтись."""
    mod = _load()
    a = mod.cards_by_id(_run([_card(1), _card(2)]))
    b = mod.cards_by_id(_run([_card(2), _card(3)]))
    assert sorted(set(a) & set(b)) == [2]


def test_cards_of_both_funnels_land_in_one_table():
    mod = _load()
    run = {"funnels": [
        {"cards": [_card(1)]},
        {"cards": [_card(2)]},
    ]}
    assert sorted(mod.cards_by_id(run)) == [1, 2]


def test_identical_runs_disagree_on_nothing():
    mod = _load()
    cards = [_card(1), _card(2)]
    a = mod.cards_by_id(_run(cards))
    b = mod.cards_by_id(_run(cards))
    diff = mod.disagreements(a, b, [1, 2])
    assert all(not ids for ids in diff.values())


def test_a_changed_verdict_is_reported_by_deal_id():
    mod = _load()
    a = mod.cards_by_id(_run([_card(1), _card(2)]))
    b = mod.cards_by_id(_run([_card(1), _card(2, verdict="tolerable")]))
    diff = mod.disagreements(a, b, [1, 2])
    assert diff["verdict"] == [2]
    assert diff["temperature"] == []


def test_totals_sum_both_funnels():
    mod = _load()
    run = {"funnels": [
        _run([_card(1)], cost_rub=3.0, evidence_dropped=2)["funnels"][0],
        _run([_card(2)], cost_rub=4.0, evidence_dropped=1)["funnels"][0],
    ]}
    acc = mod.totals(run)
    assert acc["cost_rub"] == 7.0
    assert acc["evidence_dropped"] == 3.0


def test_facts_share_is_counted_over_the_common_cards():
    mod = _load()
    cards = mod.cards_by_id(_run([
        _card(1, facts_present=7, facts_needed=7),
        _card(2, facts_present=0, facts_needed=3),
    ]))
    assert mod.facts_share(cards, [1, 2]) == 70.0
    assert mod.facts_share(cards, [1]) == 100.0


def test_a_stage_without_required_facts_does_not_divide_by_zero():
    mod = _load()
    cards = mod.cards_by_id(_run([_card(1, facts_present=0, facts_needed=0)]))
    assert mod.facts_share(cards, [1]) == 0.0
