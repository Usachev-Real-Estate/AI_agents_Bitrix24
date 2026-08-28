"""Совет, который нельзя выполнить, не совет.

#12290 (прогон 28.08 12:47):

    Ситуация: клиент не выходит на связь (не в сети, сбрасывает вызовы,
              не отвечает)
    ➡️ Запланировать дело: согласовать с клиентом следующий шаг и срок

Согласовать не с кем, и карточка сама об этом говорит двумя строками
выше. Брокер такой совет не выполнит и перестанет читать остальные.

Что он может в одиночку: назначить следующую попытку и записать
прошлые — заодно закрывается дыра, из-за которой «клиент не отвечает»
ничем не подтверждено.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import next_action  # noqa: E402
from client_state import apply_derived_verdict  # noqa: E402
from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 28, 12, 47, tzinfo=timezone.utc)
AGREE = "согласовать с клиентом следующий шаг и срок"
TRY_AGAIN = "поставить дело на следующую попытку"


def _state(silent: bool) -> dict:
    return {
        "next_step": {"what": "unknown", "when": "unknown", "who": "unknown"},
        "work_evidence": {
            "proven": False, "reason": "no_trace_in_window", "window_days": 2,
        },
        "counterparty_silent": silent,
    }


def test_a_silent_client_is_not_asked_to_agree():
    advice = next_action(_state(True), [], NOW)
    assert AGREE not in advice
    assert TRY_AGAIN in advice
    assert "сколько раз уже пробовали" in advice


def test_a_reachable_client_keeps_the_old_advice():
    assert next_action(_state(False), [], NOW).endswith(AGREE)


def test_an_old_cached_state_without_the_key_keeps_the_old_advice():
    state = _state(False)
    del state["counterparty_silent"]
    assert next_action(state, [], NOW).endswith(AGREE)


def test_a_named_step_is_untouched_by_the_silence():
    """Шаг назван — совет про него, а не про попытки дозвона."""
    state = _state(True)
    state["next_step"] = {
        "what": "Показ объекта", "when": "2026-09-02", "who": "broker",
    }
    advice = next_action(state, [], NOW)
    assert TRY_AGAIN not in advice
    assert "Показ объекта" in advice


# ── откуда берётся флаг ────────────────────────────────────────────────
def _derived(signals: dict, profile) -> dict:
    state = {
        "signals": signals, "recoverable": True, "stage_facts": {},
        "next_step": {"what": "unknown", "when": "unknown", "who": "unknown"},
        "missing": [], "confidence": 0.5,
    }
    record = {"ID": 12290, "STAGE_ID": "C18:NEW", "DATE_CREATE": "2026-08-01"}
    apply_derived_verdict(state, record, profile, {}, [], None)
    return state


def test_a_buyer_who_does_not_answer_sets_the_flag():
    state = _derived({"client_responsive": False}, BUYER_PROFILE)
    assert state["counterparty_silent"] is True


def test_an_owner_who_does_not_answer_sets_the_flag():
    """Ключ у воронок разный — ответ совету нужен один."""
    state = _derived({"owner_responsive": False}, SELLER_PROFILE)
    assert state["counterparty_silent"] is True


def test_silence_must_be_proven_not_assumed():
    """Молчание модели про связь не делает клиента молчащим."""
    assert _derived({}, BUYER_PROFILE)["counterparty_silent"] is False
    assert _derived({}, SELLER_PROFILE)["counterparty_silent"] is False


def test_a_responsive_buyer_is_not_silent():
    state = _derived({"client_responsive": True}, BUYER_PROFILE)
    assert state["counterparty_silent"] is False
