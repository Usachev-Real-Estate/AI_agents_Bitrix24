"""Tests for configuration loading."""

from config import Settings


def test_settings_dry_run_default_true(monkeypatch) -> None:
    """DRY_RUN defaults to true when env is unset."""
    monkeypatch.delenv("DRY_RUN", raising=False)
    settings = Settings(
        B24_WEBHOOK_URL="https://example.bitrix24.ru/rest/1/x/",
        DEEPSEEK_API_KEY="sk-test",
        _env_file=None,
    )
    assert settings.dry_run is True


def test_buyer_commission_defaults() -> None:
    settings = Settings(
        B24_WEBHOOK_URL="https://example.bitrix24.ru/rest/1/x/",
        DEEPSEEK_API_KEY="sk-test",
        _env_file=None,
    )
    assert settings.buyer_commission_reminder_enabled is True
    assert settings.buyer_commission_broker_interval_hours == 2.0
    assert settings.buyer_commission_rop_interval_hours == 1.0
    assert settings.buyer_commission_deadline_hour == 19
    assert settings.buyer_commission_pool_user_id == 1
    assert settings.buyer_commission_enforce_enabled is True
