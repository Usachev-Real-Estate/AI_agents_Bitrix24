"""Regression: broker_score must use Settings.rules_advice (not RULE_ADVICE)."""

from broker_score import score_broker


def test_score_broker_uses_settings_rules_advice(monkeypatch) -> None:
    monkeypatch.setenv(
        "RULES_ADVICE_JSON",
        '{"lead_rule_1":"ускорить квалификацию новых лидов"}',
    )
    from config import get_settings

    get_settings.cache_clear()

    result = score_broker(
        violations={
            "total": 3,
            "by_rule": {"lead_rule_1": 3},
            "by_type": {"lead": 3, "deal": 0, "missed_call": 0},
        },
        owners={"total": 10, "contacts": []},
        kpi_target=10,
    )

    assert "ускорить квалификацию новых лидов" in result["advices"]
    get_settings.cache_clear()


def test_broker_score_module_imports_without_rule_advice() -> None:
    """chat_poller imports broker_score; broken RULE_ADVICE import must not return."""
    import importlib

    import broker_score

    importlib.reload(broker_score)
    assert hasattr(broker_score, "score_broker")
    assert not hasattr(broker_score, "RULE_ADVICE")
