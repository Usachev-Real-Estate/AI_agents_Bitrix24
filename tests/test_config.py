"""Tests for configuration loading."""

from config import Settings


def test_settings_dry_run_default_true(monkeypatch) -> None:
    """DRY_RUN defaults to true when env is unset."""
    monkeypatch.delenv("DRY_RUN", raising=False)
    settings = Settings(
        B24_WEBHOOK_URL="https://example.bitrix24.ru/rest/1/x/",
        LLM_API_KEY="sk-test",
        _env_file=None,
    )
    assert settings.dry_run is True


def test_buyer_commission_defaults() -> None:
    settings = Settings(
        B24_WEBHOOK_URL="https://example.bitrix24.ru/rest/1/x/",
        LLM_API_KEY="sk-test",
        _env_file=None,
    )
    assert settings.buyer_commission_reminder_enabled is True
    assert settings.buyer_commission_broker_interval_hours == 2.0
    assert settings.buyer_commission_rop_interval_hours == 1.0
    assert settings.buyer_commission_deadline_hour == 19
    assert settings.buyer_commission_pool_user_id == 1
    assert settings.buyer_commission_enforce_enabled is False


def test_general_base_move_after_default_empty() -> None:
    settings = Settings(
        B24_WEBHOOK_URL="https://example.bitrix24.ru/rest/1/x/",
        LLM_API_KEY="sk-test",
        _env_file=None,
    )
    assert settings.general_base_move_after == ""


def test_lead_quality_defaults() -> None:
    settings = Settings(
        B24_WEBHOOK_URL="https://example.bitrix24.ru/rest/1/x/",
        LLM_API_KEY="sk-test",
        _env_file=None,
    )
    assert settings.lead_quality_enabled is True
    assert settings.lead_quality_since == "2026-07-01"
    assert settings.lead_quality_chunk_size == 40


def test_llm_defaults_routerai() -> None:
    settings = Settings(
        B24_WEBHOOK_URL="https://example.bitrix24.ru/rest/1/x/",
        LLM_API_KEY="sk-test",
        _env_file=None,
    )
    assert settings.llm_base_url == "https://routerai.ru/api/v1"
    assert settings.llm_model == "google/gemini-3.7-flash"


def test_llm_legacy_deepseek_env_aliases() -> None:
    settings = Settings(
        B24_WEBHOOK_URL="https://example.bitrix24.ru/rest/1/x/",
        DEEPSEEK_API_KEY="legacy-key",
        DEEPSEEK_BASE_URL="https://api.deepseek.com",
        DEEPSEEK_MODEL="deepseek-v4-flash",
        _env_file=None,
    )
    assert settings.llm_api_key == "legacy-key"
    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_model == "deepseek-v4-flash"
