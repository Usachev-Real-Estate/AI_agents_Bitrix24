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
    # Режим обработки RouterAI: flex / priority / пусто (обычный).
    #
    # flex — «обычно −50 %» по их документации, ценой скорости и без
    # гарантий по мощностям. Для QC это выгодная сделка: прогон идёт по
    # крону в полдень, занимает двадцать минут и никто его не ждёт. Модель
    # та же, ответ тот же — платим только временем планирования, а не
    # качеством разбора.
    #
    # Оговорка провайдера: при нехватке мощностей flex «вернёт ошибку,
    # средства не спишутся». У нас это llm_error, а пять подряд обрывают
    # прогон — то есть голый flex менял бы половину счёта на несостоявшийся
    # отчёт. Поэтому карточка, отказавшая на flex, тут же повторяется в
    # обычном режиме (см. analyze_deal), и llm_error засчитывается, только
    # если упали ОБЕ попытки. Настоящая авария — кончился баланс, лёг
    # провайдер — валит обе, и защита работает как прежде.
    llm_service_tier: str = Field(
        default="",
        validation_alias="LLM_SERVICE_TIER",
    )
    # Закреплённый провайдер RouterAI, тег из
    # GET /api/v1/models/{author}/{slug}/endpoints. Пусто — не закреплять.
    #
    # Зачем: RouterAI «распределяет нагрузку между провайдерами, отдавая
    # приоритет низкой цене», а неявный кэш живёт У ПРОВАЙДЕРА. Префикс,
    # прогретый в Google AI Studio, ничего не даёт запросу, ушедшему в
    # Vertex. Отсюда наш круглый ноль закэшированных токенов на 758
    # вызовов при постоянной части в 2452 токена — порог Gemini Flash
    # (1024) мы проходим с запасом, просто попадать было некуда.
    #
    # Какого закреплять — решает одна строка из ответа API: у Google Vertex
    # НЕТ параметра temperature, у Google AI Studio есть. Мы шлём 0.1
    # безусловно, и на Vertex это либо отказ, либо молча другая температура,
    # то есть другой разбор. Поэтому google-ai-studio, и не по вкусу, а по
    # единственному подходящему набору параметров.
    llm_provider: str = Field(
        default="",
        validation_alias="LLM_PROVIDER",
    )
    # Тариф RouterAI (₽ за 1М токенов) для google/gemini-3.7-flash. Нужен,
    # чтобы прогон писал в лог не абстрактные токены, а рубли. Меняется у
    # провайдера — правится здесь, без правки кода.
    #
    # Числа выведены из выгрузки RouterAI за 01.09.2026: 758 оплаченных
    # вызовов, 3 007 752 входных и 1 256 144 выходных токена, списано
    # 782,27 ₽. Ставка сходится до копейки и одна на все вызовы (в поле
    # input_tokens_cost по всей выгрузке ровно одно значение отношения).
    #
    # Прежние 40 и 202 занижали счёт РОВНО вдвое, и занижали молча: прогон
    # печатал 374 ₽ там, где провайдер списал 782. Врал не провайдер — врал
    # наш собственный отчёт, то есть цифра, по которой принимали решения об
    # экономии. Оценка холодного прогона в ~310 ₽ была отвергнута как
    # завышенная вдвое против «замеренных» 149 ₽ — а верной была она.
    llm_price_input: float = Field(
        default=84.2198, validation_alias="LLM_PRICE_INPUT",
    )
    # Ровно впятеро дороже входа — так у провайдера, а не по совпадению.
    # Размышления тарифицируются по ЭТОЙ же ставке и уже входят в
    # output_tokens, поэтому отдельной строкой в расчёт не идут: RouterAI
    # начисляет их отдельной строкой счёта, но по той же цене, и сумма
    # обеих строк равна output_tokens × цена выхода. Считаются они отдельно
    # только чтобы было видно, какая доля выхода уходит в них (01.09 — 58 %,
    # то есть 309 ₽ из 782).
    llm_price_output: float = Field(
        default=421.099, validation_alias="LLM_PRICE_OUTPUT",
    )
    # Цена перечитывания закэшированного входа — ровно десятая доля входа.
    # На странице модели у RouterAI показано «8 ₽», но там же вход показан
    # как «84» при фактических 84,2198, то есть цифра округлена для витрины.
    # Берём точную десятую: она сходится и с витриной, и с правилом
    # «чтение кеша вдесятеро дешевле входа».
    #
    # ВЫГРУЗКОЙ НЕ ПРОВЕРЕНО: за 01.09 провайдер вернул 0 закэшированных
    # токенов на все 758 вызовов, то есть постоянная часть запроса каждый
    # раз оплачивалась заново. Как только кэш заработает, ставку надо свести
    # с выгрузкой так же, как сведены вход и выход.
    llm_price_cache_read: float = Field(
        default=8.42198, validation_alias="LLM_PRICE_CACHE_READ",
    )
    # Цена ЗАПИСИ в кэш, со страницы модели RouterAI. Дешевле чтения и в
    # восемнадцать раз дешевле свежего входа — то есть кэш выгоден с первого
    # же повторения, а не с десятого.
    #
    # До 01.09 этой строки в расчёте не было вовсе. Пока кэш не работает,
    # она ничего не меняет; как только заработает — без неё счёт занижался
    # бы снова, ровно тем же способом, каким его занижал старый тариф.
    #
    # Записанные токены считаются ЧАСТЬЮ входа и тарифицируются вместо
    # полной цены, а не сверх неё (см. estimate_cost). Это допущение: своей
    # выгрузки с кэшем у нас пока нет, и первую же надо будет с ним сверить.
    llm_price_cache_write: float = Field(
        default=4.68, validation_alias="LLM_PRICE_CACHE_WRITE",
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
    # «Общие лиды»: напоминание квалифицировать до дедлайна. Ничего не мутирует.
    # Выключено по умолчанию: рассылка, которая начинает ходить людям сразу
    # после выкатки, — это рассылка, которую никто не согласовывал.
    shared_lead_reminder_enabled: bool = Field(
        default=False,
        validation_alias="SHARED_LEAD_REMINDER_ENABLED",
    )
    shared_lead_reminder_start_hour: int = Field(
        default=9,
        validation_alias="SHARED_LEAD_REMINDER_START_HOUR",
    )
    shared_lead_reminder_deadline_hour: int = Field(
        default=14,
        validation_alias="SHARED_LEAD_REMINDER_DEADLINE_HOUR",
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
    # Сколько дней тишины по карточке означают, что о ней просто забыли.
    # Нормы этапов измеряются днями, и на их фоне сделка, где месяц не было
    # ни звонка, ни комментария, — не отставание от каденса, а другой
    # разговор: возвращать клиента или закрывать сделку.
    client_state_abandoned_days: float = Field(
        default=30.0,
        validation_alias="CLIENT_STATE_ABANDONED_DAYS",
    )
    client_state_transcript_retry_hours: float = Field(
        default=1.0,
        validation_alias="CLIENT_STATE_TRANSCRIPT_RETRY_HOURS",
    )

    # --- Выгрузка досье по сделкам (src/dossier.py) ---
    # Куда складывать jsonl, когда Drive не настроен или недоступен. Каталог
    # внутри data/: это единственный том, который переживает пересборку
    # образа, и тот же, где лежат базы.
    dossier_dir: str = Field(
        default="data/dossier",
        validation_alias="DOSSIER_DIR",
    )
    # Сколько расшифровок дозапрашивать за прогон.
    #
    # Число взято по замеру портфеля (18.09): 1645 звонков по 1289 открытым
    # сделкам, из них длиннее минуты 448. Отсечка по длительности снимает
    # 1197 обращений ещё до кэша, и 500 покрывают ВЕСЬ остаток за один
    # проход. Это и есть условие: полный прогон с холодным кэшем обязан
    # уложиться в бюджет целиком.
    #
    # Иначе предохранитель превращается в источник вранья. Незабранная
    # расшифровка получает статус deferred, а он по правилу README означает
    # «данных недостаточно» — и полтораста карточек на ровном месте
    # оказались бы необъяснимыми, причём именно в первом прогоне, по
    # которому судят обо всей выгрузке.
    dossier_transcript_budget: int = Field(
        default=500,
        validation_alias="DOSSIER_TRANSCRIPT_BUDGET",
    )
    # Сколько звонков за прогон класть в очередь на запуск расшифровки.
    # Очередь отстреливается руками, и сотня — это объём, который человек
    # проходит за один присест. Больше положить значит не ускорить, а
    # спрятать хвост, до которого никто не дойдёт.
    dossier_launch_budget: int = Field(
        default=100,
        validation_alias="DOSSIER_LAUNCH_BUDGET",
    )
    # Сколько файлов выгрузки хранить (и локально, и в папке Drive).
    dossier_keep_files: int = Field(
        default=30,
        validation_alias="DOSSIER_KEEP_FILES",
    )
    # Ключ сервисного аккаунта Google и папка назначения. Пусто — выгрузка
    # остаётся в dossier_dir, и прогон об этом сообщает. Отсутствие Drive
    # роняет доставку, но не сбор: файлы уже собраны и лежат на диске.
    gdrive_credentials_file: str = Field(
        default="",
        validation_alias="GDRIVE_CREDENTIALS_FILE",
    )
    gdrive_folder_id: str = Field(
        default="",
        validation_alias="GDRIVE_FOLDER_ID",
    )

    # --- Аналитическая витрина (ETL) ---
    analytics_db_path: str = Field(
        default="data/analytics.db",
        validation_alias="ANALYTICS_DB_PATH",
    )
    analytics_months_back: int = Field(
        default=12,
        validation_alias="ANALYTICS_MONTHS_BACK",
    )
    analytics_rate_limit_rps: float = Field(
        default=2.0,
        validation_alias="ANALYTICS_RATE_LIMIT_RPS",
    )
    # Инкремент по >=DATE_MODIFY теряет записи, изменённые в ту же секунду,
    # что и watermark, и во время самого прогона. Читаем с перекрытием —
    # upsert делает повтор безвредным.
    analytics_etl_overlap_minutes: int = Field(
        default=5,
        validation_alias="ANALYTICS_ETL_OVERLAP_MINUTES",
    )
    # Валюта, в которой считаются денежные метрики дашборда. Складывать
    # доллары с рублями нельзя, а курса у витрины нет: она хранит сумму ровно
    # так, как её ввели в Bitrix. Сделки в другой валюте в суммы не входят и
    # показываются отдельным счётчиком.
    analytics_base_currency: str = Field(
        default="RUB",
        validation_alias="ANALYTICS_BASE_CURRENCY",
    )
    # Поле суммы по воронкам: {"0": "UF_CRM_XXX"}. Пусто → OPPORTUNITY.
    analytics_amount_field_by_category_json: str = Field(
        default="{}",
        validation_alias="ANALYTICS_AMOUNT_FIELD_BY_CATEGORY_JSON",
    )
    # Переопределение семантики стадии: {"C18:UC_RUCRAH": "won"}.
    analytics_stage_semantic_overrides_json: str = Field(
        default="{}",
        validation_alias="ANALYTICS_STAGE_SEMANTIC_OVERRIDES_JSON",
    )

    # --- Клиентский слой ---
    # Своя база, а не таблицы в витрине: у витрины один писатель, и
    # сентябрьские «database is locked» стоили ночного бюджета ровно
    # потому, что к нему подсаживались соседи.
    clients_db_path: str = Field(
        default="data/clients.db",
        validation_alias="CLIENTS_DB_PATH",
    )

    # --- Веб-дашборд ---
    dashboard_secret_key: str = Field(
        default="",
        validation_alias="DASHBOARD_SECRET_KEY",
    )
    dashboard_host: str = Field(
        default="127.0.0.1",
        validation_alias="DASHBOARD_HOST",
    )
    dashboard_port: int = Field(
        default=8080,
        validation_alias="DASHBOARD_PORT",
    )
    dashboard_base_path: str = Field(
        default="/dashboard",
        validation_alias="DASHBOARD_BASE_PATH",
    )
    # --- Утренний дайджест «Пульса» ---
    # Выключен по умолчанию: новая рассылка, которая начинает ходить людям
    # сразу после выкатки, — это рассылка, которую никто не согласовывал.
    pulse_digest_enabled: bool = Field(
        default=False,
        validation_alias="PULSE_DIGEST_ENABLED",
    )
    # Кому уходит сводка по компании и отделы, которые разбирает владелец
    # отчёта. Отдельно от ADMIN_USER_ID: на том висят технические
    # уведомления о падении задач, а ещё он исключается из рейтинга брокеров
    # и из-под замка поля «Источник». Живой человек, назначенный туда ради
    # одной рассылки, тихо выпал бы из двух проверок. Пусто — ADMIN_USER_ID.
    pulse_digest_to: int = Field(
        default=0,
        validation_alias="PULSE_DIGEST_TO",
    )
    # Внешний адрес дашборда для ссылки в сообщении. Пустой — ссылки не будет:
    # неверный адрес хуже отсутствующего, он выглядит рабочим.
    pulse_digest_url: str = Field(
        default="",
        validation_alias="PULSE_DIGEST_URL",
    )
    # Воронки, сделки которых идут в план. Решение агентства от 07.09:
    # только «Покупатели» (18).
    #
    # Настройкой, а не константой: воронки в портале заводят и закрывают, и
    # день, когда план начнёт считаться по двум, наступит раньше, чем
    # следующая выкатка. Пустой список означал бы «все воронки» — это
    # молчаливое расширение плана, поэтому пустым он не бывает: разбор ниже
    # падает обратно на [18], а не на «всё подряд».
    # Стадии, на которых простой не считается простоем: {"воронка": [стадии]}.
    #
    # Норма стадии — 75-й перцентиль ЗАВЕРШЁННЫХ интервалов, то есть время
    # тех карточек, которые со стадии ушли. Для «Поиска клиента» у продавцов
    # это ловушка: объект в рекламе живёт там месяцами, уходят первыми самые
    # быстрые, и норма считается по ним. Она выходит короткой, а всё
    # честно рекламируемое оказывается «зависшим».
    #
    # Пока в витрине нет действий — звонков и встреч, — отличить работу от
    # забвения на этой стадии нечем, и лучше молчать, чем назвать виноватыми
    # не тех.
    # Воронки, где встречи заводят в портал. Со собственниками их не ведут
    # (ответ агентства 08.09): показывать «просрочено 18» там значит мерить
    # процесс, которого нет, и обвинять брокеров в отсутствии записи, которую
    # никто не просил делать.
    analytics_meeting_funnels_json: str = Field(
        default="[18]",
        validation_alias="ANALYTICS_MEETING_FUNNELS_JSON",
    )
    analytics_stuck_exclude_stages_json: str = Field(
        default='{"0": ["UC_FADPBF"]}',
        validation_alias="ANALYTICS_STUCK_EXCLUDE_STAGES_JSON",
    )
    # Чат, куда уходит ежедневный разбор воронки. Отдельно от личных сводок:
    # план-факт — разговор с конкретным РОПом, а движение сделок общее, и
    # обсуждать его удобнее там, где его видят все сразу.
    #
    # Ноль — разбор остаётся в личной сводке владельца отчёта, как было.
    pulse_events_chat_id: int = Field(
        default=0,
        validation_alias="PULSE_EVENTS_CHAT_ID",
    )
    pulse_category_ids_json: str = Field(
        default='[18]',
        validation_alias="PULSE_CATEGORY_IDS_JSON",
    )
    # --- Рубеж безубыточности ---
    # Витрина знает только доходы: расходы и доля, остающаяся компании после
    # выплат брокерам и налога, живут в отчёте руководству. Обе берутся
    # оттуда и обновляются вместе с ним — примерно раз в квартал.
    #
    # Ноль в любой из двух настроек выключает рубеж целиком. Это осознанно:
    # выдуманный порог безубыточности хуже отсутствующего, потому что по
    # нему принимают решения о людях.
    pulse_monthly_costs: float = Field(
        default=0.0,
        validation_alias="PULSE_MONTHLY_COSTS",
    )
    # Доля валовой комиссии, остающаяся компании после выплат брокерам,
    # налога и выплат РОП. В августе 2026 вышло 0.586.
    pulse_net_share: float = Field(
        default=0.0,
        validation_alias="PULSE_NET_SHARE",
    )
    dashboard_session_ttl_hours: int = Field(
        default=12,
        validation_alias="DASHBOARD_SESSION_TTL_HOURS",
    )
    dashboard_session_idle_hours: int = Field(
        default=2,
        validation_alias="DASHBOARD_SESSION_IDLE_HOURS",
    )
    # Отключать только для локальной отладки по http: без Secure кука уедет
    # по незашифрованному каналу.
    dashboard_cookie_secure: bool = Field(
        default=True,
        validation_alias="DASHBOARD_COOKIE_SECURE",
    )
    dashboard_login_max_attempts: int = Field(
        default=5,
        validation_alias="DASHBOARD_LOGIN_MAX_ATTEMPTS",
    )
    dashboard_login_lockout_minutes: int = Field(
        default=15,
        validation_alias="DASHBOARD_LOGIN_LOCKOUT_MINUTES",
    )

    # --- Афина CRM: объекты для дашборда ---
    # Read-only витрина Афины: тот же ключ, что у публичного каталога, но
    # другой контракт — статусы, причины снятия и даты событий.
    # Префикс afina_api_, а не afina_: «ID Афины» — это поле сделки в Bitrix
    # (SELLERS_AFINA_UF), и путать его с внешним API не надо.
    afina_api_base_url: str = Field(
        default="https://afina-crm.ru",
        validation_alias="AFINA_API_BASE_URL",
    )
    # Пусто → раздел «Объекты» не показывается и в навигацию не попадает.
    # Так дашборд не рисует пустую страницу с ошибкой там, где интеграцию
    # просто не настроили.
    afina_api_key: str = Field(
        default="",
        validation_alias="AFINA_API_KEY",
    )
    # Дашборд синхронный: страница ждёт ответ Афины. Долгий таймаут держал бы
    # воркер uvicorn занятым, поэтому он заметно короче хардкодных 60 с
    # фоновых задач.
    afina_api_timeout_seconds: float = Field(
        default=10.0,
        validation_alias="AFINA_API_TIMEOUT_SECONDS",
    )
    # Потолок страницы у Афины — 100. Просить больше значит получить 422.
    afina_api_page_size: int = Field(
        default=50,
        validation_alias="AFINA_API_PAGE_SIZE",
    )
    # Перевод отдела Битрикса (числовой ID) в название отдела Афины. Без него
    # РОП раздела «Объекты» не видит вовсе.
    #
    # Составлять руками приходится потому, что справочники разные и по имени
    # не сходятся: в Битриксе отдел зовётся «Трофимова», в Афине — «Отдел
    # Трофимовой». Автоматически подобрать пару значило бы угадывать падеж, а
    # ошибка здесь — это чужой отдел на экране РОПа. Готовую строку печатает
    # scripts/afina_departments.py.
    #
    #   AFINA_DEPARTMENT_MAP_JSON={"44": "Отдел Трофимовой", "50": "Отдел Волковой"}
    afina_department_map_json: str = Field(
        default="",
        validation_alias="AFINA_DEPARTMENT_MAP_JSON",
    )

    @property
    def afina_department_map(self) -> dict[int, str]:
        """Отдел Битрикса → отдел Афины. Кривой JSON = пустая карта.

        Молча пустая карта здесь безопаснее исключения при старте: раздел
        просто не откроется РОПам, а дашборд останется жив. Обратное — падение
        всего сервиса из-за запятой в необязательной настройке.
        """
        try:
            raw = json.loads(self.afina_department_map_json)
            return {int(k): str(v).strip() for k, v in raw.items() if str(v).strip()}
        except Exception:
            return {}

    @property
    def analytics_amount_field_by_category(self) -> dict[int, str]:
        """Поле суммы по воронкам. Пусто → OPPORTUNITY."""
        try:
            raw = json.loads(self.analytics_amount_field_by_category_json)
            return {int(k): str(v) for k, v in raw.items() if str(v).strip()}
        except Exception:
            return {}

    @property
    def analytics_stage_semantic_overrides(self) -> dict[str, str]:
        """stage_id → in_progress|won|lost поверх вывода по суффиксу."""
        try:
            raw = json.loads(self.analytics_stage_semantic_overrides_json)
            return {str(k): str(v).strip().lower() for k, v in raw.items()}
        except Exception:
            return {}

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
    def analytics_meeting_funnels(self) -> list[int]:
        try:
            return [int(x) for x in json.loads(self.analytics_meeting_funnels_json)]
        except Exception:
            return [18]

    @property
    def analytics_stuck_exclude_stages(self) -> dict[int, set[str]]:
        """Стадии вне подсчёта простоя, по воронкам. Мусор — как будто пусто."""
        raw = (self.analytics_stuck_exclude_stages_json or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return {
                int(category): {str(stage) for stage in stages}
                for category, stages in parsed.items()
            }
        except Exception:
            return {}

    @property
    def pulse_category_ids(self) -> list[int]:
        """Воронки плана. Никогда не пустой список — см. поле выше."""
        raw = (self.pulse_category_ids_json or "").strip()
        if raw:
            try:
                parsed = [int(x) for x in json.loads(raw)]
                if parsed:
                    return parsed
            except Exception:
                pass
        return [18]

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
