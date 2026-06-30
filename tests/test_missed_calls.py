"""Tests for deterministic missed calls rule."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools import check_missed_callback_violations


def test_missed_call_violation():
    entities = [
        {
            "entity_id": 55,
            "assigned_by_id": 10,
            "calls": [
                {
                    "call_type": "incoming",
                    "status": "missed",
                    "start_date": "2026-06-15T10:00:00+00:00"
                }
            ],
            "timeline": []
        }
    ]
    violations = check_missed_callback_violations(entities, "lead")
    assert len(violations) == 1
    assert violations[0]["rule"] == "lead_missed_callback"


def test_missed_call_resolved_by_callback():
    entities = [
        {
            "entity_id": 56,
            "assigned_by_id": 10,
            "calls": [
                {
                    "call_type": "incoming",
                    "status": "missed",
                    "start_date": "2026-06-15T10:00:00+00:00"
                },
                {
                    "call_type": "outgoing",
                    "status": "successful",
                    "start_date": "2026-06-15T10:30:00+00:00",
                }
            ],
            "timeline": []
        }
    ]
    violations = check_missed_callback_violations(entities, "deal")
    assert len(violations) == 0


def test_missed_call_resolved_by_timeline_comment():
    entities = [
        {
            "entity_id": 57,
            "assigned_by_id": 10,
            "calls": [
                {
                    "direction": 1,
                    "completed": "Y",
                    "result_code": "304",
                    "created": "2026-06-15T10:00:00+00:00"
                }
            ],
            "timeline": [
                {
                    "author_id": 10,
                    "created": "2026-06-15T10:45:00+00:00",
                    "comment": "Клиент перезвонил сам"
                }
            ]
        }
    ]
    violations = check_missed_callback_violations(entities, "lead")
    assert len(violations) == 0
