"""Tests for scheduled-job failure alerts."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from job_alert import TAIL_LIMIT, build_alert  # noqa: E402

WHEN = datetime(2026, 8, 20, 7, 5, tzinfo=timezone.utc)


def test_alert_names_the_job_and_code():
    text = build_alert("audit", 1, "", WHEN)
    assert "«audit»" in text
    assert "код 1" in text
    assert "20.08.2026 07:05 UTC" in text


def test_alert_includes_log_tail():
    text = build_alert("lead-quality", 2, "Traceback ...\nValueError: boom", WHEN)
    assert "ValueError: boom" in text


def test_alert_truncates_a_huge_tail():
    text = build_alert("audit", 1, "x" * 50_000, WHEN)
    assert len(text) < TAIL_LIMIT + 500


def test_alert_without_tail_has_no_empty_section():
    text = build_alert("audit", 1, "   ", WHEN)
    assert "Последние строки" not in text
