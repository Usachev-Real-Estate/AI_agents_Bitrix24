"""Application configuration loaded from environment variables."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BX_EXECUTOR = ThreadPoolExecutor(max_workers=4)


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
    deepseek_api_key: str = Field(validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias="DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="DEEPSEEK_MODEL",
    )
    deepseek_v3_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="DEEPSEEK_V3_MODEL",
    )
    dry_run: bool = Field(default=True, validation_alias="DRY_RUN")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    management_chat_id: str = Field(
        default="",
        validation_alias="MANAGEMENT_CHAT_ID",
    )
    admin_user_id: int = Field(default=1, validation_alias="ADMIN_USER_ID")
    report_chat_id: int = Field(default=22358, validation_alias="REPORT_CHAT_ID")
    dept_chat_map_json: str = Field(default="{}", validation_alias="DEPT_CHAT_MAP_JSON")
    analyst_chunk_size: int = Field(default=100, validation_alias="ANALYST_CHUNK_SIZE")
    back_office_dept_id: int = Field(default=0, validation_alias="BACK_OFFICE_DEPT_ID")
    back_office_chat_id: int = Field(default=0, validation_alias="BACK_OFFICE_CHAT_ID")
    contact_owner_type_id: str = Field(default="UC_2G0TD3", validation_alias="CONTACT_OWNER_TYPE_ID")
    owner_kpi_since: str = Field(default="2026-06-01", validation_alias="OWNER_KPI_SINCE")
    owner_kpi_target: int = Field(default=10, validation_alias="OWNER_KPI_TARGET")
    owner_exclude_user_ids_json: str = Field(default="[]", validation_alias="OWNER_EXCLUDE_USER_IDS_JSON")
    rules_advice_json: str = Field(
        default='{}',
        validation_alias="RULES_ADVICE_JSON",
    )
    owner_sales_dept_ids_json: str = Field(
        default='[42, 44, 46, 50, 60, 66]',
        validation_alias="OWNER_SALES_DEPT_IDS_JSON",
    )
    task_auditor_exclude_users_json: str = Field(
        default='["Агентство Недвижимости", "Вера Волкова", "Светлана Щербакова", "Марина Володина"]',
        validation_alias="TASK_AUDITOR_EXCLUDE_USERS_JSON",
    )

    @property
    def rules_advice(self) -> dict[str, str]:
        try:
            return json.loads(self.rules_advice_json)
        except Exception:
            return {}

    @property
    def owner_sales_dept_ids(self) -> list[int]:
        try:
            return json.loads(self.owner_sales_dept_ids_json)
        except Exception:
            return [42, 44, 46, 50, 60, 66]

    @property
    def task_auditor_exclude_users(self) -> set[str]:
        try:
            return set(json.loads(self.task_auditor_exclude_users_json))
        except Exception:
            return set()

    @property
    def owner_exclude_user_ids(self) -> set[int]:
        try:
            return set(json.loads(self.owner_exclude_user_ids_json))
        except Exception:
            return set()

    @property
    def dept_chat_map(self) -> dict[int, int]:
        """Return parsed DEPT_CHAT_MAP mapping from JSON string.

        Returns empty dict on malformed JSON — logged as warning.
        """
        try:
            return {
                int(k): int(v)
                for k, v in json.loads(self.dept_chat_map_json).items()
            }
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Invalid DEPT_CHAT_MAP_JSON: %s",
                exc,
            )
            return {}


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

    file_handler = RotatingFileHandler(
        log_dir / "audit.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(fmt))
    root_logger.addHandler(file_handler)
