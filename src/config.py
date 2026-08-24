"""Application configuration loaded from environment variables."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field
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
    llm_api_key: str = Field(
        validation_alias=AliasChoices("LLM_API_KEY", "DEEPSEEK_API_KEY"),
    )
    llm_base_url: str = Field(
        default="https://routerai.ru/api/v1",
        validation_alias=AliasChoices("LLM_BASE_URL", "DEEPSEEK_BASE_URL"),
    )
    llm_model: str = Field(
        default="google/gemini-3.7-flash",
        validation_alias=AliasChoices("LLM_MODEL", "DEEPSEEK_MODEL"),
    )
    # Потолок ответа. Наш ответ — JSON фиксированной схемы, ему хватает пары
    # тысяч токенов; лимит стоит не ради экономии на норме, а чтобы сорвавшаяся
    # генерация не выставила счёт на сотню тысяч токенов. 0 — не ограничивать.
    llm_max_tokens: int = Field(
        default=16_000,
        validation_alias="LLM_MAX_TOKENS",
    )
    # Бюджет «размышлений» у моделей, которые их поддерживают. У RouterAI это
    # отдельная и самая дорогая строка тарифа, а наша задача — извлечение
    # фактов по схеме, а не рассуждение. Пусто — оставить дефолт провайдера.
    llm_reasoning_effort: str = Field(
        default="",
        validation_alias="LLM_REASONING_EFFORT",
    )
    # Тариф RouterAI (₽ за 1М токенов) на 2026-08. Нужен, чтобы прогон писал
    # в лог не абстрактные токены, а рубли. Меняется у провайдера — правится
    # здесь, без правки кода.
    llm_price_input: float = Field(default=40.0, validation_alias="LLM_PRICE_INPUT")
    llm_price_output: float = Field(
        default=202.0, validation_alias="LLM_PRICE_OUTPUT",
    )
    # Размышления тарифицируются по цене выхода и уже входят в output_tokens,
    # поэтому отдельной строкой в расчёт не идут — но считаются отдельно,
    # чтобы было видно, какая доля выхода уходит в них.
    llm_price_cache_read: float = Field(
        default=4.04, validation_alias="LLM_PRICE_CACHE_READ",
    )
    llm_v3_model: str = Field(
        default="google/gemini-3.7-flash",
        validation_alias=AliasChoices("LLM_V3_MODEL", "DEEPSEEK_V3_MODEL"),
    )
    dry_run: bool = Field(default=True, validation_alias="DRY_RUN")
    general_base_move_after: str = Field(
        default="",
        validation_alias="GENERAL_BASE_MOVE_AFTER",
    )
    force_routine_audit: bool = Field(
        default=False,
        validation_alias="FORCE_ROUTINE_AUDIT",
    )
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
    contact_owner_type_id: str = Field(
        default="UC_2G0TD3",
        validation_alias="CONTACT_OWNER_TYPE_ID",
    )
    owner_kpi_since: str = Field(default="2026-06-01", validation_alias="OWNER_KPI_SINCE")
    owner_kpi_target: int = Field(default=10, validation_alias="OWNER_KPI_TARGET")
    owner_exclude_user_ids_json: str = Field(
        default="[]",
        validation_alias="OWNER_EXCLUDE_USER_IDS_JSON",
    )
    rules_advice_json: str = Field(
        default='{}',
        validation_alias="RULES_ADVICE_JSON",
    )
    owner_sales_dept_ids_json: str = Field(
        default='[42, 44, 46, 50, 60, 66]',
        validation_alias="OWNER_SALES_DEPT_IDS_JSON",
    )
    task_auditor_exclude_users_json: str = Field(
        default=(
            '["Агентство Недвижимости", "Вера Волкова", '
            '"Светлана Щербакова", "Марина Володина"]'
        ),
        validation_alias="TASK_AUDITOR_EXCLUDE_USERS_JSON",
    )
    # Exclusive (smart process) expiry reminders
    exclusive_entity_type_id: int = Field(
        default=1080,
        validation_alias="EXCLUSIVE_ENTITY_TYPE_ID",
    )
    exclusive_end_date_field: str = Field(
        default="ufCrm20_1784712129125",
        validation_alias="EXCLUSIVE_END_DATE_FIELD",
    )
    exclusive_address_field: str = Field(
        default="ufCrm20_1784712031409",
        validation_alias="EXCLUSIVE_ADDRESS_FIELD",
    )
    exclusive_expiry_days: int = Field(
        default=7,
        validation_alias="EXCLUSIVE_EXPIRY_DAYS",
    )
    # Reminder milestones before end date: 7 days, then 3, then 1.
    exclusive_expiry_milestones_json: str = Field(
        default="[7, 3, 1]",
        validation_alias="EXCLUSIVE_EXPIRY_MILESTONES_JSON",
    )
    exclusive_skip_stage_ids_json: str = Field(
        default='["DT1080_26:SUCCESS", "DT1080_26:FAIL"]',
        validation_alias="EXCLUSIVE_SKIP_STAGE_IDS_JSON",
    )
    exclusive_expiry_catch_up: bool = Field(
        default=True,
        validation_alias="EXCLUSIVE_EXPIRY_CATCH_UP",
    )
    exclusive_notify_user_ids_json: str = Field(
        default="[32, 154, 378]",
        validation_alias="EXCLUSIVE_NOTIFY_USER_IDS_JSON",
    )
    exclusive_notify_from_user_id: int = Field(
        default=154,
        validation_alias="EXCLUSIVE_NOTIFY_FROM_USER_ID",
    )
    # Optional dedicated webhook for chat sends. Empty → B24_WEBHOOK_URL.
    exclusive_notify_webhook_url: str = Field(
        default="",
        validation_alias="EXCLUSIVE_NOTIFY_WEBHOOK_URL",
    )
    # Contact SOURCE_ID lock: revert changes made by brokers / ROPs.
    contact_source_lock_enabled: bool = Field(
        default=True,
        validation_alias="CONTACT_SOURCE_LOCK_ENABLED",
    )
    contact_source_lock_lookback_minutes: int = Field(
        default=30,
        validation_alias="CONTACT_SOURCE_LOCK_LOOKBACK_MINUTES",
    )
    contact_source_lock_notify: bool = Field(
        default=True,
        validation_alias="CONTACT_SOURCE_LOCK_NOTIFY",
    )
    contact_source_lock_notify_user_id: int = Field(
        default=0,
        validation_alias="CONTACT_SOURCE_LOCK_NOTIFY_USER_ID",
    )
    contact_source_lock_rop_position_substr_json: str = Field(
        default='["РОП", "Руководитель отдела продаж"]',
        validation_alias="CONTACT_SOURCE_LOCK_ROP_POSITION_SUBSTR_JSON",
    )
    contact_source_lock_exclude_user_ids_json: str = Field(
        default="[]",
        validation_alias="CONTACT_SOURCE_LOCK_EXCLUDE_USER_IDS_JSON",
    )
    contact_source_lock_exclude_names_json: str = Field(
        default='["Агентство Недвижимости", "Asterisk1 1"]',
        validation_alias="CONTACT_SOURCE_LOCK_EXCLUDE_NAMES_JSON",
    )
    # Deal SOURCE_ID lock (sellers funnel): same rules as contacts.
    deal_source_lock_enabled: bool = Field(
        default=True,
        validation_alias="DEAL_SOURCE_LOCK_ENABLED",
    )
    # Buyer funnel: auto-fill «Базовая ставка» + revert broker/ROP changes.
    buyer_base_rate_lock_enabled: bool = Field(
        default=True,
        validation_alias="BUYER_BASE_RATE_LOCK_ENABLED",
    )
    buyer_base_rate_csv: str = Field(
        default="data/Мотивация брокеров 3 кв 2026 - Мотивация брокеров 3 кв 2026.csv",
        validation_alias="BUYER_BASE_RATE_CSV",
    )
    # Buyer funnel: remind to fill OPPORTUNITY (Комиссия). No assignee change.
    buyer_commission_reminder_enabled: bool = Field(
        default=True,
        validation_alias="BUYER_COMMISSION_REMINDER_ENABLED",
    )
    buyer_commission_broker_interval_hours: float = Field(
        default=2.0,
        validation_alias="BUYER_COMMISSION_BROKER_INTERVAL_HOURS",
    )
    buyer_commission_rop_interval_hours: float = Field(
        default=1.0,
        validation_alias="BUYER_COMMISSION_ROP_INTERVAL_HOURS",
    )
    buyer_commission_deadline_hour: int = Field(
        default=19,
        validation_alias="BUYER_COMMISSION_DEADLINE_HOUR",
    )
    buyer_commission_remind_start_hour: int = Field(
        default=9,
        validation_alias="BUYER_COMMISSION_REMIND_START_HOUR",
    )
    buyer_commission_pool_user_id: int = Field(
        default=1,
        validation_alias="BUYER_COMMISSION_POOL_USER_ID",
    )
    buyer_commission_enforce_enabled: bool = Field(
        default=False,
        validation_alias="BUYER_COMMISSION_ENFORCE_ENABLED",
    )
    buyer_commission_timezone: str = Field(
        default="Europe/Moscow",
        validation_alias="BUYER_COMMISSION_TIMEZONE",
    )
    # Broker CRM rating
    broker_rating_period_days: int = Field(
        default=7,
        validation_alias="BROKER_RATING_PERIOD_DAYS",
    )
    # Fixed start date YYYY-MM-DD (MSK). If set — rating accumulates from this day.
    broker_rating_since: str = Field(
        default="",
        validation_alias="BROKER_RATING_SINCE",
    )
    broker_rating_weights_json: str = Field(
        default="{}",
        validation_alias="BROKER_RATING_WEIGHTS_JSON",
    )
    broker_rating_news_tag: str = Field(
        default="",
        validation_alias="BROKER_RATING_NEWS_TAG",
    )
    broker_rating_top_n: int = Field(
        default=10,
        validation_alias="BROKER_RATING_TOP_N",
    )
    broker_rating_sales_dept_ids_json: str = Field(
        default="",
        validation_alias="BROKER_RATING_SALES_DEPT_IDS_JSON",
    )
    broker_rating_exclude_user_ids_json: str = Field(
        default="[]",
        validation_alias="BROKER_RATING_EXCLUDE_USER_IDS_JSON",
    )
    # Quality audit: Spam / Non-target leads (leaked / wrong qualification)
    lead_quality_enabled: bool = Field(
        default=True,
        validation_alias="LEAD_QUALITY_ENABLED",
    )
    lead_quality_since: str = Field(
        default="2026-07-01",
        validation_alias="LEAD_QUALITY_SINCE",
    )
    lead_quality_chunk_size: int = Field(
        default=40,
        validation_alias="LEAD_QUALITY_CHUNK_SIZE",
    )
    client_state_enabled: bool = Field(
        default=False,
        validation_alias="CLIENT_STATE_ENABLED",
    )
    # Потолок объёма событий в одном запросе. Первый прогон по карточке шлёт
    # всю историю, и карточка с двумя десятками звонков иначе выходит за лимит
    # контекста модели.
    client_state_max_event_chars: int = Field(
        default=40_000,
        validation_alias="CLIENT_STATE_MAX_EVENT_CHARS",
    )
    client_state_transcript_retry_hours: float = Field(
        default=1.0,
        validation_alias="CLIENT_STATE_TRANSCRIPT_RETRY_HOURS",
    )

    @property
    def broker_rating_weights(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "component_weights": {
                "crm": 0.60,
                "portfolio": 0.20,
                "tasks": 0.10,
                "engagement": 0.10,
            },
            "severity_penalties": {
                "medium": 4,
                "high": 6,
                "very high": 10,
            },
            "shared_lead_penalty": 12,
            "pool_deal_penalty": 18,
            "tasks_neutral_score": 75,
            "tasks_closure_bonus": 5,
            "clean_day_bonus": 0.5,
            "clean_day_bonus_max": 5,
            "tier_green": 80,
            "tier_yellow": 50,
        }
        try:
            overrides = json.loads(self.broker_rating_weights_json)
        except Exception:
            return defaults
        if not isinstance(overrides, dict):
            return defaults
        merged = dict(defaults)
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged

    @property
    def broker_rating_sales_dept_ids(self) -> list[int]:
        raw = (self.broker_rating_sales_dept_ids_json or "").strip()
        if raw:
            try:
                return [int(x) for x in json.loads(raw)]
            except Exception:
                pass
        return self.owner_sales_dept_ids

    @property
    def broker_rating_exclude_user_ids(self) -> set[int]:
        try:
            return {int(x) for x in json.loads(self.broker_rating_exclude_user_ids_json)}
        except Exception:
            return set()

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
    def exclusive_skip_stage_ids(self) -> set[str]:
        try:
            return {str(x) for x in json.loads(self.exclusive_skip_stage_ids_json)}
        except Exception:
            return {"DT1080_26:SUCCESS", "DT1080_26:FAIL"}

    @property
    def exclusive_expiry_milestones(self) -> list[int]:
        try:
            values = [int(x) for x in json.loads(self.exclusive_expiry_milestones_json)]
            return sorted({v for v in values if v >= 0}, reverse=True)
        except Exception:
            return [7, 3, 1]

    @property
    def exclusive_notify_user_ids(self) -> list[int]:
        try:
            return [int(x) for x in json.loads(self.exclusive_notify_user_ids_json)]
        except Exception:
            return [32, 154, 378]

    @property
    def exclusive_notify_webhook(self) -> str:
        """Webhook used to send chat messages (must belong to FROM user)."""
        return (self.exclusive_notify_webhook_url or self.b24_webhook_url).strip()

    @property
    def contact_source_lock_rop_position_substr(self) -> list[str]:
        try:
            values = json.loads(self.contact_source_lock_rop_position_substr_json)
            return [str(x) for x in values if str(x).strip()]
        except Exception:
            return ["РОП", "Руководитель отдела продаж"]

    @property
    def contact_source_lock_exclude_user_ids(self) -> set[int]:
        try:
            return {int(x) for x in json.loads(self.contact_source_lock_exclude_user_ids_json)}
        except Exception:
            return set()

    @property
    def contact_source_lock_exclude_names(self) -> set[str]:
        try:
            return {str(x) for x in json.loads(self.contact_source_lock_exclude_names_json)}
        except Exception:
            return {"Агентство Недвижимости", "Asterisk1 1"}

    @property
    def contact_source_lock_notify_user(self) -> int:
        """Recipient for SOURCE lock alerts. 0 → ADMIN_USER_ID."""
        uid = int(self.contact_source_lock_notify_user_id or 0)
        return uid if uid > 0 else int(self.admin_user_id)

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
