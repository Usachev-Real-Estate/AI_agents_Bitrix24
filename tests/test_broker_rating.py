"""Tests for broker CRM rating engine."""

from datetime import datetime, timezone

import pytest

from broker_rating import (
    BrokerRating,
    TasksMetrics,
    EngagementMetrics,
    analyze_tasks,
    assign_ranks,
    broker_has_active_crm,
    compute_clean_day_bonus,
    compute_crm_score,
    compute_engagement_score,
    compute_portfolio_score,
    compute_tasks_score,
    compute_total_score,
    score_to_tier,
)


DEFAULT_WEIGHTS = {
    "component_weights": {"crm": 0.60, "portfolio": 0.20, "tasks": 0.10, "engagement": 0.10},
    "severity_penalties": {"medium": 4, "high": 6, "very high": 10},
    "shared_lead_penalty": 12,
    "pool_deal_penalty": 18,
    "tasks_neutral_score": 75,
    "tasks_closure_bonus": 5,
    "clean_day_bonus": 0.5,
    "clean_day_bonus_max": 5,
    "tier_green": 80,
    "tier_yellow": 50,
}


def test_compute_crm_score_no_violations():
    score, count, by_rule = compute_crm_score([], DEFAULT_WEIGHTS)
    assert score == 100.0
    assert count == 0
    assert by_rule == {}


def test_compute_crm_score_with_penalties():
    violations = [
        {"rule": "lead_rule_1", "severity": "high"},
        {"rule": "buyer_stage_2", "severity": "medium"},
        {"rule": "lead_missed_callback", "severity": "very high"},
    ]
    score, count, by_rule = compute_crm_score(violations, DEFAULT_WEIGHTS)
    assert count == 3
    assert score == 100.0 - 6 - 4 - 10
    assert by_rule["lead_rule_1"] == 1


def test_compute_portfolio_score():
    assert compute_portfolio_score(0, 0, DEFAULT_WEIGHTS) == 100.0
    assert compute_portfolio_score(1, 0, DEFAULT_WEIGHTS) == 88.0
    assert compute_portfolio_score(0, 1, DEFAULT_WEIGHTS) == 82.0


def test_compute_tasks_score_no_tasks():
    assert compute_tasks_score(TasksMetrics(), DEFAULT_WEIGHTS) == 75.0


def test_compute_tasks_score_overdue():
    metrics = TasksMetrics(had_deadline_tasks=True, active_with_deadline=4, overdue=1)
    assert compute_tasks_score(metrics, DEFAULT_WEIGHTS) == 75.0


def test_compute_tasks_score_closure_bonus():
    metrics = TasksMetrics(had_deadline_tasks=True, all_closed_on_time=True)
    assert compute_tasks_score(metrics, DEFAULT_WEIGHTS) == 100.0


def test_compute_engagement_score():
    m = EngagementMetrics(crm_visit_days=18, workdays_in_period=22, important_posts_total=5, important_posts_read=4)
    score = compute_engagement_score(m)
    assert 75 < score < 85


def test_compute_engagement_no_important_posts():
    m = EngagementMetrics(crm_visit_days=10, workdays_in_period=20, important_posts_total=0)
    assert compute_engagement_score(m) == 75.0


def test_clean_day_bonus_capped():
    assert compute_clean_day_bonus(6, DEFAULT_WEIGHTS) == 3.0
    assert compute_clean_day_bonus(20, DEFAULT_WEIGHTS) == 5.0


def test_total_score_example_from_plan():
    total = compute_total_score(84, 88, 75, 81, 3, DEFAULT_WEIGHTS)
    assert 86 <= total <= 88


def test_score_to_tier():
    assert score_to_tier(85, DEFAULT_WEIGHTS)[0] == "green"
    assert score_to_tier(65, DEFAULT_WEIGHTS)[0] == "yellow"
    assert score_to_tier(40, DEFAULT_WEIGHTS)[0] == "red"


def test_assign_ranks():
    ratings = [
        BrokerRating(1, "A", "D1", 42, 90, "green", "🟢", 90, 90, 90, 90, 0),
        BrokerRating(2, "B", "D1", 42, 70, "yellow", "🟡", 70, 70, 70, 70, 0),
        BrokerRating(3, "C", "D2", 44, 80, "green", "🟢", 80, 80, 80, 80, 0),
    ]
    assign_ranks(ratings)
    assert ratings[0].rank_overall == 1
    assert ratings[2].rank_overall == 2
    assert ratings[1].rank_overall == 3
    assert ratings[0].rank_in_dept == 1
    assert ratings[1].rank_in_dept == 2


def test_compute_crm_score_portfolio_normalization():
    violations = [{"rule": "lead_rule_1", "severity": "high"}] * 4
    raw, _, _ = compute_crm_score(violations, DEFAULT_WEIGHTS, portfolio_size=0)
    norm, _, _ = compute_crm_score(violations, DEFAULT_WEIGHTS, portfolio_size=20)
    assert norm > raw


def test_broker_has_active_crm():
    assert broker_has_active_crm({"lead_count": 1, "deal_count": 0}) is True
    assert broker_has_active_crm({"lead_count": 0, "deal_count": 2}) is True
    assert broker_has_active_crm({"lead_count": 0, "deal_count": 0}) is False


def test_get_rating_period_calendar_week(monkeypatch):
    from broker_rating import get_rating_period

    class FakeSettings:
        broker_rating_period_days = 7
        broker_rating_since = ""

    import broker_rating as br
    monkeypatch.setattr(br, "get_settings", lambda: FakeSettings())

    since_iso, until_iso, since_dt, until_dt = get_rating_period()
    assert since_dt.weekday() == 0
    assert since_dt.hour == 0
    assert since_dt <= until_dt


def test_get_rating_period_fixed_since(monkeypatch):
    from broker_rating import get_rating_period
    from zoneinfo import ZoneInfo

    class FakeSettings:
        broker_rating_period_days = 7
        broker_rating_since = "2026-08-06"

    import broker_rating as br
    monkeypatch.setattr(br, "get_settings", lambda: FakeSettings())

    since_iso, until_iso, since_dt, until_dt = get_rating_period()
    local = since_dt.astimezone(ZoneInfo("Europe/Moscow"))
    assert local.date().isoformat() == "2026-08-06"
    assert local.hour == 0
    assert since_dt <= until_dt


def test_format_leaderboard_without_department():
    from broker_rating import BrokerRating, format_rating_leaderboard
    from datetime import datetime, timezone

    rating = BrokerRating(
        responsible_id=1,
        responsible_name="Иван Иванов",
        department="Кретов",
        department_id=42,
        score=80.0,
        tier="green",
        tier_label="🟢 ОТЛИЧНО",
        crm_score=80.0,
        portfolio_score=100.0,
        tasks_score=80.0,
        engagement_score=70.0,
        clean_day_bonus=0.0,
        rank_overall=1,
    )
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    until = datetime(2026, 8, 6, tzinfo=timezone.utc)
    text = format_rating_leaderboard([rating], since, until)
    assert "Иван Иванов — 80%" in text
    assert "(Кретов)" not in text


def test_analyze_tasks_overdue():
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    tasks = [
        {"STATUS": 2, "DEADLINE": "2026-08-01T10:00:00+00:00"},
        {"STATUS": 2, "DEADLINE": "2026-08-10T10:00:00+00:00"},
    ]
    metrics = analyze_tasks(tasks, now)
    assert metrics.overdue == 1
    assert metrics.active_with_deadline == 2
