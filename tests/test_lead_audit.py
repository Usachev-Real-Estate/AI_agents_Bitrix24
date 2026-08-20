"""Tests for deterministic lead audit rules."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools import (
    LEAD_STATUS_CONVERTED,
    LEAD_STATUS_JUNK,
    LEAD_STATUS_NECELEVOY,
    LEAD_STATUS_SHARED,
    check_lead_rule1_violations,
    check_lead_rule2_rule3_violations,
    lead_has_rule2_justification,
    lead_has_rule3_justification,
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


def test_lead_needs_llm_check_true_for_spam_without_justification():
    lead = {"status_id": "SPAM", "timeline": [], "comments_field": ""}
    assert _lead_needs_llm_check(lead) is True


def test_lead_needs_llm_check_false_for_new():
    lead = {"status_id": "NEW", "timeline": [], "comments_field": ""}
    assert _lead_needs_llm_check(lead) is False


def test_lead_needs_llm_check_true_for_junk_without_justification():
    lead = {"status_id": LEAD_STATUS_JUNK, "timeline": [], "comments_field": ""}
    assert _lead_needs_llm_check(lead) is True


def test_lead_needs_llm_check_false_for_junk_with_timeline():
    lead = {
        "status_id": LEAD_STATUS_JUNK,
        "timeline": [{"comment": "Спам от МТС."}],
        "comments_field": "",
    }
    assert _lead_needs_llm_check(lead) is False


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


def test_rule2_spam_word_in_timeline_is_justification():
    lead = {
        "lead_id": 2001,
        "status_id": LEAD_STATUS_JUNK,
        "status_name": "Спам",
        "assigned_by_id": 10,
        "timeline": [{"comment": "спам"}],
        "comments_field": "",
    }
    assert lead_has_rule2_justification(lead)
    assert check_lead_rule2_rule3_violations([lead]) == []


def test_rule2_violation_when_no_timeline():
    lead = {
        "lead_id": 2002,
        "status_id": LEAD_STATUS_JUNK,
        "status_name": "Спам",
        "assigned_by_id": 10,
        "timeline": [],
        "comments_field": "BitrixGPT длинный текст не считается для спама",
    }
    assert not lead_has_rule2_justification(lead)
    violations = check_lead_rule2_rule3_violations([lead])
    assert len(violations) == 1
    assert violations[0]["rule"] == "lead_rule_2"


def test_rule3_bitrixgpt_comments_field_is_justification():
    lead = {
        "lead_id": 2682,
        "status_id": LEAD_STATUS_NECELEVOY,
        "status_name": "Нецелевой",
        "assigned_by_id": 70,
        "timeline": [],
        "comments_field": (
            "[p]BitrixGPT\nВ диалоге отсутствует деловая информация.[/p]"
        ),
    }
    assert lead_has_rule3_justification(lead)
    assert check_lead_rule2_rule3_violations([lead]) == []


def test_rule3_timeline_comment_is_justification():
    lead = {
        "lead_id": 3001,
        "status_id": LEAD_STATUS_NECELEVOY,
        "status_name": "Нецелевой",
        "assigned_by_id": 10,
        "timeline": [{"comment": "Набрали случайно"}],
        "comments_field": "",
    }
    assert lead_has_rule3_justification(lead)
    assert check_lead_rule2_rule3_violations([lead]) == []


def test_rule3_violation_when_empty():
    lead = {
        "lead_id": 3002,
        "status_id": LEAD_STATUS_NECELEVOY,
        "status_name": "Нецелевой",
        "assigned_by_id": 10,
        "timeline": [],
        "comments_field": "",
    }
    violations = check_lead_rule2_rule3_violations([lead])
    assert len(violations) == 1
    assert violations[0]["rule"] == "lead_rule_3"
    assert "Нецелевой" in violations[0]["reason"]


def test_new_over_24h_dry_run_reports_without_move(monkeypatch):
    current_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    leads = [
        {
            "lead_id": 4001,
            "status_id": "NEW",
            "title": "Stale",
            "date_create": "2026-06-14T10:00:00+00:00",
            "assigned_by_id": 10,
            "timeline": [{"author_id": 10, "comment": "Взял"}],
        }
    ]

    def _fake_move(lead_id: int):
        return {"dry_run_skipped": True, "lead_id": lead_id, "status_id": "1"}

    monkeypatch.setattr("tools.move_lead_to_shared_pool", _fake_move)
    from tools import process_stale_new_leads

    violations = process_stale_new_leads(leads, current_time)
    assert len(violations) == 1
    assert violations[0]["rule"] == "lead_new_over_24h"
    assert violations[0]["details"]["dry_run_skipped"] is True
    assert "Общие лиды" in violations[0]["reason"]


def test_new_under_24h_not_moved(monkeypatch):
    current_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    leads = [
        {
            "lead_id": 4002,
            "status_id": "NEW",
            "date_create": "2026-06-15T01:00:00+00:00",
            "assigned_by_id": 10,
            "timeline": [],
        }
    ]
    called = []
    monkeypatch.setattr(
        "tools.move_lead_to_shared_pool",
        lambda lead_id: called.append(lead_id) or {"ok": True},
    )
    from tools import process_stale_new_leads

    assert process_stale_new_leads(leads, current_time) == []
    assert called == []


def test_move_lead_to_shared_pool_dry_run(monkeypatch):
    class _Settings:
        dry_run = True

    monkeypatch.setattr("tools.get_settings", lambda: _Settings())
    from tools import LEAD_STATUS_SHARED, move_lead_to_shared_pool

    result = move_lead_to_shared_pool(55)
    assert result["dry_run_skipped"] is True
    assert result["status_id"] == LEAD_STATUS_SHARED


def test_latest_stage_entered_at_picks_last_match():
    from tools import _latest_stage_entered_at

    rows = [
        {"OWNER_ID": 10, "STAGE_ID": "C18:NEW", "CREATED_TIME": "2026-06-01T10:00:00+00:00"},
        {"OWNER_ID": 10, "STAGE_ID": "C18:NEW", "CREATED_TIME": "2026-06-05T10:00:00+00:00"},
        {"OWNER_ID": 10, "STAGE_ID": "C18:UC_UFPFKK", "CREATED_TIME": "2026-06-08T10:00:00+00:00"},
        {"OWNER_ID": 11, "STAGE_ID": "C18:NEW", "CREATED_TIME": "2026-06-09T10:00:00+00:00"},
    ]
    entered = _latest_stage_entered_at(rows, 10, "C18:NEW")
    assert entered is not None
    assert entered.day == 5
