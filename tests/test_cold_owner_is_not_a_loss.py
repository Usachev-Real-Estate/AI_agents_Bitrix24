"""Холодный собственник — не потерянный собственник.

#15594 (прогон 28.08 10:41): карточка суток от роду, брокер написал
собственнику и ждёт ответ, дело стоит на сегодня — и она в разделе
«🚨 ТЕРЯЕМ КЛИЕНТА» с вердиктом «рано судить». Причина: модель поставила
«холодный — собственник не выходит на связь», а холод заводил в тревожный
раздел безусловно.

У покупателя «остыл» — событие: клиент был в разговоре и вышел из него.
У продавца из холодной базы «не выходит на связь» — обычное начало: контакт
взят с Циана, собственник нас не звал. Решение агентства: собственник
холодный, но не потерянный. Ярлык остаётся, уходит только тревога.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from client_state_report import split_sections  # noqa: E402
from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE  # noqa: E402


def _card(deal_id: int, *, cold_is_a_loss: bool, verdict: str = "too_early") -> dict:
    return {
        "deal_id": deal_id,
        "skipped": False,
        "state": {
            "temperature": "cold",
            "temperature_reason": "собственник не выходит на связь",
            "verdict": verdict,
            "cold_is_a_loss": cold_is_a_loss,
            "work_evidence": {
                "proven": True, "reason": "window_not_started", "window_days": 1,
            },
        },
    }


def test_the_profiles_carry_the_decision():
    assert BUYER_PROFILE.cold_means_losing is True
    assert SELLER_PROFILE.cold_means_losing is False


def test_a_cold_owner_does_not_raise_the_alarm():
    losing, _ab, _n, _rem, waiting, _fine = split_sections(
        [_card(15594, cold_is_a_loss=False)],
    )
    assert losing == []
    assert [r["deal_id"] for r in waiting] == [15594]


def test_a_cold_buyer_still_does():
    losing, _ab, _n, _rem, _w, _fine = split_sections(
        [_card(13512, cold_is_a_loss=True)],
    )
    assert [r["deal_id"] for r in losing] == [13512]


def test_an_old_cached_state_without_the_key_keeps_the_alarm():
    """Ключа нет — ведём себя как раньше, а не тише."""
    card = _card(13512, cold_is_a_loss=True)
    del card["state"]["cold_is_a_loss"]
    losing, _ab, _n, _rem, _w, _fine = split_sections([card])
    assert [r["deal_id"] for r in losing] == [13512]


def test_a_cold_owner_whose_work_is_unproven_is_still_a_shortfall():
    """Тревога снята — претензия к работе нет."""
    card = _card(15594, cold_is_a_loss=False, verdict="poor")
    card["state"]["work_evidence"] = {
        "proven": False, "reason": "no_trace_in_window", "window_days": 1,
    }
    losing, _ab, neglected, _rem, _w, _fine = split_sections([card])
    assert losing == []
    assert [r["deal_id"] for r in neglected] == [15594]
