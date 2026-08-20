"""Regression: broker_score module loads and uses broker_rating."""

import importlib


def test_broker_score_module_imports():
    import broker_score

    importlib.reload(broker_score)
    assert hasattr(broker_score, "build_scorecard_for_broker")
    assert hasattr(broker_score, "get_broker_info")


def test_broker_rating_advices_in_scorecard(monkeypatch):
    """Violations breakdown appears in rating scorecard format."""
    from broker_rating import BrokerRating, format_rating_scorecard
    from datetime import datetime, timezone

    rating = BrokerRating(
        responsible_id=40,
        responsible_name="Test Broker",
        department="Test Dept",
        department_id=42,
        score=75.0,
        tier="yellow",
        tier_label="🟡 СРЕДНЕ",
        crm_score=80.0,
        portfolio_score=100.0,
        tasks_score=70.0,
        engagement_score=65.0,
        clean_day_bonus=2.0,
        lead_count=5,
        deal_count=3,
        task_count=10,
        violations_count=2,
        violations_by_rule={"lead_rule_1": 2},
        rank_overall=5,
        rank_in_dept=2,
    )
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    until = datetime(2026, 8, 6, tzinfo=timezone.utc)
    text = format_rating_scorecard(rating, since, until)
    assert "75%" in text
    assert "Рейтинг:" in text
    assert "CRM 40%" not in text
