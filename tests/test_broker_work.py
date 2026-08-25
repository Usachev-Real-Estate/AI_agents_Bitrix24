"""Доказательства работы брокера: звонок → комментарий → скриншот."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from broker_work import (  # noqa: E402
    GAP_CLAIMED_MESSAGE,
    GAP_EMPTY_COMMENT,
    GAP_NO_TRACE,
    GAP_OUT_OF_WINDOW,
    PROVEN_BY_CALL,
    PROVEN_BY_COMMENT,
    PROVEN_BY_SCREENSHOT,
    assess_broker_work,
    work_window_days,
)
from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE  # noqa: E402

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def _comment(hours: float, text: str = "поговорили", files: bool = False) -> dict:
    return {
        "kind": "comment", "created": _ago(hours), "text": text,
        "has_files": files,
    }


def _call(hours: float, completed: str = "Y") -> dict:
    return {
        "kind": "activity", "created": _ago(hours), "type_id": 2,
        "completed": completed, "text": "Звонок",
    }


def _assess(events: list[dict], **over: Any) -> dict:
    params: dict[str, Any] = {
        "profile": BUYER_PROFILE,
        "stage_id": "C18:UC_DVW1P9",   # Повторный показ, окно 3 дня
        "hours_on_stage": 1000.0,
        "claims_messaged": False,
        "comment_informative": True,
        "now": NOW,
    }
    params.update(over)
    return assess_broker_work(events, **params)


# ── Звонок — сильнейшее доказательство ─────────────────────────────────
def test_a_completed_call_proves_the_work():
    result = _assess([_call(hours=10)])
    assert result["proven"] is True
    assert result["reason"] == PROVEN_BY_CALL


def test_a_transcript_alone_proves_the_work():
    """Расшифровка есть — значит разговор был, что бы ни лежало в делах."""
    result = _assess([{"kind": "transcript", "created": _ago(10), "text": "..."}])
    assert result["reason"] == PROVEN_BY_CALL


def test_a_planned_call_is_not_a_call():
    """«Позвонить клиенту» в делах — это план, а не работа."""
    result = _assess([_call(hours=10, completed="N")])
    assert result["proven"] is False
    assert result["reason"] == GAP_NO_TRACE


# ── Нет звонка → нужен развёрнутый комментарий ──────────────────────────
def test_an_informative_comment_proves_the_work_without_a_call():
    result = _assess([_comment(hours=10)], comment_informative=True)
    assert result["proven"] is True
    assert result["reason"] == PROVEN_BY_COMMENT


def test_an_empty_comment_does_not_prove_the_work():
    result = _assess([_comment(hours=10, text="ок")], comment_informative=False)
    assert result["proven"] is False
    assert result["reason"] == GAP_EMPTY_COMMENT


# ── «Написал клиенту» → нужен скриншот ─────────────────────────────────
def test_claiming_a_message_without_a_screenshot_is_not_proof():
    """Иначе правило вырождается: «написал» пишется за две секунды."""
    result = _assess(
        [_comment(hours=10, text="написал клиенту в вотсап")],
        claims_messaged=True,
    )
    assert result["proven"] is False
    assert result["reason"] == GAP_CLAIMED_MESSAGE


def test_a_screenshot_turns_the_claim_into_proof():
    result = _assess(
        [_comment(hours=10, text="написал клиенту", files=True)],
        claims_messaged=True,
    )
    assert result["proven"] is True
    assert result["reason"] == PROVEN_BY_SCREENSHOT


def test_a_call_outranks_a_missing_screenshot():
    """Позвонил и написал — звонка достаточно, скриншот не нужен."""
    result = _assess(
        [_call(hours=5), _comment(hours=10, text="ещё написал")],
        claims_messaged=True,
    )
    assert result["reason"] == PROVEN_BY_CALL


def test_a_screenshot_outside_the_window_does_not_count():
    """Скриншот месячной давности не подтверждает работу на этой неделе."""
    result = _assess(
        [_comment(hours=24 * 30, text="написал", files=True)],
        claims_messaged=True,
    )
    assert result["proven"] is False
    assert result["reason"] == GAP_NO_TRACE


# ── Тишина ─────────────────────────────────────────────────────────────
def test_no_trace_at_all_is_the_worst_case():
    result = _assess([])
    assert result["proven"] is False
    assert result["reason"] == GAP_NO_TRACE
    assert result["days_quiet"] is None


def test_days_quiet_counts_from_the_last_trace():
    result = _assess([_comment(hours=24 * 12)])
    assert result["reason"] == GAP_NO_TRACE
    assert result["days_quiet"] == pytest.approx(12.0, abs=0.01)


# ── Окно не наступило ──────────────────────────────────────────────────
def test_a_card_younger_than_its_window_is_not_blamed():
    """Спрашивать за трёхдневное окно с карточки, которой сутки, нельзя."""
    result = _assess([], hours_on_stage=24.0)
    assert result["proven"] is True
    assert result["reason"] == GAP_OUT_OF_WINDOW


def test_unknown_stage_age_does_not_grant_an_exemption():
    """Неизвестен возраст — судим как обычно, иначе окно станет лазейкой."""
    result = _assess([], hours_on_stage=None)
    assert result["proven"] is False


# ── Окна по этапам взяты из каденса основного аудита ───────────────────
def test_window_matches_the_main_audit_cadence():
    from tools import BUYERS_STAGE_AUDIT_RULE, SELLERS_STAGE_CADENCE

    for stage, days in BUYERS_STAGE_AUDIT_RULE.items():
        if stage in BUYER_PROFILE.work_window_days:
            assert work_window_days(BUYER_PROFILE, stage) == days, stage

    for stage, delta in SELLERS_STAGE_CADENCE.items():
        if stage in SELLER_PROFILE.work_window_days:
            assert work_window_days(SELLER_PROFILE, stage) == delta.days, stage


def test_unknown_stage_falls_back_to_the_default_window():
    assert work_window_days(BUYER_PROFILE, "C18:SOMETHING_NEW") == 7
