"""Application configuration loaded from environment variables."""

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Pydantic settings for b24-ai-auditor (.env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    b24_webhook_url: str = Field(validation_alias="B24_WEBHOOK_URL")
    b24_user_id: int = Field(default=1, validation_alias="B24_USER_ID")
    buyers_category_id: int = Field(default=0, validation_alias="BUYERS_CATEGORY_ID")
    sellers_category_id: int = Field(default=0, validation_alias="SELLERS_CATEGORY_ID")
    report_since: str = Field(default="", validation_alias="REPORT_SINCE")
    routerai_api_key: str = Field(validation_alias="ROUTERAI_API_KEY")
    routerai_base_url: str = Field(
        default="https://routerai.ru/api/v1",
        validation_alias="ROUTERAI_BASE_URL",
    )
    routerai_r1_model: str = Field(
        default="deepseek/deepseek-v4-flash",
        validation_alias="ROUTERAI_R1_MODEL",
    )
    routerai_v3_model: str = Field(
        default="deepseek/deepseek-v4-flash",
        validation_alias="ROUTERAI_V3_MODEL",
    )
    dry_run: bool = Field(default=True, validation_alias="DRY_RUN")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    management_chat_id: str = Field(
        default="",
        validation_alias="MANAGEMENT_CHAT_ID",
    )
    admin_user_id: int = Field(default=1, validation_alias="ADMIN_USER_ID")


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance.

    Returns:
        Loaded Settings from environment and .env file.
    """
    return Settings()


def setup_logging(level: str) -> None:
    """Configure root logger: console + file.

    Args:
        level: Log level name (INFO, DEBUG, WARNING, ...).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(log_level)

    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(logging.Formatter(fmt))
    root_logger.addHandler(console)

    file_handler = logging.FileHandler(log_dir / "audit.log", encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(fmt))
    root_logger.addHandler(file_handler)
