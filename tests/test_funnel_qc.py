"""Tests for per-funnel QC rules: client temperature."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from client_state import compute_temperature  # noqa: E402
from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE, profile_for  # noqa: E402


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


def test_seller_price_never_discussed_is_not_cold():
    """Не обсуждённая цена — пробел, а не остывание.

    #17002: карточке пять часов, собственник подтвердил, что продажа
    актуальна, — и она уехала в «теряем клиента». Одинаковый ярлык на пятом
    часу жизни и на тридцатом дне обесценивает сам ярлык.
    """
    signals = dict(SELLER_HOT, price_discussed=False)
    assert compute_temperature(signals, profile=SELLER_PROFILE)[0] != "cold"


def test_seller_who_stopped_answering_is_still_cold():
    """Молчание собственника — событие, и оно остаётся холодом."""
    signals = dict(SELLER_HOT, owner_responsive=False)
    level, why = compute_temperature(signals, profile=SELLER_PROFILE)
    assert level == "cold" and "не выходит на связь" in why


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
    assert level != "hot"


def test_profile_lookup():
    assert profile_for("buyers") is BUYER_PROFILE
    assert profile_for("SELLERS") is SELLER_PROFILE
    with pytest.raises(ValueError):
        profile_for("прочее")


# ── Агент не остывает по горизонту ─────────────────────────────────────
def test_an_agents_horizon_does_not_make_the_card_cold():
    """#11954: «Лариса (Клекова) агент» уехала в «теряем клиента».

    Горизонт там принадлежит клиентам агента, а не самому агенту.
    """
    signals = dict(BUYER_HOT, timeline_horizon="более 3 месяцев")
    client, _ = compute_temperature(signals, profile=BUYER_PROFILE)
    agent, _ = compute_temperature(
        signals, profile=BUYER_PROFILE, counterparty="agent",
    )
    assert client == "cold"
    assert agent != "cold"


def test_a_silent_agent_is_still_cold():
    """Не выходит на связь — контакт теряем, кто бы он ни был."""
    signals = dict(BUYER_HOT, client_responsive=False)
    level, why = compute_temperature(
        signals, profile=BUYER_PROFILE, counterparty="agent",
    )
    assert level == "cold" and "не выходит на связь" in why
