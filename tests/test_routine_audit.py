"""Regression: routine audit gate must keep saving production runs."""

import pytest

from db import is_routine_audit_run


@pytest.mark.parametrize(
    "total_leads",
    [100, 500, 1000, 2500, 9999],
)
def test_routine_accepts_production_lead_volumes(total_leads: int) -> None:
    """Weekday cron slots stay routine even when CRM has ~1000+ leads."""
    assert is_routine_audit_run(
        "2026-06-12",
        total_leads,
        "2026-07-23T07:00:02",
    ) is True


def test_routine_allows_small_startup_minute_skew() -> None:
    assert is_routine_audit_run(
        "2026-06-12",
        1012,
        "2026-07-23T14:05:00",
    ) is True


def test_routine_rejects_old_report_since() -> None:
    assert is_routine_audit_run(
        "2026-05-01",
        200,
        "2026-07-23T07:00:00",
    ) is False


def test_routine_rejects_weekend() -> None:
    # 2026-07-25 is Saturday
    assert is_routine_audit_run(
        "2026-06-12",
        200,
        "2026-07-25T07:00:00",
    ) is False


def test_routine_rejects_off_schedule_hour() -> None:
    assert is_routine_audit_run(
        "2026-06-12",
        200,
        "2026-07-23T10:00:00",
    ) is False


def test_routine_rejects_pathological_full_dump() -> None:
    assert is_routine_audit_run(
        "2026-06-12",
        10_001,
        "2026-07-23T07:00:00",
    ) is False
