"""Tests for per-funnel QC rules: contradictions and client temperature."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from client_state import (  # noqa: E402
    compute_temperature,
    split_corpora,
    verify_contradictions,
)
from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE, profile_for  # noqa: E402


def _events():
    return [
        {"kind": "comment", "text": "Клиент готов выходить на сделку"},
        {"kind": "transcript", "text": "Мне надо подумать до осени"},
    ]


# ── Сверка источников ──────────────────────────────────────────────────
def test_contradiction_confirmed_by_both_sources():
    card, call = split_corpora(_events())
    rows = [{
        "what": "готовность", "in_card": "готов выходить на сделку",
        "in_call": "надо подумать до осени", "severity": "high",
    }]
    ok, dropped = verify_contradictions(rows, card, call)
    assert len(ok) == 1 and not dropped


def test_invented_quote_is_rejected():
    card, call = split_corpora(_events())
    rows = [{
        "what": "цена", "in_card": "клиент назвал 30 млн",
        "in_call": "я говорил про 20", "severity": "high",
    }]
    ok, dropped = verify_contradictions(rows, card, call)
    assert not ok and len(dropped) == 1


def test_both_quotes_from_the_card_are_rejected():
    """Расхождение имеет смысл только между разными источниками."""
    card, call = split_corpora(_events())
    rows = [{
        "what": "мнимое", "in_card": "Клиент готов выходить",
        "in_call": "готов выходить на сделку", "severity": "low",
    }]
    ok, dropped = verify_contradictions(rows, card, call)
    assert not ok and len(dropped) == 1


def test_corpora_are_split_by_source():
    card, call = split_corpora(_events())
    assert "готов выходить" in card and "готов выходить" not in call
    assert "подумать до осени" in call and "подумать до осени" not in card


# ── Температура покупателя ─────────────────────────────────────────────
BUYER_HOT = {
    "budget_named": True, "timeline_named": True, "timeline_horizon": "до месяца",
    "next_step_agreed": True, "next_step_date": "2026-08-24", "client_responsive": True,
}


def test_buyer_hot_needs_step_budget_and_timeline():
    level, _ = compute_temperature(BUYER_HOT, profile=BUYER_PROFILE)
    assert level == "hot"


@pytest.mark.parametrize("drop", ["budget_named", "timeline_named", "next_step_agreed"])
def test_buyer_without_any_ingredient_is_warm(drop):
    signals = dict(BUYER_HOT, **{drop: False})
    level, why = compute_temperature(signals, profile=BUYER_PROFILE)
    assert level == "warm" and why


def test_buyer_unresponsive_is_cold():
    signals = dict(BUYER_HOT, client_responsive=False)
    assert compute_temperature(signals, profile=BUYER_PROFILE)[0] == "cold"


def test_buyer_far_horizon_is_cold():
    signals = dict(BUYER_HOT, timeline_horizon="более 3 месяцев")
    assert compute_temperature(signals, profile=BUYER_PROFILE)[0] == "cold"


# ── Температура продавца ───────────────────────────────────────────────
SELLER_HOT = {
    "price_named": True, "price_discussed": True,
    "next_step_agreed": True, "next_step_date": "2026-08-24", "owner_responsive": True,
}


def test_seller_hot_needs_step_and_price():
    level, _ = compute_temperature(SELLER_HOT, profile=SELLER_PROFILE)
    assert level == "hot"


def test_motivation_no_longer_decides_the_temperature():
    """Убрано по решению агентства: заинтересованность видно по разговору,
    а не по тому, как её пересказал брокер в комментарии."""
    signals = dict(SELLER_HOT, motivation="просто интерес")
    assert compute_temperature(signals, profile=SELLER_PROFILE)[0] == "hot"


def test_the_word_motivation_is_gone_from_the_seller_rule():
    from funnel_profiles import SELLER_PROMPT, _normalize_seller_signals

    assert "motivation" not in SELLER_PROMPT
    assert "motivation" not in _normalize_seller_signals({}, int)
    _level, why = compute_temperature(
        dict(SELLER_HOT, price_named=False), profile=SELLER_PROFILE,
    )
    assert "мотивац" not in why


def test_seller_price_never_discussed_is_cold():
    signals = dict(SELLER_HOT, price_discussed=False)
    assert compute_temperature(signals, profile=SELLER_PROFILE)[0] == "cold"


def test_seller_without_price_is_warm():
    signals = dict(SELLER_HOT, price_named=False)
    assert compute_temperature(signals, profile=SELLER_PROFILE)[0] == "warm"


# ── Общее ──────────────────────────────────────────────────────────────
def test_unrecoverable_card_has_no_temperature():
    level, why = compute_temperature(BUYER_HOT, recoverable=False, profile=BUYER_PROFILE)
    assert level == "unknown" and "восстановить" in why


def test_buyer_signals_do_not_make_a_seller_hot():
    """Сигналы покупателя не должны случайно удовлетворить правило продавца."""
    level, _ = compute_temperature(BUYER_HOT, profile=SELLER_PROFILE)
    assert level == "cold"


def test_profile_lookup():
    assert profile_for("buyers") is BUYER_PROFILE
    assert profile_for("SELLERS") is SELLER_PROFILE
    with pytest.raises(ValueError):
        profile_for("прочее")
