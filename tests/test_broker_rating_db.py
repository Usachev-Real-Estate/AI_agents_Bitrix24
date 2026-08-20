"""Tests for broker rating DB helpers."""

from datetime import datetime, timezone

import pytest

from db import (
    get_broker_daily_metrics_summary,
    get_previous_rating_snapshot,
    init_db,
    save_broker_ratings_snapshot,
    upsert_broker_daily_metric,
    upsert_broker_shared_lead,
)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("db.DB_PATH", db_path)
    init_db()
    yield db_path


def test_upsert_shared_lead(temp_db):
    now = datetime.now(timezone.utc).isoformat()
    upsert_broker_shared_lead(100, 40, now, now)
    upsert_broker_shared_lead(100, 40, now, now)  # idempotent


def test_daily_metrics_summary(temp_db):
    upsert_broker_daily_metric("2026-08-01", 40, had_crm_visit=1, clean_day=1)
    upsert_broker_daily_metric("2026-08-02", 40, had_crm_visit=0, clean_day=1)
    summary = get_broker_daily_metrics_summary(40, "2026-08-01", "2026-08-03")
    assert summary["crm_visit_days"] == 1
    assert summary["clean_days"] == 2


def test_rating_snapshot(temp_db):
    save_broker_ratings_snapshot([
        {
            "responsible_id": 40,
            "responsible_name": "Test",
            "department": "D",
            "department_id": 42,
            "score": 85.0,
            "tier": "green",
            "crm_score": 90,
            "portfolio_score": 100,
            "tasks_score": 80,
            "engagement_score": 75,
            "rank_overall": 1,
            "rank_in_dept": 1,
        }
    ], "2026-08-06")
    prev = get_previous_rating_snapshot("2026-08-07")
    assert prev.get(40) == 85.0
