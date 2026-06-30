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
