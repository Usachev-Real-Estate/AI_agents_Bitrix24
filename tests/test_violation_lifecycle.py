"""Tests for violation identity and lifecycle tracking."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import db  # noqa: E402

ALL_SCOPES = {"leads", "buyers", "sellers", "general_base"}


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point the DB at a throwaway file for each test."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "violations.db")
    db.init_db()


def _v(entity_id: int, rule: str = "buyer_stage_2", responsible: int = 7):
    return {
        "entity_type": "deal",
        "entity_id": entity_id,
        "rule": rule,
        "severity": "medium",
        "responsible_id": responsible,
        "reason": "тест",
    }


def _state(entity_id: int, rule: str = "buyer_stage_2"):
    with db.db_session() as conn:
        row = conn.execute(
            "SELECT first_detected_at, last_seen_at, resolved_at, times_seen "
            "FROM violation_states WHERE entity_id = ? AND rule = ?",
            (entity_id, rule),
        ).fetchone()
    return row


def test_new_violation_is_opened():
    stats = db.sync_violation_states(
        [_v(1)], "2026-08-01T07:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    assert stats["opened"] == 1
    first, last, resolved, seen = _state(1)
    assert first == last == "2026-08-01T07:00:00+00:00"
    assert resolved is None
    assert seen == 1


def test_repeat_keeps_first_detection_and_counts_sightings():
    db.sync_violation_states(
        [_v(1)], "2026-08-01T07:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    stats = db.sync_violation_states(
        [_v(1)], "2026-08-01T14:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    assert stats == {"opened": 0, "still_open": 1, "reopened": 0, "resolved": 0}
    first, last, resolved, seen = _state(1)
    assert first == "2026-08-01T07:00:00+00:00", "начало отсчёта не должно сдвигаться"
    assert last == "2026-08-01T14:00:00+00:00"
    assert resolved is None
    assert seen == 2


def test_disappeared_violation_is_resolved():
    db.sync_violation_states(
        [_v(1)], "2026-08-01T07:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    stats = db.sync_violation_states(
        [], "2026-08-02T07:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    assert stats["resolved"] == 1
    assert _state(1)[2] == "2026-08-02T07:00:00+00:00"


def test_empty_scope_never_resolves_anything():
    """Сбой сбора данных не должен выглядеть как «всё исправлено»."""
    db.sync_violation_states(
        [_v(1)], "2026-08-01T07:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    # Воронка покупателей не собралась — область отсутствует в scopes_with_data.
    stats = db.sync_violation_states(
        [], "2026-08-02T07:00:00+00:00", scopes_with_data={"leads", "sellers"},
    )
    assert stats["resolved"] == 0
    assert _state(1)[2] is None, "нарушение закрыто без данных по своей области"


def test_no_scopes_at_all_resolves_nothing():
    db.sync_violation_states(
        [_v(1)], "2026-08-01T07:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    stats = db.sync_violation_states(
        [], "2026-08-02T07:00:00+00:00", scopes_with_data=set(),
    )
    assert stats["resolved"] == 0
    assert _state(1)[2] is None


def test_other_scope_still_resolves():
    """Закрытие работает по своей области, даже если другая не собралась."""
    db.sync_violation_states(
        [_v(1, "lead_rule_2"), _v(2, "buyer_stage_2")],
        "2026-08-01T07:00:00+00:00",
        scopes_with_data=ALL_SCOPES,
    )
    db.sync_violation_states(
        [], "2026-08-02T07:00:00+00:00", scopes_with_data={"leads"},
    )
    assert _state(1, "lead_rule_2")[2] is not None, "лиды собрались — закрыто"
    assert _state(2, "buyer_stage_2")[2] is None, "покупатели не собрались — открыто"


def test_reappearance_restarts_the_clock():
    db.sync_violation_states(
        [_v(1)], "2026-08-01T07:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    db.sync_violation_states(
        [], "2026-08-02T07:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    stats = db.sync_violation_states(
        [_v(1)], "2026-08-05T07:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    assert stats["reopened"] == 1
    first, _last, resolved, seen = _state(1)
    assert first == "2026-08-05T07:00:00+00:00"
    assert resolved is None
    assert seen == 1


def test_scope_mapping():
    assert db.violation_scope("lead_rule_2") == "leads"
    assert db.violation_scope("lead_missed_callback") == "leads"
    assert db.violation_scope("buyer_stage_3") == "buyers"
    assert db.violation_scope("seller_afina_id_missing") == "sellers"
    assert db.violation_scope("general_base_no_plan") == "general_base"
    assert db.violation_scope("unknown_rule") == ""


def test_open_violations_for_broker():
    db.sync_violation_states(
        [_v(1), _v(2), _v(3, responsible=9)],
        "2026-08-01T07:00:00+00:00",
        scopes_with_data=ALL_SCOPES,
    )
    rows = db.get_open_violations_for_broker(7)
    assert {r["entity_id"] for r in rows} == {1, 2}


def test_resolution_stats_measure_time_to_fix():
    db.sync_violation_states(
        [_v(1), _v(2)], "2026-08-01T00:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    # Одно чинится через 6 часов, второе остаётся открытым.
    db.sync_violation_states(
        [_v(2)], "2026-08-01T06:00:00+00:00", scopes_with_data=ALL_SCOPES,
    )
    stats = db.get_resolution_stats("2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00")
    assert stats["opened"] == 2
    assert stats["resolved"] == 1
    assert stats["still_open"] == 1
    assert stats["median_hours_to_fix"] == 6.0


def test_rating_counts_problems_not_audit_runs():
    """Одна и та же проблема в двух прогонах — одно нарушение для рейтинга."""
    run_a = db.save_audit_run("2026-08-01T07:00:00+00:00", 1, 1, 1, 1)
    run_b = db.save_audit_run("2026-08-01T14:00:00+00:00", 1, 1, 1, 1)
    payload = [_v(100)]
    for run_id in (run_a, run_b):
        db.save_violations(run_id, payload, {7: "Иван (Кретов)"}, {7: 42},
                           "2026-08-01T07:00:00+00:00")

    rows = db.get_violations_for_broker(7, "2026-08-01T00:00:00+00:00")
    assert len(rows) == 1, f"счёт привязан к числу прогонов: {rows}"
    assert rows[0]["times_detected"] == 2
    assert db.count_violations_on_date(7, "2026-08-01") == 1


# ── Раздел отчёта про скорость исправления ─────────────────────────────
def test_resolution_section_is_empty_without_data():
    from weekly_report import format_resolution_section

    assert format_resolution_section(
        {"opened": 0, "resolved": 0, "still_open": 0},
    ) == ""


def test_resolution_section_reports_hours_and_days():
    from weekly_report import format_resolution_section

    hours = format_resolution_section({
        "opened": 12, "resolved": 9, "still_open": 3, "median_hours_to_fix": 5.0,
    })
    assert "Появилось за период: 12" in hours
    assert "Исправлено за период: 9" in hours
    assert "Остаётся открытыми: 3" in hours
    assert "5 ч" in hours

    days = format_resolution_section({
        "opened": 4, "resolved": 2, "still_open": 2, "median_hours_to_fix": 60.0,
    })
    assert "2.5 дн." in days


def test_resolution_section_omits_speed_when_nothing_resolved():
    from weekly_report import format_resolution_section

    text = format_resolution_section({
        "opened": 5, "resolved": 0, "still_open": 5, "median_hours_to_fix": 0.0,
    })
    assert "Остаётся открытыми: 5" in text
    assert "Медианное время" not in text
