"""Tests for deterministic lead audit rules."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools import (
    LEAD_STATUS_CONVERTED,
    LEAD_STATUS_JUNK,
    LEAD_STATUS_SHARED,
    check_lead_rule1_violations,
    _lead_needs_llm_check,
)


def test_rule1_violation_when_new_over_2_hours():
    current_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    leads = [
        {
            "lead_id": 1001,
            "status_id": "NEW",
            "date_create": "2026-06-15T09:00:00+00:00",  # 3 hours ago
            "assigned_by_id": 10,
            "timeline": []
        }
    ]
    violations = check_lead_rule1_violations(leads, current_time)
    assert len(violations) == 1
    assert violations[0]["rule"] == "lead_rule_1"


def test_rule1_no_violation_when_under_2_hours():
    current_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    leads = [
        {
            "lead_id": 1002,
            "status_id": "NEW",
            "date_create": "2026-06-15T11:00:00+00:00",  # 1 hour ago
            "assigned_by_id": 10,
            "timeline": []
        }
    ]
    violations = check_lead_rule1_violations(leads, current_time)
    assert len(violations) == 0


def test_rule1_no_violation_when_has_broker_comment():
    current_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    leads = [
        {
            "lead_id": 1003,
            "status_id": "NEW",
            "date_create": "2026-06-15T08:00:00+00:00",  # 4 hours ago
            "assigned_by_id": 10,
            "timeline": [
                {"author_id": 10, "comment": "Взял в работу"}
            ]
        }
    ]
    violations = check_lead_rule1_violations(leads, current_time)
    assert len(violations) == 0


def test_lead_needs_llm_check_true_for_spam():
    lead = {"status_id": "SPAM", "timeline": [], "comments_field": ""}
    assert _lead_needs_llm_check(lead) is True


def test_lead_needs_llm_check_false_for_new():
    lead = {"status_id": "NEW", "timeline": [], "comments_field": ""}
    assert _lead_needs_llm_check(lead) is False


def test_lead_needs_llm_check_true_for_junk():
    lead = {"status_id": LEAD_STATUS_JUNK, "timeline": [], "comments_field": ""}
    assert _lead_needs_llm_check(lead) is True


def test_lead_needs_llm_check_false_for_converted():
    lead = {"status_id": LEAD_STATUS_CONVERTED, "timeline": [], "comments_field": ""}
    assert _lead_needs_llm_check(lead) is False


def test_lead_needs_llm_check_false_for_shared_pool():
    lead = {"status_id": LEAD_STATUS_SHARED, "timeline": [], "comments_field": ""}
    assert _lead_needs_llm_check(lead) is False


def test_rule1_no_violation_for_shared_pool():
    current_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    leads = [
        {
            "lead_id": 1004,
            "status_id": LEAD_STATUS_SHARED,
            "date_create": "2026-06-15T08:00:00+00:00",
            "assigned_by_id": 10,
            "timeline": [],
        }
    ]
    violations = check_lead_rule1_violations(leads, current_time)
    assert len(violations) == 0
