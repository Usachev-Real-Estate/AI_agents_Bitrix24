"""Tests for seller-funnel deal audit rules and report icons."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools import (  # noqa: E402
    SELLERS_PAID_SOURCE_IDS,
    _is_seller_violation,
    _severity_icon,
    check_seller_deal_violations,
    seller_violation_action,
)


CURRENT = "2026-07-31T12:00:00+00:00"


def _deal(**overrides):
    base = {
        "deal_id": 15000,
        "title": "Seller deal",
        "stage_id": "NEW",
        "stage_name": "Назначение встречи",
        "assigned_by_id": 100,
        "date_create": "2026-07-29T10:00:00+00:00",
        "source_id": "24",
        "category_id": 0,
        "timeline": [],
        "calls": [],
    }
    base.update(overrides)
    return base


def test_paid_source_ids():
    assert SELLERS_PAID_SOURCE_IDS == frozenset({"24", "25", "26"})


def test_meeting_no_outgoing_after_24h():
    deal = _deal(calls=[])
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_meeting_no_outgoing"
    assert violations[0]["severity"] == "high"
    assert violations[0]["details"]["funnel"] == "sellers"
    assert "Назначение встречи" in violations[0]["reason"]
    assert "источник" not in violations[0]["reason"].lower()
    assert "КЦ" not in violations[0]["reason"]


def test_meeting_no_outgoing_skipped_when_comment_exists():
    deal = _deal(
        calls=[],
        timeline=[
            {
                "author_id": 100,
                "comment": "Договорились созвониться завтра",
                "created": "2026-07-30T10:00:00+00:00",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_meeting_grace_under_24h():
    deal = _deal(date_create="2026-07-31T10:00:00+00:00", calls=[])
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_meeting_not_advanced_when_has_outgoing():
    deal = _deal(calls=[{"call_type": "outgoing", "crm_entity_id": 15000}])
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_meeting_not_advanced"


def test_deferred_no_comment():
    deal = _deal(
        stage_id="LOSE",
        stage_name="Отложенная продажа",
        source_id="CALL",
        timeline=[],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_deferred_no_comment"
    assert violations[0]["severity"] == "medium"


def test_deferred_with_broker_comment_ok():
    deal = _deal(
        stage_id="LOSE",
        stage_name="Отложенная продажа",
        source_id="CALL",
        timeline=[
            {
                "author_id": 100,
                "comment": "Свяжемся через неделю",
                "created": "2026-07-30T10:00:00+00:00",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_source_no_outgoing_other_stage():
    deal = _deal(
        stage_id="UC_FADPBF",
        stage_name="Поиск клиента",
        source_id="26",
        calls=[],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_source_no_outgoing"
    assert "Поиск клиента" in violations[0]["reason"]
    assert "источник" not in violations[0]["reason"].lower()


def test_source_no_outgoing_skipped_when_comment_exists():
    deal = _deal(
        stage_id="UC_FADPBF",
        stage_name="Поиск клиента",
        source_id="26",
        calls=[],
        timeline=[
            {
                "author_id": 100,
                "comment": "Клиент просил перезвонить",
                "created": "2026-07-30T12:00:00+00:00",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_non_paid_source_skips_call_rules():
    deal = _deal(source_id="CALL", calls=[])
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_severity_icons_sellers_blue():
    assert _severity_icon("high", seller=True) == "🔵"
    assert _severity_icon("medium", seller=True) == "🟦"
    assert _severity_icon("high", seller=False) == "🔴"


def test_is_seller_violation():
    assert _is_seller_violation({"rule": "seller_meeting_no_outgoing"})
    assert not _is_seller_violation({"rule": "buyer_stage_2"})
    assert _is_seller_violation(
        {"rule": "x", "details": {"funnel": "sellers"}},
    )


def test_seller_violation_action_text():
    action = seller_violation_action({"rule": "seller_meeting_no_outgoing"})
    assert "исходящий звонок с рабочего номера" in action
    assert "перенести сделку" in action
    assert seller_violation_action({"rule": "seller_deferred_no_comment"})
