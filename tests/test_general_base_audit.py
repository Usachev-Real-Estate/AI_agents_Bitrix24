"""Tests for Общая база 2-day plan rule (general_base_no_plan)."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools import (  # noqa: E402
    DEAL_MOVE_RULES_BUYERS,
    DEAL_MOVE_RULES_SELLERS,
    GENERAL_BASE_CATEGORY_ID,
    _general_base_moved_at_from_history,
    _has_action_plan_text,
    _is_general_base_violation,
    _is_seller_violation,
    check_general_base_violations,
)

CURRENT = "2026-08-16T12:00:00+03:00"


def _deal(**overrides):
    base = {
        "deal_id": 16001,
        "title": "GB deal",
        "stage_id": "C26:NEW",
        "stage_name": "Продавцы",
        "assigned_by_id": 1,
        "date_create": "2026-06-01T10:00:00+03:00",
        "category_id": GENERAL_BASE_CATEGORY_ID,
        "gb_moved_at": "2026-08-14T12:00:00+03:00",
        "timeline": [],
        "deal_activities": [],
        "open_activities": [],
    }
    base.update(overrides)
    return base


def test_within_two_days_no_violation():
    deal = _deal(
        gb_moved_at="2026-08-14T12:00:00+03:00",
        timeline=[],
    )
    assert check_general_base_violations([deal], CURRENT) == []


def test_exactly_two_days_no_violation():
    deal = _deal(gb_moved_at="2026-08-14T12:00:00+03:00")
    assert check_general_base_violations(
        [deal], "2026-08-16T12:00:00+03:00",
    ) == []


def test_after_two_days_no_plan_is_violation():
    deal = _deal(gb_moved_at="2026-08-14T11:59:00+03:00")
    violations = check_general_base_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "general_base_no_plan"
    assert violations[0]["details"]["funnel"] == "general_base"
    assert "более 2 дней" in violations[0]["reason"]
    assert "планом дальнейших действий" in violations[0]["reason"]


def test_missing_gb_moved_at_skipped():
    deal = _deal()
    deal.pop("gb_moved_at")
    assert check_general_base_violations([deal], CURRENT) == []


def test_live_activity_clears_violation():
    deal = _deal(
        gb_moved_at="2026-08-10T12:00:00+03:00",
        deal_activities=[
            {
                "COMPLETED": "N",
                "DEADLINE": "2026-08-20T12:00:00+03:00",
                "SUBJECT": "Показ объекта",
            },
        ],
    )
    assert check_general_base_violations([deal], CURRENT) == []


def test_overdue_activity_does_not_clear():
    deal = _deal(
        gb_moved_at="2026-08-10T12:00:00+03:00",
        deal_activities=[
            {
                "COMPLETED": "N",
                "DEADLINE": "2026-08-15T12:00:00+03:00",
                "SUBJECT": "Связаться с клиентом",
            },
        ],
    )
    violations = check_general_base_violations([deal], CURRENT)
    assert len(violations) == 1


def test_action_plan_comment_after_move_clears():
    deal = _deal(
        gb_moved_at="2026-08-10T12:00:00+03:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Договорились о встрече 20 августа, подготовка КП",
                "created": "2026-08-15T10:00:00+03:00",
            },
        ],
    )
    assert check_general_base_violations([deal], CURRENT) == []


def test_generic_contact_comment_does_not_clear():
    deal = _deal(
        gb_moved_at="2026-08-10T12:00:00+03:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Связаться с клиентом",
                "created": "2026-08-15T10:00:00+03:00",
            },
        ],
    )
    violations = check_general_base_violations([deal], CURRENT)
    assert len(violations) == 1


def test_plan_comment_before_move_does_not_clear():
    deal = _deal(
        gb_moved_at="2026-08-14T12:00:00+03:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Назначили показ и подготовим подборку",
                "created": "2026-08-13T10:00:00+03:00",
            },
        ],
    )
    violations = check_general_base_violations(
        [deal], "2026-08-17T12:00:00+03:00",
    )
    assert len(violations) == 1


def test_buyer_funnel_deal_ignored():
    deal = _deal(
        category_id=18,
        stage_id="C18:NEW",
        gb_moved_at="2026-08-10T12:00:00+03:00",
    )
    assert check_general_base_violations([deal], CURRENT) == []


def test_has_action_plan_text():
    assert _has_action_plan_text("Назначили встречу, подготовим документы")
    assert _has_action_plan_text("Выезд на оценку, сделаем фото")
    assert not _has_action_plan_text("Связаться с клиентом")
    assert not _has_action_plan_text("позвонить")
    assert not _has_action_plan_text("Клиент отказался от встречи, не готов")


def test_moved_at_is_first_c26_after_last_non_gb():
    rows = [
        {"ID": 1, "OWNER_ID": 10, "STAGE_ID": "NEW",
         "CREATED_TIME": "2026-08-01T10:00:00+03:00"},
        {"ID": 2, "OWNER_ID": 10, "STAGE_ID": "C26:NEW",
         "CREATED_TIME": "2026-08-14T11:00:00+03:00"},
        {"ID": 3, "OWNER_ID": 10, "STAGE_ID": "C26:EXECUTING",
         "CREATED_TIME": "2026-08-14T15:00:00+03:00"},
    ]
    moved = _general_base_moved_at_from_history(rows, 10)
    assert moved is not None
    assert moved.day == 14
    assert moved.hour == 11


def test_moved_at_uses_latest_transfer_after_restore():
    rows = [
        {"ID": 1, "OWNER_ID": 10, "STAGE_ID": "NEW",
         "CREATED_TIME": "2026-07-01T10:00:00+03:00"},
        {"ID": 2, "OWNER_ID": 10, "STAGE_ID": "C26:NEW",
         "CREATED_TIME": "2026-08-10T10:00:00+03:00"},
        {"ID": 3, "OWNER_ID": 10, "STAGE_ID": "NEW",
         "CREATED_TIME": "2026-08-11T10:00:00+03:00"},
        {"ID": 4, "OWNER_ID": 10, "STAGE_ID": "C26:NEW",
         "CREATED_TIME": "2026-08-14T12:00:00+03:00"},
    ]
    moved = _general_base_moved_at_from_history(rows, 10)
    assert moved is not None
    assert moved.day == 14


def test_moved_at_fallback_date_create_when_no_history():
    created = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    assert _general_base_moved_at_from_history([], 10, created) == created


def test_is_general_base_violation_not_seller():
    v = {"rule": "general_base_no_plan", "details": {"funnel": "general_base"}}
    assert _is_general_base_violation(v)
    assert not _is_seller_violation(v)
    assert not _is_general_base_violation({"rule": "buyer_stage_2"})


def test_general_base_rule_does_not_auto_move():
    assert "general_base_no_plan" not in DEAL_MOVE_RULES_SELLERS
    assert "general_base_no_plan" not in DEAL_MOVE_RULES_BUYERS
