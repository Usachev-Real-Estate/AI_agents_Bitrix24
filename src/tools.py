"""Bitrix24 REST tools via fast_bitrix24."""

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from fast_bitrix24 import Bitrix
from langchain_core.tools import tool

from config import BX_EXECUTOR, get_settings

logger = logging.getLogger(__name__)

MAX_TIMELINE_WORKERS = 10

# Поля сделки для аудита воронки «Покупатели» / «Продавцы» (портал b24-po7frr)
DEAL_AUDIT_UF_FIELD_CODES = (
    "UF_CRM_1659375809326",  # Дата встречи
    "UF_CRM_1774361998551",  # Результат показа
    "UF_CRM_1780911079",     # ID объекта Афины (сделки; у лидов другой код)
)
DEAL_AUDIT_UF_LABELS: dict[str, str] = {
    "UF_CRM_1659375809326": "Дата встречи",
    "UF_CRM_1774361998551": "Результат показа",
    "UF_CRM_1780911079": "ID Афины",
}
SELLERS_AFINA_UF = "UF_CRM_1780911079"  # сделка; лид = UF_CRM_1780911032

# Воронка «Покупатели» (category_id=18): stage_id → правило аудита
# Актуальная воронка: Подбор → Первый показ → Повторный показ → Офер → …
BUYERS_STAGE_AUDIT_RULE: dict[str, int] = {
    "C18:NEW": 2,           # Подбор — каденс 2 дня; плюс buyer_podbor_stale >7 дн. на стадии
    "C18:UC_UFPFKK": 3,     # Первый показ — живое дело, дедлайн ≤ 14 дн. от входа
    "C18:UC_DVW1P9": 3,     # Повторный показ — как первый
    "C18:UC_8Z3SP6": 6,     # Офер — комментарий брокера/РОПа >= 30 символов
    "C18:LOSE": 5,          # Отложенный спрос — живое запланированное дело
    "C18:APOLOGY": 7,       # Сделка проиграна — комментарий с причиной
    "C18:UC_2ZBA0G": 8,     # Агент — комментарий
}
BUYER_PODBOR_MAX_DAYS = 7
# Ограничение горизонта планируемого дела для показов:
# по запросу бизнеса не применяется на этапе «Первый показ».
BUYER_SHOW_MAX_DAYS_FROM_STAGE = 14
BUYER_SHOW_HORIZON_STAGE_IDS = frozenset({
    "C18:UC_DVW1P9",  # Повторный показ
})
# «Комментариев ответственного не было» — sentinel из
# _days_since_last_comment_by_authors. Не выводится пользователю.
NO_COMMENT_DAYS = 999
BUYER_OFER_MIN_COMMENT_LEN = 30
BUYER_REASON_MIN_COMMENT_LEN = 5
# Брокер пишет причину, затем сразу меняет стадию: CREATED комментария
# на секунды раньше crm.stagehistory. Без люфта такие карточки ложно флажатся.
STAGE_COMMENT_LOOKBACK = timedelta(hours=24)
BUYERS_LOST_STAGE_ID = "C18:APOLOGY"
# Закрытые / служебные стадии — без аудита (Задаток/Сделка — чат, позже)
BUYERS_SKIP_AUDIT_STAGES = frozenset({
    "C18:UC_RUCRAH",  # Задаток
    "C18:UC_8X12HI",  # Сделка
    "C18:WON",        # Договор закрыт
})
BUYERS_STAGE_NAMES: dict[str, str] = {
    "C18:NEW": "Подбор",
    "C18:UC_UFPFKK": "Первый показ",
    "C18:UC_DVW1P9": "Повторный показ",
    "C18:UC_8Z3SP6": "Офер",
    "C18:UC_RUCRAH": "Задаток",
    "C18:UC_8X12HI": "Сделка",
    "C18:WON": "Договор закрыт",
    "C18:LOSE": "Отложенный спрос",
    "C18:APOLOGY": "Сделка проиграна",
    "C18:UC_2ZBA0G": "Агент",
}

# Воронка «Продавцы» (category_id=0)
SELLERS_PAID_SOURCE_IDS = frozenset({"24", "25", "26"})  # legacy labels only
SELLERS_PAID_SOURCE_NAMES: dict[str, str] = {
    "24": "КЦ - 5%",
    "25": "Диспозл 10%",
    "26": "Диспозл 5%",
}
SELLER_STAGE_NEW = "NEW"  # Назначение встречи
SELLER_STAGE_DEFERRED = "LOSE"  # Отложенная продажа
SELLER_STAGE_NEGOTIATIONS = "UC_KEOOG8"
SELLER_STAGE_LOST = "APOLOGY"
SELLER_NEGOTIATIONS_MAX_DAYS = 14
SELLERS_LOST_STAGE_ID = "APOLOGY"
SELLERS_STAGE_NAMES: dict[str, str] = {
    "NEW": "Назначение встречи",
    "FINAL_INVOICE": "Подготовка объекта в рекламу",
    "UC_A94BGF": "Закрытая продажа (На сайт)",
    "UC_FADPBF": "Поиск клиента",
    "UC_KEOOG8": "Переговоры",
    "WON": "Договор закрыт",
    "LOSE": "Отложенная продажа",
    "APOLOGY": "Сделка проиграна",
}
# Свежесть касания (комментарий или дело брокера/РОП) по этапам
SELLER_STAGE_SEARCH_CLIENT = "UC_FADPBF"  # Поиск клиента — без аудита
SELLERS_STAGE_CADENCE: dict[str, timedelta] = {
    "NEW": timedelta(hours=24),
    "UC_KEOOG8": timedelta(days=3),
    "FINAL_INVOICE": timedelta(days=7),
    "UC_A94BGF": timedelta(days=7),
}
SELLERS_SKIP_AUDIT_STAGES = frozenset({"WON", SELLER_STAGE_SEARCH_CLIENT})
# Комментарий (не дело) для каденса: Переговоры, Закрытая продажа
SELLERS_COMMENT_ONLY_STAGES = frozenset({
    SELLER_STAGE_NEGOTIATIONS,
    "UC_A94BGF",
})
SELLERS_AFINA_REQUIRED_STAGES = frozenset({"UC_A94BGF"})
# «Поиск клиента» никогда не уходит в Общую базу
SELLERS_NEVER_MOVE_TO_GENERAL_BASE = frozenset({SELLER_STAGE_SEARCH_CLIENT})
SELLER_MEETING_DEADLINE = timedelta(hours=24)
SELLER_MEETING_REMIND_BEFORE = timedelta(hours=2)
# Стадии, для которых нужна дата входа (crm.stagehistory.list)
STAGES_NEEDING_HISTORY = frozenset({
    "C18:NEW",
    "C18:UC_UFPFKK",
    "C18:UC_DVW1P9",
    "C18:UC_8Z3SP6",
    "C18:APOLOGY",
    "C18:UC_2ZBA0G",
    SELLER_STAGE_NEGOTIATIONS,
    SELLER_STAGE_LOST,
})
STAGE_HISTORY_OWNER_CHUNK = 50
LEAD_NEW_MOVE_HOURS = 24
# Воронка «Общая база» (category 26): Продавцы / Покупатели
GENERAL_BASE_CATEGORY_ID = 26
GENERAL_BASE_SELLERS_STAGE_ID = "C26:NEW"  # Общая база → Продавцы
GENERAL_BASE_BUYERS_STAGE_ID = "C26:PREPARATION"  # Общая база → Покупатели
GENERAL_BASE_MOVE_WARNING = (
    "В случае невыполнения требования регламента сделка будет перенесена "
    "в воронку «Общая база»."
)
# Констатация вместо предупреждения: если перенос уже выполнен, будущее время
# вводит брокера в заблуждение — он считает, что время ещё есть.
GENERAL_BASE_MOVED_NOTICE = (
    "Сделка перенесена в воронку «Общая база»."
)
# После переноса в Общую базу: 2 дня на живое дело или комментарий с планом
GENERAL_BASE_PLAN_DEADLINE = timedelta(days=2)
GENERAL_BASE_RULE_ACTION = (
    "Запланировать дело или написать комментарий с планом дальнейших действий"
)
# Комментарий «с дальнейшими действиями»: не «связаться с клиентом», а конкретный шаг
_GB_PLAN_RE = re.compile(
    r"встреч|показ|договор|документ|оценк|выезд|замер|"
    r"фотограф|фотосъем|сделать фото|"
    r"реклам|подпис|"
    r"\bключ(и|ей|а|ом|ами)?\b|"
    r"задаток|эксклюзив|офер|осмотр|назнач|"
    r"приед|приеду|выеха|подготов|"
    r"отправить|отправлю|отправим|"
    r"соглас|презентац|коммерческ|\bкп\b|бронь|эксклюз|"
    r"подборк|выставить|пробив|обсуд",
    re.I,
)
_GB_NOT_A_PLAN_RE = re.compile(
    r"отказал\w*\s+\w*\s*встреч|не готов\w*\s+к\s+встреч|"
    r"был[аи]?\s+на\s+встреч|не договорил",
    re.I,
)
DEAL_MOVE_RULES_SELLERS = frozenset({
    "seller_stage_stale",
    "seller_deferred_no_activity",
    "seller_negotiations_max",
})
DEAL_MOVE_RULES_BUYERS = frozenset({
    "buyer_stage_2",
    "buyer_podbor_stale",
    "buyer_stage_3",
    "buyer_stage_5",
    "buyer_agent_no_comment",
})

# Что сделать — текст рядом с нарушением в отчёте отдела
SELLER_RULE_ACTIONS: dict[str, str] = {
    "seller_stage_stale": "Добавить комментарий или дело в карточку сделки",
    "seller_deferred_no_activity": "Запланировать дело в карточке сделки",
    "seller_negotiations_max": "Перевести сделку с «Переговоры» (лимит 14 дней)",
    "seller_lost_no_reason": "Добавить комментарий брокера/РОПа или дело в карточку",
    "seller_afina_id_missing": "Заполнить поле «ID Афины» в карточке сделки",
}

# Стадии лидов (ENTITY_ID=STATUS), портал b24-po7frr
LEAD_STATUS_NEW = "NEW"
LEAD_STATUS_CONVERTED = "CONVERTED"  # «Квалифицирован»
LEAD_STATUS_JUNK = "JUNK"  # «Спам»
LEAD_STATUS_NECELEVOY = "UC_A7I8DK"  # «Нецелевой»
LEAD_STATUS_SHARED = "1"  # «Общие Лиды» — общая очередь (b24-po7frr)
LEAD_STATUS_AGENT = "UC_52VG81"  # «Агент»

# Стадии без LLM-аудита (rule_2 / rule_3)
LEAD_SKIP_LLM_STATUS_IDS = frozenset({
    LEAD_STATUS_NEW,
    LEAD_STATUS_CONVERTED,
    LEAD_STATUS_SHARED,
    LEAD_STATUS_AGENT,
    "WON",
    "LOSE",
})

# Стадии без проверки пропущенных звонков
LEAD_SKIP_MISSED_CALL_STATUS_IDS = frozenset({
    LEAD_STATUS_CONVERTED,
    LEAD_STATUS_SHARED,
})


def _lead_status_id(lead: dict[str, Any]) -> str:
    """Normalize lead status_id from collector or API record."""
    return _clean_str(lead.get("status_id") or lead.get("STATUS_ID")).upper()


def _is_lead_spam_status(status_id: str) -> bool:
    """True if lead is in spam stage (JUNK on portal, or legacy SPAM code)."""
    return status_id == LEAD_STATUS_JUNK or "SPAM" in status_id


# Прогрессбар клиента портала. В ручном прогоне он полезен, в cron — вреден:
# рисуется на КАЖДЫЙ запрос, а полная выгрузка досье это две с половиной
# тысячи запросов, то есть столько же строк в общий cron.log, где их никто
# не ищет. Задача, которой он не нужен, гасит его себе одной строкой
# (см. dossier.main); по умолчанию поведение прежнее.
BX_VERBOSE = True


def _get_bitrix() -> Bitrix:
    """Create Bitrix REST client from current settings.

    Returns:
        Configured Bitrix instance.
    """
    return Bitrix(get_settings().b24_webhook_url, verbose=BX_VERBOSE)


def _bx_get_all_sync(method: str, params: dict[str, Any]) -> Any:
    """Run Bitrix get_all in a thread when called from asyncio.

    fast_bitrix24 returns pending tasks inside a running event loop.

    Args:
        method: REST API method name.
        params: Request parameters.

    Returns:
        get_all result (typically a list of records).
    """

    def _run() -> Any:
        return _get_bitrix().get_all(method, params)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()

    return BX_EXECUTOR.submit(_run).result()


def _as_list(payload: Any) -> list[dict[str, Any]]:
    """Normalize API list responses to a list of dicts.

    Args:
        payload: Raw API response.

    Returns:
        List of record dictionaries.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        for key in ("result", "comments", "calls", "leads", "tasks"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
            if isinstance(inner, dict):
                nested_items = inner.get("items")
                if isinstance(nested_items, list):
                    return [item for item in nested_items if isinstance(item, dict)]
                return [item for item in inner.values() if isinstance(item, dict)]
    return []


def _clean_str(value: Any) -> str:
    """Clean string from encoding artifacts."""
    s = str(value) if value is not None else ""
    return s.encode("utf-8", errors="replace").decode("utf-8")


def _build_deal_uf_fields(deal: dict[str, Any]) -> dict[str, Any]:
    """Map deal UF_* values to human-readable labels for the analyst prompt."""
    labeled: dict[str, Any] = {}
    for code, label in DEAL_AUDIT_UF_LABELS.items():
        labeled[label] = _normalize_uf_value(deal.get(code))
    return labeled


def _afina_id_filled(deal: dict[str, Any]) -> bool:
    """True when deal has a non-empty «ID Афины» UF value."""
    uf_fields = deal.get("uf_fields") if isinstance(deal.get("uf_fields"), dict) else {}
    labeled = _normalize_uf_value(uf_fields.get("ID Афины") if uf_fields else None)
    if labeled is not None:
        return True
    return _normalize_uf_value(deal.get(SELLERS_AFINA_UF) or deal.get("afina_id")) is not None


def _fetch_funnel_stage_names(category_id: int) -> dict[str, str]:
    """Load stage_id → Russian name map for a deal category.

    Uses crm.status.list: default funnel is ENTITY_ID=DEAL_STAGE,
    other funnels are DEAL_STAGE_{categoryId}.
    """
    entity_id = "DEAL_STAGE" if int(category_id) == 0 else f"DEAL_STAGE_{int(category_id)}"
    try:
        stages = _bx_get_all_sync(
            "crm.status.list",
            {"filter": {"ENTITY_ID": entity_id}},
        )
        return {
            _clean_str(item.get("STATUS_ID")): _clean_str(item.get("NAME"))
            for item in (stages or [])
            if isinstance(item, dict) and item.get("STATUS_ID")
        }
    except Exception:
        logger.warning(
            "Failed to load stage names for category_id=%s entity_id=%s",
            category_id,
            entity_id,
        )
        return {}


def _buyers_audit_rule(stage_id: str) -> int | None:
    """Return audit rule number (1–5) for buyers funnel stage, or None."""
    if stage_id in BUYERS_SKIP_AUDIT_STAGES:
        return None
    return BUYERS_STAGE_AUDIT_RULE.get(stage_id)


def _normalize_uf_value(value: Any) -> Any:
    """Treat Bitrix empty sentinels as missing UF values."""
    if value in (None, "", [], False):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped == "0" or stripped.startswith("0000-00-00"):
            return None
        return stripped
    return value


def _parse_datetime(value: Any) -> datetime | None:
    """Parse Bitrix / ISO datetime strings to timezone-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(" ", "T", 1) if " " in text and "T" not in text else text
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt, length in (
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d", 10),
    ):
        try:
            parsed = datetime.strptime(text[:length], fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _days_between(start: Any, end: datetime) -> float:
    """Days from start timestamp to end (24h = 1 day)."""
    start_dt = _parse_datetime(start)
    if start_dt is None:
        return 0.0
    delta = end - start_dt.astimezone(timezone.utc)
    return max(delta.total_seconds() / 86400.0, 0.0)


def _chunks(values: list[int], size: int) -> list[list[int]]:
    """Split a list of ints into chunks of `size`."""
    if size <= 0:
        return [values]
    return [values[i:i + size] for i in range(0, len(values), size)]


def _stage_entered_dt(deal: dict[str, Any], now: datetime) -> datetime:
    """Datetime the deal entered its current stage; fallback DATE_CREATE then now."""
    entered = _parse_datetime(deal.get("stage_entered_at"))
    if entered is not None:
        return entered
    created = _parse_datetime(deal.get("date_create"))
    if created is not None:
        return created
    return now


def _latest_stage_entered_at(
    rows: list[dict[str, Any]],
    deal_id: int,
    stage_id: str,
) -> datetime | None:
    """Latest CREATED_TIME in stagehistory rows for deal_id + stage_id."""
    matched: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _coerce_int(row.get("OWNER_ID") or row.get("owner_id")) != deal_id:
            continue
        row_stage = _clean_str(row.get("STAGE_ID") or row.get("stage_id"))
        if row_stage != stage_id:
            continue
        created = _parse_datetime(row.get("CREATED_TIME") or row.get("created_time"))
        if created is not None:
            matched.append(created)
    return max(matched) if matched else None


def _author_comment_meets(
    timeline: list[dict[str, Any]],
    allowed_author_ids: set[int],
    min_len: int,
    since: datetime | None = None,
) -> bool:
    """True if a broker/ROP timeline comment is long enough (optionally after since).

    ``since`` is the stage-entry time. Comments up to STAGE_COMMENT_LOOKBACK
    before that still count: brokers typically comment, then move the stage.
    """
    threshold: datetime | None = None
    if since is not None:
        threshold = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        threshold = threshold - STAGE_COMMENT_LOOKBACK
    for item in timeline:
        if not isinstance(item, dict):
            continue
        if _coerce_int(item.get("author_id")) not in allowed_author_ids:
            continue
        text = _strip_lead_markup(str(item.get("comment") or ""))
        if len(text) < min_len:
            continue
        if threshold is None:
            return True
        created = _parse_datetime(item.get("created"))
        if created is None:
            # Дату не разобрать — доказать, что комментарий после входа на стадию,
            # нельзя. Не засчитываем, но делаем сбой заметным.
            logger.warning(
                "Timeline comment without parsable date: author=%s created=%r",
                item.get("author_id"),
                item.get("created"),
            )
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= threshold:
            return True
    return False


def _days_since_last_comment_by_authors(
    timeline: list[dict[str, Any]],
    current: datetime,
    allowed_author_ids: set[int],
) -> int:
    """Days since last comment from allowed authors, or NO_COMMENT_DAYS if none."""
    authored = [
        item for item in timeline
        if _coerce_int(item.get("author_id")) in allowed_author_ids
        and str(item.get("comment") or "").strip()
    ]
    if not authored:
        return NO_COMMENT_DAYS
    dated = [
        (parsed, item)
        for item, parsed in (
            (item, _parse_datetime(item.get("created"))) for item in authored
        )
        if parsed is not None
    ]
    if not dated:
        return NO_COMMENT_DAYS
    # Сравниваем datetime, а не ISO-строки: разные смещения (+03:00 / +00:00)
    # сортируются лексикографически неверно.
    created = max(parsed for parsed, _ in dated)
    return int(_days_between(created, current))


def _activity_author_ids(activity: dict[str, Any]) -> set[int]:
    """User IDs linked to an activity (responsible / author)."""
    ids: set[int] = set()
    for key in ("RESPONSIBLE_ID", "AUTHOR_ID", "responsible_id", "author_id"):
        uid = _coerce_int(activity.get(key))
        if uid:
            ids.add(uid)
    return ids


def _activity_touch_datetime(activity: dict[str, Any]) -> datetime | None:
    """Best timestamp for an activity as a timeline «касание»."""
    completed = str(activity.get("COMPLETED") or "").upper() == "Y"
    if completed:
        keys = ("END_TIME", "START_TIME", "DEADLINE", "CREATED", "created")
    else:
        keys = ("CREATED", "created", "START_TIME", "DEADLINE", "END_TIME")
    for key in keys:
        dt = _parse_datetime(activity.get(key))
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _deal_activities_list(deal: dict[str, Any]) -> list[dict[str, Any]]:
    """Activities bound to the deal (collector `deal_activities` or open fallback)."""
    activities = deal.get("deal_activities")
    if isinstance(activities, list) and activities:
        return [a for a in activities if isinstance(a, dict)]
    open_activities = deal.get("open_activities") or []
    if isinstance(open_activities, list):
        return [a for a in open_activities if isinstance(a, dict)]
    return []


def _last_seller_touch_dt(
    timeline: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    allowed_author_ids: set[int],
) -> datetime | None:
    """Latest broker/ROP comment or activity datetime on the deal."""
    candidates: list[datetime] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        if _coerce_int(item.get("author_id")) not in allowed_author_ids:
            continue
        if not str(item.get("comment") or "").strip():
            continue
        created = _parse_datetime(item.get("created"))
        if created is not None:
            candidates.append(
                created if created.tzinfo else created.replace(tzinfo=timezone.utc),
            )
    for activity in activities:
        if not (_activity_author_ids(activity) & allowed_author_ids):
            continue
        touch = _activity_touch_datetime(activity)
        if touch is not None:
            candidates.append(touch)
    return max(candidates) if candidates else None


def _deal_has_open_activity(deal: dict[str, Any]) -> bool:
    """True if deal has at least one incomplete CRM activity."""
    for activity in _deal_activities_list(deal):
        if str(activity.get("COMPLETED") or "N").upper() != "Y":
            return True
    open_activities = deal.get("open_activities") or []
    if isinstance(open_activities, list):
        for activity in open_activities:
            if not isinstance(activity, dict):
                continue
            if str(activity.get("COMPLETED") or "N").upper() != "Y":
                return True
    return False


def is_general_base_move_enabled(now: datetime | None = None) -> bool:
    """True when deal/lead pool moves are allowed (after GENERAL_BASE_MOVE_AFTER).

    Empty GENERAL_BASE_MOVE_AFTER means enabled. Unparseable value → disabled.
    """
    settings = get_settings()
    raw = str(getattr(settings, "general_base_move_after", "") or "").strip()
    if not raw:
        return True
    after = _parse_datetime(raw)
    if after is None:
        logger.warning("Invalid GENERAL_BASE_MOVE_AFTER=%r — moves disabled", raw)
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    return current >= after


def _move_deal_to_general_base(
    deal_id: int,
    target_stage_id: str,
) -> dict[str, Any]:
    """Move a deal to воронка «Общая база» at target_stage_id (honors dry_run).

    Uses crm.item.update so CATEGORY_ID change is applied. Assignee is not
    changed.
    """
    from notify import _bx_call_sync

    settings = get_settings()
    if deal_id <= 0:
        return {"error": "invalid_deal_id", "deal_id": deal_id}
    if settings.dry_run:
        logger.info(
            "DRY_RUN: would move deal %s to category=%s stage=%s",
            deal_id,
            GENERAL_BASE_CATEGORY_ID,
            target_stage_id,
        )
        return {
            "dry_run_skipped": True,
            "deal_id": deal_id,
            "category_id": GENERAL_BASE_CATEGORY_ID,
            "stage_id": target_stage_id,
        }
    if not is_general_base_move_enabled():
        logger.info(
            "Skip general-base move for deal %s: GENERAL_BASE_MOVE_AFTER not reached",
            deal_id,
        )
        return {
            "skipped": True,
            "reason": "move_after_not_reached",
            "deal_id": deal_id,
            "stage_id": target_stage_id,
        }
    try:
        result = _bx_call_sync(
            "crm.item.update",
            {
                "entityTypeId": 2,
                "id": deal_id,
                "fields": {
                    "categoryId": GENERAL_BASE_CATEGORY_ID,
                    "stageId": target_stage_id,
                },
            },
        )
        logger.info(
            "Moved deal %s to general base (%s / %s)",
            deal_id,
            GENERAL_BASE_CATEGORY_ID,
            target_stage_id,
        )
        return {
            "ok": True,
            "deal_id": deal_id,
            "category_id": GENERAL_BASE_CATEGORY_ID,
            "stage_id": target_stage_id,
            "result": result,
        }
    except Exception as exc:
        logger.exception("Failed to move deal %s to general base", deal_id)
        return {"error": str(exc), "deal_id": deal_id}


def move_seller_deal_to_general_base(
    deal_id: int,
    stage_id: str = "",
) -> dict[str, Any]:
    """Move a sellers-funnel deal to «Общая база» → «Продавцы».

    «Поиск клиента» (UC_FADPBF) is never moved. Honors Settings.dry_run.
    """
    if stage_id in SELLERS_NEVER_MOVE_TO_GENERAL_BASE:
        logger.info(
            "Skip general-base move for deal %s: stage %s never moves",
            deal_id,
            stage_id,
        )
        return {
            "skipped": True,
            "reason": "search_client_never_move",
            "deal_id": deal_id,
            "stage_id": stage_id,
        }
    return _move_deal_to_general_base(deal_id, GENERAL_BASE_SELLERS_STAGE_ID)


def move_buyer_deal_to_general_base(
    deal_id: int,
    stage_id: str = "",
) -> dict[str, Any]:
    """Move a buyers-funnel deal to «Общая база» → «Покупатели». Honors dry_run."""
    return _move_deal_to_general_base(deal_id, GENERAL_BASE_BUYERS_STAGE_ID)


def _general_base_move_reason_suffix(result: dict[str, Any]) -> str:
    """Human-readable suffix for a deal moved (or not) to «Общая база».

    A completed move is stated in the past tense: the warning wording promises
    a future transfer, and a broker reading it after the fact believes the deal
    is still in their funnel.
    """
    if result.get("skipped"):
        return ""
    if result.get("ok"):
        return f" {GENERAL_BASE_MOVED_NOTICE}"
    if result.get("dry_run_skipped"):
        return f" {GENERAL_BASE_MOVE_WARNING}"
    if result.get("error"):
        return " Перенос в воронку «Общая база» не выполнен."
    return ""


def _apply_move_outcome(reason: str, result: dict[str, Any]) -> str:
    """Attach the move outcome, replacing a pre-baked future-tense warning.

    Some rules embed GENERAL_BASE_MOVE_WARNING while building the reason, before
    the move is attempted; once the deal has actually moved that sentence has to
    go, or the report both warns and reports the same transfer.
    """
    suffix = _general_base_move_reason_suffix(result)
    text = str(reason or "")
    if result.get("ok") and GENERAL_BASE_MOVE_WARNING in text:
        text = text.replace(GENERAL_BASE_MOVE_WARNING, "").strip()
    if not suffix:
        return text
    if suffix.strip() in text:
        return text
    return f"{text}{suffix}" if text else suffix.strip()


def process_deals_to_general_base(
    violations: list[dict[str, Any]],
    funnel: str,
) -> list[dict[str, Any]]:
    """Move unique violating deals to «Общая база»; annotate violation details.

    Sellers → stage «Продавцы» (C26:NEW). Buyers → stage «Покупатели»
    (C26:PREPARATION). Do not move: missed calls, «Поиск клиента», «Проиграна»,
    «Офер», missing Afina ID. Honors DRY_RUN and GENERAL_BASE_MOVE_AFTER.
    Assignee is not changed.
    """
    if not is_general_base_move_enabled():
        logger.info(
            "Skip general-base moves: GENERAL_BASE_MOVE_AFTER not reached",
        )
        return violations
    if funnel == "sellers":
        allowed_rules = DEAL_MOVE_RULES_SELLERS
        mover = move_seller_deal_to_general_base
    elif funnel == "buyers":
        allowed_rules = DEAL_MOVE_RULES_BUYERS
        mover = move_buyer_deal_to_general_base
    else:
        logger.warning("process_deals_to_general_base: unknown funnel %s", funnel)
        return violations

    by_deal: dict[int, str] = {}
    for violation in violations:
        if str(violation.get("rule") or "") not in allowed_rules:
            continue
        deal_id = _coerce_int(violation.get("entity_id"))
        if deal_id <= 0 or deal_id in by_deal:
            continue
        details = violation.get("details")
        stage_id = ""
        if isinstance(details, dict):
            stage_id = str(details.get("stage_id") or "")
        if funnel == "sellers" and stage_id in SELLERS_NEVER_MOVE_TO_GENERAL_BASE:
            continue
        by_deal[deal_id] = stage_id

    results: dict[int, dict[str, Any]] = {}
    for deal_id, stage_id in by_deal.items():
        results[deal_id] = mover(deal_id, stage_id)

    for violation in violations:
        deal_id = _coerce_int(violation.get("entity_id"))
        result = results.get(deal_id)
        if not result:
            continue
        details = violation.get("details")
        if not isinstance(details, dict):
            details = {}
            violation["details"] = details
        details["moved"] = bool(result.get("ok"))
        details["dry_run_skipped"] = bool(result.get("dry_run_skipped"))
        details["move_error"] = result.get("error")
        details["general_base_stage_id"] = result.get("stage_id")
        violation["reason"] = _apply_move_outcome(
            str(violation.get("reason") or ""), result,
        )
    return violations


def move_lead_to_shared_pool(lead_id: int) -> dict[str, Any]:
    """Move a lead to «Общие лиды» (STATUS_ID=1). Does not change assignee.

    Honors Settings.dry_run. Uses crm.lead.update.
    """
    from notify import _bx_call_sync

    settings = get_settings()
    if lead_id <= 0:
        return {"error": "invalid_lead_id", "lead_id": lead_id}
    if settings.dry_run:
        logger.info(
            "DRY_RUN: would move lead %s to STATUS_ID=%s",
            lead_id,
            LEAD_STATUS_SHARED,
        )
        return {
            "dry_run_skipped": True,
            "lead_id": lead_id,
            "status_id": LEAD_STATUS_SHARED,
        }
    if not is_general_base_move_enabled():
        logger.info(
            "Skip lead shared-pool move for %s: GENERAL_BASE_MOVE_AFTER not reached",
            lead_id,
        )
        return {"skipped": True, "reason": "move_after_not_reached", "lead_id": lead_id}
    try:
        result = _bx_call_sync(
            "crm.lead.update",
            {
                "id": lead_id,
                "fields": {"STATUS_ID": LEAD_STATUS_SHARED},
            },
        )
        logger.info(
            "Moved lead %s to shared pool STATUS_ID=%s",
            lead_id,
            LEAD_STATUS_SHARED,
        )
        return {
            "ok": True,
            "lead_id": lead_id,
            "status_id": LEAD_STATUS_SHARED,
            "result": result,
        }
    except Exception as exc:
        logger.exception("Failed to move lead %s to shared pool", lead_id)
        return {"error": str(exc), "lead_id": lead_id}


def _fetch_stage_history_rows(deal_ids: list[int]) -> list[dict[str, Any]]:
    """Load crm.stagehistory.list rows for deals (entityTypeId=2). No order param."""
    rows: list[dict[str, Any]] = []
    unique_ids = [did for did in deal_ids if did > 0]
    if not unique_ids:
        return rows
    for chunk in _chunks(unique_ids, STAGE_HISTORY_OWNER_CHUNK):
        try:
            raw = _bx_get_all_sync(
                "crm.stagehistory.list",
                {
                    "entityTypeId": 2,
                    "filter": {"OWNER_ID": chunk},
                    "select": [
                        "ID",
                        "OWNER_ID",
                        "STAGE_ID",
                        "CREATED_TIME",
                        "TYPE_ID",
                    ],
                },
            )
            rows.extend(_as_list(raw))
        except Exception:
            logger.warning(
                "crm.stagehistory.list failed for %d deal ids, trying per-deal",
                len(chunk),
            )
            from notify import _bx_call_sync

            for did in chunk:
                try:
                    raw = _bx_call_sync(
                        "crm.stagehistory.list",
                        {
                            "entityTypeId": 2,
                            "filter": {"OWNER_ID": did},
                            "select": [
                                "ID",
                                "OWNER_ID",
                                "STAGE_ID",
                                "CREATED_TIME",
                                "TYPE_ID",
                            ],
                        },
                    )
                    rows.extend(_as_list(raw))
                except Exception:
                    logger.warning(
                        "crm.stagehistory.list failed for deal %s", did,
                    )
    return rows


def _attach_stage_entered_at(deal_records: dict[int, dict[str, Any]]) -> None:
    """Set stage_entered_at ISO on deals whose stage is in STAGES_NEEDING_HISTORY."""
    need: list[dict[str, Any]] = [
        rec for rec in deal_records.values()
        if rec.get("stage_id") in STAGES_NEEDING_HISTORY
    ]
    if not need:
        return
    rows = _fetch_stage_history_rows([_coerce_int(rec.get("deal_id")) for rec in need])
    for rec in need:
        deal_id = _coerce_int(rec.get("deal_id"))
        stage_id = _clean_str(rec.get("stage_id"))
        entered = _latest_stage_entered_at(rows, deal_id, stage_id)
        if entered is not None:
            rec["stage_entered_at"] = entered.isoformat()


def _is_general_base_stage(stage_id: str) -> bool:
    """True for stages of category 26 (C26:*)."""
    return _clean_str(stage_id).startswith("C26:")


def _general_base_moved_at_from_history(
    rows: list[dict[str, Any]],
    deal_id: int,
    date_create: Any = None,
) -> datetime | None:
    """Datetime of the current stay in Общая база.

    Latest transfer = first C26:* stage after the last non-C26 stage.
    If the deal never left a non-GB stage, first C26:* (or DATE_CREATE).
    """
    events: list[tuple[datetime, int, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _coerce_int(row.get("OWNER_ID") or row.get("owner_id")) != deal_id:
            continue
        stage = _clean_str(row.get("STAGE_ID") or row.get("stage_id"))
        created = _parse_datetime(row.get("CREATED_TIME") or row.get("created_time"))
        if created is None or not stage:
            continue
        rid = _coerce_int(row.get("ID") or row.get("id"))
        events.append((created, rid, stage))
    if not events:
        return _parse_datetime(date_create)
    events.sort(key=lambda item: (item[0], item[1]))
    last_non_gb = -1
    for i, (_, _, stage) in enumerate(events):
        if not _is_general_base_stage(stage):
            last_non_gb = i
    if last_non_gb >= 0:
        for created, _, stage in events[last_non_gb + 1:]:
            if _is_general_base_stage(stage):
                return created
        return None
    for created, _, stage in events:
        if _is_general_base_stage(stage):
            return created
    return _parse_datetime(date_create)


def _attach_general_base_moved_at(deal_records: dict[int, dict[str, Any]]) -> None:
    """Set gb_moved_at ISO on category-26 deals from crm.stagehistory.list."""
    need: list[dict[str, Any]] = [
        rec for rec in deal_records.values()
        if (
            _coerce_int(rec.get("category_id")) == GENERAL_BASE_CATEGORY_ID
            or _is_general_base_stage(str(rec.get("stage_id") or ""))
        )
        and not rec.get("gb_moved_at")
    ]
    if not need:
        return
    rows = _fetch_stage_history_rows(
        [_coerce_int(rec.get("deal_id")) for rec in need],
    )
    for rec in need:
        moved = _general_base_moved_at_from_history(
            rows,
            _coerce_int(rec.get("deal_id")),
            rec.get("date_create"),
        )
        if moved is not None:
            rec["gb_moved_at"] = moved.isoformat()


def _lost_stage_id_for_category(category_id: int) -> str | None:
    """APOLOGY stage id for buyers/sellers funnels, else None."""
    settings = get_settings()
    if category_id == settings.buyers_category_id:
        return BUYERS_LOST_STAGE_ID
    if category_id == settings.sellers_category_id:
        return SELLERS_LOST_STAGE_ID
    return None


# Границы слова с учётом кириллицы: «РОП» не должен совпадать внутри «Европа».
_WORD_CHARS = "а-яёa-z0-9_"


def _position_is_rop(position: str, substrings: list[str]) -> bool:
    """True when a job title marks the user as a ROP.

    Matching is on word boundaries, not raw substrings: a bare "роп" occurs
    inside ordinary words («Европа»), and a false ROP would make an unrelated
    user's comments count towards the audit.
    """
    text = (position or "").strip().lower()
    if not text:
        return False
    for sub in substrings:
        token = sub.strip().lower()
        if not token:
            continue
        pattern = (
            rf"(?<![{_WORD_CHARS}]){re.escape(token)}(?![{_WORD_CHARS}])"
        )
        if re.search(pattern, text):
            return True
    return False


def _build_rop_map() -> dict[int, int]:
    """Build department_id → ROP user_id mapping.

    Matching is by substring (CONTACT_SOURCE_LOCK_ROP_POSITION_SUBSTR_JSON), the
    same rule the SOURCE locks use. An exact-title filter used to miss any
    spelling variation, and a missed ROP silently voids their comments.
    """
    substrings = get_settings().contact_source_lock_rop_position_substr
    try:
        users = _bx_get_all_sync("user.get", {"FILTER": {"ACTIVE": True}})
    except Exception:
        logger.warning("Failed to build ROP map, ROP comments won't be counted")
        return {}

    rop_map: dict[int, int] = {}
    for user in _as_list(users):
        if not isinstance(user, dict):
            continue
        if not _position_is_rop(str(user.get("WORK_POSITION") or ""), substrings):
            continue
        uid = _coerce_int(user.get("ID"))
        depts = user.get("UF_DEPARTMENT", [])
        if not uid or not isinstance(depts, list) or not depts:
            continue
        dept_id = _coerce_int(depts[0])
        if not dept_id:
            continue
        if dept_id in rop_map and rop_map[dept_id] != uid:
            logger.warning(
                "Department %s has several ROPs (%s, %s) — keeping %s",
                dept_id,
                rop_map[dept_id],
                uid,
                rop_map[dept_id],
            )
            continue
        rop_map[dept_id] = uid
    if not rop_map:
        logger.warning("ROP map is empty — ROP comments won't be counted")
    return rop_map


def _build_broker_dept_map(broker_ids: set[int]) -> dict[int, int]:
    """Build broker user_id → primary department_id mapping.

    One request for the whole set; per-broker fallback on failure. A partially
    built map means the ROP of the missing brokers stops counting as a comment
    author, so failures are logged loudly rather than swallowed.
    """
    if not broker_ids:
        return {}
    broker_dept_map: dict[int, int] = {}

    def _absorb(payload: Any) -> None:
        for user in _as_list(payload):
            if not isinstance(user, dict):
                continue
            uid = _coerce_int(user.get("ID"))
            depts = user.get("UF_DEPARTMENT", [])
            if uid and isinstance(depts, list) and depts:
                broker_dept_map[uid] = _coerce_int(depts[0])

    try:
        _absorb(_bx_get_all_sync("user.get", {"FILTER": {"ID": sorted(broker_ids)}}))
    except Exception:
        logger.warning("Bulk broker department fetch failed, falling back per user")

    # Добираем поштучно только тех, кого не вернул общий запрос: один сбой
    # больше не обрывает обход остальных брокеров.
    missing = sorted(broker_ids - set(broker_dept_map))
    failed = 0
    for bid in missing:
        try:
            _absorb(_bx_get_all_sync("user.get", {"ID": bid}))
        except Exception:
            failed += 1
            logger.warning("Failed to load department for broker %s", bid)
    if failed:
        logger.warning(
            "Broker department map incomplete: %d/%d users unresolved — "
            "their ROP comments will not be counted",
            failed,
            len(broker_ids),
        )
    return broker_dept_map


def _allowed_comment_authors(
    broker_id: int,
    broker_dept_map: dict[int, int],
    rop_map: dict[int, int],
) -> set[int]:
    """Authors whose timeline comments count: broker and their ROP."""
    allowed = {broker_id} if broker_id else set()
    broker_dept = broker_dept_map.get(broker_id)
    if broker_dept and rop_map:
        rop_id = rop_map.get(broker_dept)
        if rop_id:
            allowed.add(rop_id)
    return allowed


def _show_date_from_uf(uf_fields: dict[str, Any]) -> datetime | None:
    """Parse show/meeting date from labeled uf_fields."""
    return _parse_datetime(uf_fields.get("Дата встречи"))


def _show_date_from_timeline_comments(
    timeline: list[dict[str, Any]],
    now: datetime,
    allowed_author_ids: set[int] | None = None,
) -> datetime | None:
    """Try to parse show date from timeline comments text."""
    month_map = {
        "январ": 1,
        "феврал": 2,
        "март": 3,
        "апрел": 4,
        "мая": 5,
        "май": 5,
        "июн": 6,
        "июл": 7,
        "август": 8,
        "сентябр": 9,
        "октябр": 10,
        "ноябр": 11,
        "декабр": 12,
    }
    parsed: list[datetime] = []

    for item in timeline:
        if allowed_author_ids is not None:
            if _coerce_int(item.get("author_id")) not in allowed_author_ids:
                continue
        text = str(item.get("comment") or "").lower()
        if not text:
            continue

        for m in re.finditer(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", text):
            day = int(m.group(1))
            month = int(m.group(2))
            year_raw = m.group(3)
            year = now.year if not year_raw else int(year_raw)
            if year < 100:
                year += 2000
            try:
                parsed.append(now.replace(year=year, month=month, day=day))
            except ValueError:
                continue

        for m in re.finditer(
            r"(\d{1,2})\s+"
            r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|"
            r"июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)"
            r"(?:\s+(\d{4}))?",
            text,
        ):
            day = int(m.group(1))
            month_word = m.group(2)
            year = int(m.group(3)) if m.group(3) else now.year
            month = 0
            for key, value in month_map.items():
                if key in month_word:
                    month = value
                    break
            if month == 0:
                continue
            try:
                parsed.append(now.replace(year=year, month=month, day=day))
            except ValueError:
                continue

    return max(parsed) if parsed else None


def _has_show_plan_comment(
    timeline: list[dict[str, Any]],
    allowed_author_ids: set[int] | None = None,
) -> bool:
    """True when timeline contains a meaningful comment about a planned showing."""
    plan_markers = ("показ", "договарива", "назнач", "встреч")
    for item in timeline:
        if allowed_author_ids is not None:
            if _coerce_int(item.get("author_id")) not in allowed_author_ids:
                continue
        text = str(item.get("comment") or "").strip().lower()
        if len(text) < 12:
            continue
        if "показ" in text and any(marker in text for marker in plan_markers):
            return True
    return False


def _open_activity_due_datetime(activity: dict[str, Any]) -> datetime | None:
    """Return due datetime for an open activity."""
    for key in ("DEADLINE", "END_TIME", "START_TIME"):
        dt = _parse_datetime(activity.get(key))
        if dt is not None:
            return dt
    return None


def _deal_has_live_activity(deal: dict[str, Any], now: datetime) -> bool:
    """True if the deal has an incomplete CRM activity that is not overdue."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for activity in _deal_activities_list(deal):
        if str(activity.get("COMPLETED") or "N").upper() == "Y":
            continue
        due = _open_activity_due_datetime(activity)
        if due is None:
            return True
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if due > now:
            return True
    return False


def _activity_text(activity: dict[str, Any]) -> str:
    """Build normalized text from activity fields."""
    subject = str(activity.get("SUBJECT") or "")
    description = str(activity.get("DESCRIPTION") or "")
    return f"{subject} {description}".strip().lower()


def _is_contact_plan_activity(activity: dict[str, Any]) -> bool:
    """True when activity text describes planned client contact."""
    text = _activity_text(activity)
    if not text:
        return False
    contact_keywords = (
        "связ",
        "связат",
        "созвон",
        "звон",
        "позвон",
        "контакт",
        "клиент",
        "напис",
        "whatsapp",
        "telegram",
    )
    return any(keyword in text for keyword in contact_keywords)


def _activities_for_deal(
    deal_id: int,
    activities: list[Any],
) -> list[dict[str, Any]]:
    """Keep activities owned by this deal; fixtures without OWNER_* pass through.

    Bitrix OWNER_TYPE_ID=2 is deal. Activities on contacts/leads/other deals
    must not affect buyer_stage_2 contact-plan checks for this deal.
    """
    scoped: list[dict[str, Any]] = []
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        owner_id = _coerce_int(activity.get("OWNER_ID"))
        owner_type_id = _coerce_int(activity.get("OWNER_TYPE_ID"))
        if owner_id <= 0 and owner_type_id <= 0:
            scoped.append(activity)
            continue
        if owner_type_id == 2 and owner_id == deal_id:
            scoped.append(activity)
    return scoped


def _violation(
    deal: dict[str, Any],
    rule: str,
    reason: str,
    details: dict[str, Any],
    severity: str = "medium",
) -> dict[str, Any]:
    """Build a deal violation record (buyers or sellers)."""
    return {
        "entity_type": "deal",
        "entity_id": _coerce_int(deal.get("deal_id")),
        "responsible_id": _coerce_int(deal.get("assigned_by_id")),
        "severity": severity,
        "rule": rule,
        "reason": reason,
        "details": details,
    }


def _is_seller_violation(violation: dict[str, Any]) -> bool:
    """Return True if violation belongs to sellers funnel audit."""
    rule = str(violation.get("rule") or "")
    if rule.startswith("seller_"):
        return True
    details = violation.get("details")
    if isinstance(details, dict) and details.get("funnel") == "sellers":
        return True
    return False


def _is_general_base_violation(violation: dict[str, Any]) -> bool:
    """Return True if violation belongs to Общая база audit."""
    rule = str(violation.get("rule") or "")
    if rule == "general_base_no_plan":
        return True
    details = violation.get("details")
    if isinstance(details, dict) and details.get("funnel") == "general_base":
        return True
    return False


def seller_violation_action(violation: dict[str, Any]) -> str:
    """Return «что сделать» text for a seller violation (empty if unknown)."""
    rule = str(violation.get("rule") or "").strip()
    if rule == "seller_stage_stale":
        details = violation.get("details")
        stage_id = ""
        if isinstance(details, dict):
            stage_id = str(details.get("stage_id") or "")
        if stage_id == SELLER_STAGE_NEW:
            return (
                "Добавить комментарий брокера/РОПа или запланировать "
                "непросроченное дело"
            )
        if stage_id in SELLERS_COMMENT_ONLY_STAGES:
            return "Добавить комментарий в карточку сделки"
        return "Добавить комментарий или дело в карточку сделки"
    return SELLER_RULE_ACTIONS.get(rule, "")


def _severity_icon(severity: str, *, seller: bool = False) -> str:
    """Map severity to report icon; sellers use blue circles."""
    if seller:
        return {
            "very high": "🔵🔵",
            "high": "🔵",
            "medium": "🟦",
        }.get(severity, "⚪")
    return {
        "very high": "🔴🔴",
        "high": "🔴",
        "medium": "🟡",
    }.get(severity, "⚪")


def _deal_has_outgoing_call(deal: dict[str, Any]) -> bool:
    """True if deal.calls contains at least one outgoing call."""
    for call in deal.get("calls") or []:
        if isinstance(call, dict) and call.get("call_type") == "outgoing":
            return True
    return False


def _hours_since_create(deal: dict[str, Any], now: datetime) -> float:
    """Hours since deal date_create (0 if unknown)."""
    created = _parse_datetime(deal.get("date_create"))
    if created is None:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created).total_seconds() / 3600.0)


# Коды сущностей в звонках: voximplant отдаёт строку, activity.list — OWNER_TYPE_ID.
CALL_ENTITY_ALIASES: dict[str, frozenset[str]] = {
    "deal": frozenset({"DEAL", "2"}),
    "lead": frozenset({"LEAD", "1"}),
}


def _filter_calls_for_entity(
    calls: list[dict[str, Any]],
    entity_type: str,
    entity_id: int,
) -> list[dict[str, Any]]:
    """Keep only calls linked to the given CRM entity.

    `_fetch_user_calls_for_audit` returns the whole call history of a user, so
    the caller must narrow it down: a violation is raised against one card and
    must rest on that card's calls only.
    """
    aliases = CALL_ENTITY_ALIASES.get(entity_type.lower())
    if entity_id <= 0 or not aliases:
        return []
    linked: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        etype = str(call.get("crm_entity_type") or "").upper()
        eid = _coerce_int(call.get("crm_entity_id"))
        if etype in aliases and eid == entity_id:
            linked.append(call)
    return linked


def _filter_calls_for_deal(
    calls: list[dict[str, Any]],
    deal_id: int,
) -> list[dict[str, Any]]:
    """Keep only calls linked to the given deal."""
    return _filter_calls_for_entity(calls, "deal", deal_id)


def _evidence_incomplete(record: dict[str, Any]) -> bool:
    """True when the card's timeline/activities could not be read this run.

    Rules treat "no comment" and "no activity" as violations, so a failed fetch
    must not be mistaken for an empty card: that would flag a compliant broker
    and, for movable rules, relocate the deal to «Общая база».
    """
    return bool(record.get("evidence_incomplete"))


def count_incomplete(records: list[dict[str, Any]]) -> int:
    """How many cards had unreadable evidence in this run."""
    return sum(1 for r in records if _evidence_incomplete(r))


def _skip_incomplete(
    records: list[dict[str, Any]],
    id_key: str,
    scope: str,
) -> list[dict[str, Any]]:
    """Drop cards with unreadable evidence, logging how many were skipped."""
    usable = [r for r in records if not _evidence_incomplete(r)]
    skipped = len(records) - len(usable)
    if skipped:
        logger.warning(
            "%s: %d/%d cards skipped — evidence fetch failed (ids: %s)",
            scope,
            skipped,
            len(records),
            ", ".join(
                str(r.get(id_key))
                for r in records
                if _evidence_incomplete(r)
            )[:500],
        )
    return usable


def _has_comment_by_authors(
    timeline: list[dict[str, Any]],
    allowed_author_ids: set[int],
) -> bool:
    """True when timeline has a non-empty comment from an allowed author."""
    for item in timeline:
        if not isinstance(item, dict):
            continue
        if _coerce_int(item.get("author_id")) not in allowed_author_ids:
            continue
        if str(item.get("comment") or "").strip():
            return True
    return False


def _deal_has_author_activity(
    deal: dict[str, Any],
    allowed_author_ids: set[int],
) -> bool:
    """True if the deal has a CRM activity by the broker or their ROP."""
    if not allowed_author_ids:
        return False
    for activity in _deal_activities_list(deal):
        if _activity_author_ids(activity) & allowed_author_ids:
            return True
    return False


def _lost_stage_has_evidence(
    deal: dict[str, Any],
    timeline: list[dict[str, Any]],
    allowed_author_ids: set[int],
) -> bool:
    """Lost-stage evidence: any broker/ROP comment or activity (no time window)."""
    if _has_comment_by_authors(timeline, allowed_author_ids):
        return True
    return _deal_has_author_activity(deal, allowed_author_ids)


def check_seller_deal_violations(
    deals: list[dict[str, Any]],
    current_time: str,
    rop_map: dict[int, int] | None = None,
    broker_dept_map: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic audit of seller-funnel deals (reglament scope A).

    Rules:
    - seller_stage_stale: no broker/ROP comment or activity within stage cadence
    - seller_deferred_no_activity: LOSE without an open CRM activity

    Outgoing calls and paid-source filters are not used. Evidence is timeline
    comments and deal activities only.
    """
    now = _parse_datetime(current_time) or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    violations: list[dict[str, Any]] = []

    deals = _skip_incomplete(deals, "deal_id", "Seller audit")

    if rop_map is None:
        rop_map = _build_rop_map()
    if broker_dept_map is None:
        broker_ids = {
            _coerce_int(deal.get("assigned_by_id"))
            for deal in deals
            if _coerce_int(deal.get("assigned_by_id"))
        }
        broker_dept_map = _build_broker_dept_map(broker_ids)

    for deal in deals:
        stage_id = _clean_str(deal.get("stage_id"))
        if stage_id in SELLERS_SKIP_AUDIT_STAGES:
            continue

        source_id = _clean_str(deal.get("source_id"))
        raw_stage_name = str(deal.get("stage_name") or "").strip()
        stage_name = (
            SELLERS_STAGE_NAMES.get(stage_id)
            or (raw_stage_name if raw_stage_name and raw_stage_name != stage_id else "")
            or stage_id
            or "—"
        )
        source_name = SELLERS_PAID_SOURCE_NAMES.get(source_id, source_id or "—")
        assigned_by_id = _coerce_int(deal.get("assigned_by_id"))
        allowed_authors = _allowed_comment_authors(
            assigned_by_id, broker_dept_map, rop_map,
        )
        timeline = deal.get("timeline") or []
        if not isinstance(timeline, list):
            timeline = []
        activities = _deal_activities_list(deal)
        last_touch = _last_seller_touch_dt(timeline, activities, allowed_authors)
        baseline = last_touch or _parse_datetime(deal.get("date_create"))
        if baseline is not None and baseline.tzinfo is None:
            baseline = baseline.replace(tzinfo=timezone.utc)
        hours_since_touch = (
            round(max(0.0, (now - baseline).total_seconds() / 3600.0), 2)
            if baseline is not None
            else 0.0
        )
        has_open_activity = _deal_has_open_activity(deal)
        base_details: dict[str, Any] = {
            "deal_id": _coerce_int(deal.get("deal_id")),
            "title": str(deal.get("title") or ""),
            "stage_id": stage_id,
            "stage_name": stage_name,
            "source_id": source_id,
            "source_name": source_name,
            "funnel": "sellers",
            "category_id": _coerce_int(deal.get("category_id")),
            "hours_since_last_touch": hours_since_touch,
            "has_open_activity": has_open_activity,
            "has_broker_or_rop_touch": last_touch is not None,
        }

        if stage_id == SELLER_STAGE_DEFERRED:
            if not has_open_activity:
                violations.append(_violation(
                    deal,
                    "seller_deferred_no_activity",
                    (
                        f"На этапе «{stage_name}» нет запланированного дела "
                        "в карточке сделки."
                    ),
                    base_details,
                    severity="medium",
                ))
            continue

        if stage_id == SELLER_STAGE_LOST:
            if not _lost_stage_has_evidence(deal, timeline, allowed_authors):
                violations.append(_violation(
                    deal,
                    "seller_lost_no_reason",
                    (
                        f"Сделка на этапе «{stage_name}» "
                        "без комментария брокера/РОПа и без дела."
                    ),
                    base_details,
                    severity="medium",
                ))
            continue

        if stage_id == SELLER_STAGE_NEW:
            created = _parse_datetime(deal.get("date_create"))
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = now - created
            has_comment = _has_comment_by_authors(timeline, allowed_authors)
            has_live_activity = _deal_has_live_activity(deal, now)
            if age > SELLER_MEETING_DEADLINE and not has_comment and not has_live_activity:
                hours = round(age.total_seconds() / 3600.0, 2)
                violations.append(_violation(
                    deal,
                    "seller_stage_stale",
                    (
                        f"На этапе «{stage_name}» нет комментария брокера/РОПа "
                        "и нет непросроченного дела в течение 24 часов "
                        "с создания сделки."
                    ),
                    {**base_details, "hours_since_create": hours},
                    severity="high",
                ))
            continue

        if stage_id in SELLERS_AFINA_REQUIRED_STAGES and not _afina_id_filled(deal):
            violations.append(_violation(
                deal,
                "seller_afina_id_missing",
                (
                    f"На этапе «{stage_name}» не заполнено поле «ID Афины»."
                ),
                base_details,
                severity="medium",
            ))

        if stage_id == SELLER_STAGE_NEGOTIATIONS:
            days_on_stage = round(
                _days_between(_stage_entered_dt(deal, now), now), 2,
            )
            if days_on_stage > SELLER_NEGOTIATIONS_MAX_DAYS:
                violations.append(_violation(
                    deal,
                    "seller_negotiations_max",
                    (
                        f"Сделка находится на этапе «{stage_name}» "
                        f"более {SELLER_NEGOTIATIONS_MAX_DAYS} дней."
                    ),
                    {**base_details, "days_on_stage": days_on_stage},
                    severity="medium",
                ))

        cadence = SELLERS_STAGE_CADENCE.get(stage_id)
        if cadence is None:
            continue
        comment_only = stage_id in SELLERS_COMMENT_ONLY_STAGES
        if comment_only:
            last_touch = _last_seller_touch_dt(timeline, [], allowed_authors)
            baseline = last_touch or _parse_datetime(deal.get("date_create"))
            if baseline is not None and baseline.tzinfo is None:
                baseline = baseline.replace(tzinfo=timezone.utc)
        if baseline is None:
            continue
        if (now - baseline) <= cadence:
            continue

        cadence_hours = cadence.total_seconds() / 3600.0
        if cadence_hours <= 24:
            limit_text = f"{int(cadence_hours)} ч"
            severity = "high"
        else:
            limit_text = f"{max(1, int(cadence.total_seconds() // 86400))} дн."
            severity = "medium"

        if last_touch is None:
            if comment_only:
                reason = (
                    f"На этапе «{stage_name}» нет комментария брокера/РОПа "
                    f"(более {limit_text})."
                )
            else:
                reason = (
                    f"На этапе «{stage_name}» нет комментария или дела брокера/РОПа "
                    f"(более {limit_text})."
                )
        else:
            if comment_only:
                reason = (
                    f"На этапе «{stage_name}» последний комментарий брокера/РОПа "
                    f"более {limit_text} назад."
                )
            else:
                reason = (
                    f"На этапе «{stage_name}» последний комментарий или дело брокера/РОПа "
                    f"более {limit_text} назад."
                )
        violations.append(_violation(
            deal,
            "seller_stage_stale",
            reason,
            {
                **base_details,
                "cadence_hours": round(cadence.total_seconds() / 3600.0, 2),
            },
            severity=severity,
        ))

    return violations


def list_seller_meeting_reminders(
    deals: list[dict[str, Any]],
    current_time: str,
    rop_map: dict[int, int] | None = None,
    broker_dept_map: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """NEW-stage deals without broker/ROP comment or live activity, 2h before deadline."""
    now = _parse_datetime(current_time) or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if rop_map is None:
        rop_map = _build_rop_map()
    if broker_dept_map is None:
        broker_ids = {
            _coerce_int(deal.get("assigned_by_id"))
            for deal in deals
            if _coerce_int(deal.get("assigned_by_id"))
        }
        broker_dept_map = _build_broker_dept_map(broker_ids)
    remind_after = SELLER_MEETING_DEADLINE - SELLER_MEETING_REMIND_BEFORE
    reminders: list[dict[str, Any]] = []
    for deal in deals:
        if _clean_str(deal.get("stage_id")) != SELLER_STAGE_NEW:
            continue
        created = _parse_datetime(deal.get("date_create"))
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = now - created
        if age < remind_after or age >= SELLER_MEETING_DEADLINE:
            continue
        assigned_by_id = _coerce_int(deal.get("assigned_by_id"))
        timeline = deal.get("timeline") or []
        if not isinstance(timeline, list):
            timeline = []
        allowed_authors = _allowed_comment_authors(
            assigned_by_id, broker_dept_map, rop_map,
        )
        if _has_comment_by_authors(timeline, allowed_authors):
            continue
        if _deal_has_live_activity(deal, now):
            continue
        hours_left = max(
            0.0,
            (SELLER_MEETING_DEADLINE - age).total_seconds() / 3600.0,
        )
        reminders.append({
            "deal_id": _coerce_int(deal.get("deal_id")),
            "title": str(deal.get("title") or ""),
            "assigned_by_id": assigned_by_id,
            "hours_left": round(hours_left, 2),
            "stage_name": str(
                deal.get("stage_name") or SELLERS_STAGE_NAMES.get("NEW", "Назначение встречи")
            ),
        })
    return reminders


def _has_action_plan_text(text: str) -> bool:
    """True when text names a next step beyond generic contact."""
    cleaned = _strip_lead_markup(text).lower().strip()
    if not cleaned:
        return False
    if not _GB_PLAN_RE.search(cleaned):
        return False
    remainder = _GB_NOT_A_PLAN_RE.sub(" ", cleaned)
    return bool(_GB_PLAN_RE.search(remainder))


def _timeline_has_action_plan_since(
    timeline: list[dict[str, Any]],
    since: datetime,
) -> bool:
    """True if a timeline comment after ``since`` contains an action plan."""
    threshold = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    for item in timeline:
        if not isinstance(item, dict):
            continue
        if not _has_action_plan_text(str(item.get("comment") or "")):
            continue
        created = _parse_datetime(item.get("created"))
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= threshold:
            return True
    return False


def check_general_base_violations(
    deals: list[dict[str, Any]],
    current_time: str,
) -> list[dict[str, Any]]:
    """Deals in Общая база without a live activity or action-plan comment in 2 days.

    Clock starts at gb_moved_at (current stay in category 26). Missing clock →
    skip (no false positive). Report-only: does not move the deal again.
    """
    now = _parse_datetime(current_time) or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    violations: list[dict[str, Any]] = []

    for deal in _skip_incomplete(deals, "deal_id", "General base audit"):
        stage_id = _clean_str(deal.get("stage_id"))
        category_id = _coerce_int(deal.get("category_id"))
        if (
            category_id != GENERAL_BASE_CATEGORY_ID
            and not _is_general_base_stage(stage_id)
        ):
            continue
        if stage_id.endswith(":WON") or stage_id.endswith(":LOSE"):
            continue

        moved_at = _parse_datetime(deal.get("gb_moved_at"))
        if moved_at is None:
            continue
        if moved_at.tzinfo is None:
            moved_at = moved_at.replace(tzinfo=timezone.utc)
        age = now - moved_at
        if age <= GENERAL_BASE_PLAN_DEADLINE:
            continue

        timeline = deal.get("timeline") or []
        if not isinstance(timeline, list):
            timeline = []
        has_live = _deal_has_live_activity(deal, now)
        has_plan = _timeline_has_action_plan_since(timeline, moved_at)
        if has_live or has_plan:
            continue

        days_since_move = round(age.total_seconds() / 86400.0, 2)
        stage_name = str(deal.get("stage_name") or stage_id or "—")
        violations.append(_violation(
            deal,
            "general_base_no_plan",
            (
                "В воронке «Общая база» более 2 дней с даты переноса "
                "нет запланированного дела и нет комментария "
                "с планом дальнейших действий."
            ),
            {
                "deal_id": _coerce_int(deal.get("deal_id")),
                "title": str(deal.get("title") or ""),
                "stage_id": stage_id,
                "stage_name": stage_name,
                "funnel": "general_base",
                "category_id": category_id or GENERAL_BASE_CATEGORY_ID,
                "gb_moved_at": moved_at.isoformat(),
                "days_on_stage": days_since_move,
                "has_live_activity": has_live,
                "has_action_plan_comment": has_plan,
            },
            severity="medium",
        ))
    return violations


def check_missed_callback_violations(
    entities: list[dict[str, Any]],
    entity_type: str,
) -> list[dict[str, Any]]:
    """Детерминированная проверка: последний пропущенный без обратного."""
    violations = []
    id_field = f"{entity_type}_id"

    for entity in entities:
        if entity_type == "lead":
            status_id = _lead_status_id(entity)
            if status_id in LEAD_SKIP_MISSED_CALL_STATUS_IDS:
                continue

        calls = entity.get("calls", [])
        if not calls:
            continue

        # 1. Сортируем звонки по start_date
        sorted_calls = sorted(calls, key=lambda c: c.get("start_date", ""))

        # 2. Находим последний missed
        last_missed_idx = -1
        for i in range(len(sorted_calls) - 1, -1, -1):
            if sorted_calls[i].get("status") == "missed":
                last_missed_idx = i
                break

        if last_missed_idx == -1:
            continue  # Нет пропущенных

        # 3. Проверяем, есть ли исходящий после последнего пропущенного
        has_callback = any(
            c.get("call_type") == "outgoing"
            for c in sorted_calls[last_missed_idx + 1:]
        )

        if not has_callback:
            rule = (
                "lead_missed_callback"
                if entity_type == "lead"
                else "buyer_missed_callback"
            )
            violations.append({
                "entity_type": entity_type,
                "entity_id": entity.get(id_field),
                "responsible_id": entity.get("assigned_by_id"),
                "severity": "very high",
                "rule": rule,
                "reason": "Пропущенный звонок без обратного",
                "details": {},
            })

    return violations


_SPAM_JUSTIFICATION_TOKENS = frozenset({"спам", "spam"})


def _strip_lead_markup(text: str) -> str:
    """Strip BBCode/HTML and collapse whitespace for justification checks."""
    cleaned = re.sub(r"\[/?[^\]]+\]", " ", text or "")
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_spam_word_justification(text: str) -> bool:
    """True if text is exactly «спам» / «spam» (rule_2 special case)."""
    plain = _strip_lead_markup(text).lower().strip(" :;-—!.…")
    return plain in _SPAM_JUSTIFICATION_TOKENS


def _timeline_comments(lead: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in lead.get("timeline") or []:
        if not isinstance(item, dict):
            continue
        comment = str(item.get("comment") or item.get("COMMENT") or "")
        if comment.strip():
            out.append(comment)
    return out


def lead_has_rule2_justification(lead: dict[str, Any]) -> bool:
    """Spam justification: timeline comment > 5 chars, or exact word «спам»."""
    for comment in _timeline_comments(lead):
        if _is_spam_word_justification(comment):
            return True
        if len(_strip_lead_markup(comment)) > 5:
            return True
    return False


def lead_has_rule3_justification(lead: dict[str, Any]) -> bool:
    """Non-target justification: timeline OR comments_field text > 5 chars."""
    comments_field = str(
        lead.get("comments_field") or lead.get("COMMENTS") or ""
    )
    if len(_strip_lead_markup(comments_field)) > 5:
        return True
    for comment in _timeline_comments(lead):
        if len(_strip_lead_markup(comment)) > 5:
            return True
        if _is_spam_word_justification(comment):
            return True
    return False


def _lead_needs_llm_check(lead: dict[str, Any]) -> bool:
    """True if lead is in spam/non-target scope and still lacks justification.

    Kept for compatibility; rule_2/rule_3 are enforced in Python
    via check_lead_rule2_rule3_violations (no LLM).
    """
    status_id = _lead_status_id(lead)
    status_name = str(lead.get("status_name") or "")

    if _is_lead_spam_status(status_id):
        return not lead_has_rule2_justification(lead)

    if status_id in LEAD_SKIP_LLM_STATUS_IDS:
        return False
    if "агент" in status_name.lower():
        return False
    return not lead_has_rule3_justification(lead)


def check_lead_rule2_rule3_violations(
    leads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deterministic spam / non-target justification checks (rules 2–3)."""
    violations: list[dict[str, Any]] = []
    # Обоснование ищется в таймлайне — нечитаемый таймлайн не равен «нет обоснования».
    for lead in _skip_incomplete(leads, "lead_id", "Lead rules 2-3"):
        status_id = _lead_status_id(lead)
        status_name = str(lead.get("status_name") or status_id or "—")
        lead_id = _coerce_int(lead.get("lead_id") or lead.get("ID"))
        assigned = lead.get("assigned_by_id")
        if assigned is None:
            assigned = lead.get("ASSIGNED_BY_ID")
        assigned_id = _coerce_int(assigned)

        if _is_lead_spam_status(status_id):
            if lead_has_rule2_justification(lead):
                continue
            label = status_name if status_name and status_name != status_id else "Спам"
            violations.append({
                "entity_type": "lead",
                "entity_id": lead_id,
                "responsible_id": assigned_id,
                "severity": "high",
                "rule": "lead_rule_2",
                "reason": (
                    f"Лид переведен в статус «{label}» "
                    "без указания причины в карточке."
                ),
                "details": {
                    "lead_id": lead_id,
                    "title": lead.get("title"),
                    "status_name": label,
                    "has_justification": False,
                },
            })
            continue

        if status_id in LEAD_SKIP_LLM_STATUS_IDS:
            continue
        if "агент" in status_name.lower():
            continue
        if lead_has_rule3_justification(lead):
            continue

        if status_id == LEAD_STATUS_NECELEVOY:
            label = (
                status_name
                if status_name and status_name != status_id
                else "Нецелевой"
            )
        else:
            label = status_name or status_id or "—"
        violations.append({
            "entity_type": "lead",
            "entity_id": lead_id,
            "responsible_id": assigned_id,
            "severity": "high",
            "rule": "lead_rule_3",
            "reason": (
                f"Лид переведен в статус «{label}» "
                "без указания причины в карточке."
            ),
            "details": {
                "lead_id": lead_id,
                "title": lead.get("title"),
                "status_name": label,
                "has_justification": False,
            },
        })
    return violations


def check_lead_rule1_violations(
    leads: list[dict[str, Any]],
    current_time: datetime,
) -> list[dict[str, Any]]:
    """Детерминированная проверка: лид NEW > 2 часов без комментария."""
    violations = []
    for lead in leads:
        status_id = _lead_status_id(lead)
        if status_id != LEAD_STATUS_NEW:
            continue

        date_create = _parse_datetime(lead.get("date_create"))
        if date_create is None:
            continue

        hours = (current_time - date_create).total_seconds() / 3600
        if hours <= 2:
            continue

        # Проверяем, есть ли комментарий от ответственного в timeline
        assigned_id = lead.get("assigned_by_id")
        timeline = lead.get("timeline", [])
        has_broker_comment = any(
            _coerce_int(item.get("author_id")) == assigned_id
            and str(item.get("comment", "")).strip()
            for item in timeline
        )

        if not has_broker_comment:
            reason = (
                "Лид находится в статусе «Новый» более 2 часов, "
                f"необходимо квалифицировать лида. (прошло {hours:.0f} часов)"
            )
            violations.append({
                "entity_type": "lead",
                "entity_id": _coerce_int(lead.get("lead_id")),
                "responsible_id": assigned_id,
                "severity": "high",
                "rule": "lead_rule_1",
                "reason": reason,
                "details": {
                    "lead_id": lead.get("lead_id"),
                    "title": lead.get("title"),
                    "status_name": lead.get("status_name"),
                    "hours_since_creation": round(hours, 2),
                    "has_broker_comment": False,
                },
            })
    return violations


def _lead_hours_since_create(lead: dict[str, Any], current_time: datetime) -> float | None:
    """Hours since lead date_create, or None if unknown."""
    date_create = _parse_datetime(lead.get("date_create"))
    if date_create is None:
        return None
    return (current_time - date_create).total_seconds() / 3600


def process_stale_new_leads(
    leads: list[dict[str, Any]],
    current_time: datetime,
) -> list[dict[str, Any]]:
    """Move NEW leads older than 24h to «Общие лиды»; return report rows.

    Assignee is not changed. Mutation honors Settings.dry_run.
    """
    violations: list[dict[str, Any]] = []
    for lead in leads:
        if _lead_status_id(lead) != LEAD_STATUS_NEW:
            continue
        hours = _lead_hours_since_create(lead, current_time)
        if hours is None or hours <= LEAD_NEW_MOVE_HOURS:
            continue
        lead_id = _coerce_int(lead.get("lead_id") or lead.get("ID"))
        assigned_id = lead.get("assigned_by_id")
        if assigned_id is None:
            assigned_id = lead.get("ASSIGNED_BY_ID")
        result = move_lead_to_shared_pool(lead_id)
        moved = bool(result.get("ok"))
        dry_run_skipped = bool(result.get("dry_run_skipped"))
        postponed = bool(result.get("skipped"))
        if dry_run_skipped or postponed:
            reason = (
                "Лид находится в статусе «Новый» более 24 часов, "
                "требуется перевод в «Общие лиды» "
                f"(прошло {hours:.0f} часов)."
            )
        elif moved:
            reason = (
                "Лид находился в статусе «Новый» более 24 часов и переведён "
                f"в «Общие лиды» (прошло {hours:.0f} часов)."
            )
        else:
            reason = (
                "Лид находится в статусе «Новый» более 24 часов, "
                "перевод в «Общие лиды» не выполнен "
                f"(прошло {hours:.0f} часов)."
            )
        violations.append({
            "entity_type": "lead",
            "entity_id": lead_id,
            "responsible_id": _coerce_int(assigned_id),
            "severity": "high",
            "rule": "lead_new_over_24h",
            "reason": reason,
            "details": {
                "lead_id": lead_id,
                "title": lead.get("title"),
                "status_name": lead.get("status_name") or "Новый",
                "hours_since_creation": round(hours, 2),
                "moved": moved,
                "dry_run_skipped": dry_run_skipped,
                "move_error": result.get("error"),
            },
        })
    return violations


def check_buyer_deal_violations(
    deals: list[dict[str, Any]],
    current_time: str,
    rop_map: dict[int, int] | None = None,
    broker_dept_map: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic audit of buyer funnel deals (rules by audit_rule).

    Cadence/show/deferred rules plus ofer/lost/agent comments.
    A Подбор deal may emit both buyer_stage_2 and buyer_podbor_stale.
    Comments count only from the responsible broker or their ROP.
    """
    now = _parse_datetime(current_time) or datetime.now(timezone.utc)
    violations: list[dict[str, Any]] = []
    deals = _skip_incomplete(deals, "deal_id", "Buyer audit")

    if rop_map is None:
        rop_map = _build_rop_map()

    if broker_dept_map is None:
        broker_ids = {
            _coerce_int(deal.get("assigned_by_id"))
            for deal in deals
            if _coerce_int(deal.get("assigned_by_id"))
        }
        broker_dept_map = _build_broker_dept_map(broker_ids)

    for deal in deals:
        audit_rule = deal.get("audit_rule")
        if audit_rule is None:
            continue

        rule_num = int(audit_rule)
        stage_name = str(deal.get("stage_name") or BUYERS_STAGE_NAMES.get(
            str(deal.get("stage_id") or ""), "—",
        ))
        stage_id = str(deal.get("stage_id") or "")
        days_on_create = round(_days_between(deal.get("date_create"), now), 2)
        days_on_stage = round(_days_between(_stage_entered_dt(deal, now), now), 2)
        timeline = deal.get("timeline") or []
        assigned_by_id = _coerce_int(deal.get("assigned_by_id"))
        allowed_comment_authors = _allowed_comment_authors(
            assigned_by_id,
            broker_dept_map,
            rop_map,
        )
        base_details = {
            "deal_id": _coerce_int(deal.get("deal_id")),
            "title": str(deal.get("title") or ""),
            "stage_id": stage_id,
            "stage_name": stage_name,
        }

        if rule_num == 1:
            if days_on_create > 1:
                violations.append(_violation(
                    deal,
                    "buyer_stage_1",
                    f"Сделка находится на этапе «{stage_name}» более 1 дня.",
                    {**base_details, "days_on_stage": days_on_create},
                ))

        elif rule_num == 2:
            if days_on_stage > BUYER_PODBOR_MAX_DAYS:
                violations.append(_violation(
                    deal,
                    "buyer_podbor_stale",
                    (
                        f"Сделка находится на этапе «{stage_name}» "
                        f"более {BUYER_PODBOR_MAX_DAYS} дней."
                    ),
                    {**base_details, "days_on_stage": days_on_stage},
                ))
            # Касание = комментарий ответственного брокера или его РОПа.
            # Если комментариев не было — считаем от DATE_CREATE (не флажим сделки < 2 дн.).
            days_since_comment = _days_since_last_comment_by_authors(
                timeline, now, allowed_comment_authors,
            )
            if days_since_comment >= NO_COMMENT_DAYS:
                idle_days = int(days_on_create)
            else:
                idle_days = days_since_comment
            if idle_days > 2:
                deal_id = _coerce_int(deal.get("deal_id"))
                deal_open_activities = _activities_for_deal(
                    deal_id, deal.get("open_activities") or [],
                )
                responsible_open_activities = _activities_for_deal(
                    deal_id, deal.get("responsible_open_activities") or [],
                )

                contact_activities = [
                    activity for activity in (deal_open_activities + responsible_open_activities)
                    if _is_contact_plan_activity(activity)
                ]
                has_contact_plan = bool(contact_activities)
                has_non_overdue_contact_plan = False
                has_overdue_contact_plan = False
                for activity in contact_activities:
                    due = _open_activity_due_datetime(activity)
                    if due is not None and due <= now:
                        has_overdue_contact_plan = True
                    else:
                        has_non_overdue_contact_plan = True

                # Если есть живое запланированное дело по связи с клиентом,
                # отсутствие свежего комментария не считаем нарушением.
                if has_non_overdue_contact_plan:
                    continue

                if days_since_comment >= NO_COMMENT_DAYS:
                    if has_overdue_contact_plan:
                        reason = (
                            f"На этапе «{stage_name}» нет комментария ответственного "
                            "более 2 дней, а запланированное дело по связи с клиентом "
                            "просрочено."
                        )
                    else:
                        reason = (
                            f"Сделка находится на этапе «{stage_name}» более 2 дней "
                            "без комментария ответственного."
                        )
                else:
                    if has_overdue_contact_plan:
                        reason = (
                            f"Сделка находится на этапе «{stage_name}», последний комментарий "
                            f"ответственного более {days_since_comment} дней назад, "
                            "а запланированное дело по связи с клиентом просрочено."
                        )
                    else:
                        reason = (
                            f"Сделка находится на этапе «{stage_name}», последний комментарий "
                            f"ответственного более {days_since_comment} дней назад."
                        )
                violations.append(_violation(
                    deal,
                    "buyer_stage_2",
                    reason,
                    {
                        **base_details,
                        "days_since_last_comment": days_since_comment,
                        "idle_days": idle_days,
                        "has_contact_plan_activity": has_contact_plan,
                        "has_overdue_contact_plan_activity": has_overdue_contact_plan,
                    },
                ))

        elif rule_num == 3:
            deal_id = _coerce_int(deal.get("deal_id"))
            open_activities = _activities_for_deal(
                deal_id, deal.get("open_activities") or [],
            )
            overdue_activities: list[dict[str, Any]] = []
            active_activities: list[dict[str, Any]] = []
            for activity in open_activities:
                due = _open_activity_due_datetime(activity)
                if due is not None and due <= now:
                    overdue_activities.append(activity)
                else:
                    active_activities.append(activity)
            has_planned_activity = bool(open_activities)
            has_overdue_activity = bool(overdue_activities)
            has_active_planned_activity = bool(active_activities)

            planned_show = None
            if active_activities:
                dues = [
                    due for due in (
                        _open_activity_due_datetime(act) for act in active_activities
                    )
                    if due is not None
                ]
                if dues:
                    planned_show = min(dues)
            entered = _stage_entered_dt(deal, now)
            enforce_show_horizon = stage_id in BUYER_SHOW_HORIZON_STAGE_IDS
            show_horizon = entered + timedelta(days=BUYER_SHOW_MAX_DAYS_FROM_STAGE)
            show_too_far = bool(
                enforce_show_horizon and planned_show and planned_show > show_horizon
            )

            should_flag = False
            if not has_active_planned_activity:
                should_flag = True
            if show_too_far:
                should_flag = True

            if should_flag:
                reason_parts = []
                if not has_planned_activity:
                    reason_parts.append("нет запланированного дела")
                elif has_overdue_activity and not has_active_planned_activity:
                    reason_parts.append("запланированное дело просрочено")
                if show_too_far:
                    reason_parts.append(
                        f"дата дела дальше {BUYER_SHOW_MAX_DAYS_FROM_STAGE} дней "
                        "от перевода на этап"
                    )
                reasons_text = "; ".join(reason_parts) or "нет актуального запланированного дела"
                reason = (
                    f"На этапе «{stage_name}» {reasons_text}. "
                    f"{GENERAL_BASE_MOVE_WARNING}"
                )
                violations.append(_violation(
                    deal,
                    "buyer_stage_3",
                    reason,
                    {
                        **base_details,
                        "has_planned_activity": has_planned_activity,
                        "has_overdue_activity": has_overdue_activity,
                        "has_active_planned_activity": has_active_planned_activity,
                        "show_too_far": show_too_far,
                    },
                    severity="high",
                ))

        elif rule_num == 4:
            days_since_comment = _days_since_last_comment_by_authors(
                timeline, now, allowed_comment_authors,
            )
            if days_since_comment > 5:
                if days_since_comment >= NO_COMMENT_DAYS:
                    reason = f"На этапе «{stage_name}» нет комментариев."
                else:
                    reason = (
                        f"На этапе «{stage_name}» последний комментарий "
                        f"более {days_since_comment} дней назад."
                    )
                violations.append(_violation(
                    deal,
                    "buyer_stage_4",
                    reason,
                    {**base_details,
                     "days_since_last_comment": days_since_comment},
                ))

        elif rule_num == 5:
            # Отложенный спрос: нужно живое запланированное дело по сделке (не комментарий).
            deal_id = _coerce_int(deal.get("deal_id"))
            open_activities = _activities_for_deal(
                deal_id,
                (deal.get("open_activities") or [])
                + (deal.get("responsible_open_activities") or []),
            )
            overdue_activities = []
            active_activities = []
            for activity in open_activities:
                due = _open_activity_due_datetime(activity)
                if due is not None and due <= now:
                    overdue_activities.append(activity)
                else:
                    active_activities.append(activity)
            has_planned_activity = bool(open_activities)
            has_overdue_activity = bool(overdue_activities)
            has_active_planned_activity = bool(active_activities)
            if has_active_planned_activity:
                continue
            if not has_planned_activity:
                reason = f"На этапе «{stage_name}» нет запланированного дела."
            else:
                reason = f"На этапе «{stage_name}» запланированное дело просрочено."
            violations.append(_violation(
                deal,
                "buyer_stage_5",
                reason,
                {
                    **base_details,
                    "has_planned_activity": has_planned_activity,
                    "has_overdue_activity": has_overdue_activity,
                    "has_active_planned_activity": has_active_planned_activity,
                },
            ))

        elif rule_num == 6:
            entered = _stage_entered_dt(deal, now)
            if not _author_comment_meets(
                timeline,
                allowed_comment_authors,
                BUYER_OFER_MIN_COMMENT_LEN,
                entered,
            ):
                violations.append(_violation(
                    deal,
                    "buyer_ofer_comment",
                    (
                        f"На этапе «{stage_name}» нет развёрнутого комментария "
                        f"брокера или РОПа (минимум {BUYER_OFER_MIN_COMMENT_LEN} символов)."
                    ),
                    base_details,
                    severity="high",
                ))

        elif rule_num == 7:
            if not _lost_stage_has_evidence(
                deal, timeline, allowed_comment_authors,
            ):
                violations.append(_violation(
                    deal,
                    "buyer_lost_no_reason",
                    (
                        f"Сделка на этапе «{stage_name}» "
                        "без комментария брокера/РОПа и без дела."
                    ),
                    base_details,
                    severity="medium",
                ))

        elif rule_num == 8:
            entered = _stage_entered_dt(deal, now)
            if not _author_comment_meets(
                timeline,
                allowed_comment_authors,
                BUYER_REASON_MIN_COMMENT_LEN,
                entered,
            ):
                violations.append(_violation(
                    deal,
                    "buyer_agent_no_comment",
                    (
                        f"Сделка на этапе «{stage_name}» "
                        "без комментария, почему контакт — агент."
                    ),
                    base_details,
                    severity="medium",
                ))

    return violations


def build_stage_name_index(
    *,
    buyers_deals: list[dict[str, Any]] | None = None,
    sellers_deals: list[dict[str, Any]] | None = None,
    general_base_deals: list[dict[str, Any]] | None = None,
    leads: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Build stage/status code → human name map for a whole audit run.

    The map does not depend on the violation, so the dispatcher builds it once
    instead of rescanning every deal and lead for each reported violation.
    """
    code_to_name: dict[str, str] = dict(BUYERS_STAGE_NAMES)

    for deals in (
        buyers_deals or [],
        sellers_deals or [],
        general_base_deals or [],
    ):
        for deal in deals:
            stage_id = str(deal.get("stage_id") or "")
            stage_name = str(deal.get("stage_name") or "")
            if stage_id and stage_name and stage_id != stage_name:
                code_to_name[stage_id] = stage_name

    for lead in leads or []:
        status_id = str(lead.get("status_id") or "")
        status_name = str(lead.get("status_name") or "")
        if status_id and status_name and status_id != status_name:
            code_to_name[status_id] = status_name

    return code_to_name


def humanize_violation_reason(
    violation: dict[str, Any],
    *,
    buyers_deals: list[dict[str, Any]] | None = None,
    sellers_deals: list[dict[str, Any]] | None = None,
    general_base_deals: list[dict[str, Any]] | None = None,
    leads: list[dict[str, Any]] | None = None,
    name_index: dict[str, str] | None = None,
) -> str:
    """Replace CRM stage/status codes in violation reason with Russian names.

    Pass `name_index` from build_stage_name_index() to avoid rebuilding the map
    per violation; without it the map is built from the supplied lists.
    """
    reason = str(violation.get("reason") or "—")
    if name_index is None:
        name_index = build_stage_name_index(
            buyers_deals=buyers_deals,
            sellers_deals=sellers_deals,
            general_base_deals=general_base_deals,
            leads=leads,
        )
    code_to_name = dict(name_index)

    details = violation.get("details")
    if isinstance(details, dict):
        stage_name = details.get("stage_name") or details.get("status_name")
        stage_id = str(details.get("stage_id") or details.get("status_id") or "")
        if stage_id and stage_name and stage_id != stage_name:
            code_to_name[stage_id] = str(stage_name)

    for code in sorted(code_to_name, key=len, reverse=True):
        if not code:
            continue
        # Numeric status IDs (e.g. "1" for "Общие Лиды") must never be replaced
        # in free text, otherwise numbers in durations ("44 часов") get corrupted.
        if code.isdigit():
            continue
        if code not in reason:
            continue

        name = code_to_name[code]
        quoted_name = name if name.startswith("«") else f"«{name}»"

        # First replace explicit quoted codes.
        reason = reason.replace(f"«{code}»", quoted_name)
        # Then replace standalone unquoted code tokens.
        reason = re.sub(
            rf"(?<![\w]){re.escape(code)}(?![\w])",
            quoted_name,
            reason,
        )

    return reason


def _extract_comments(raw: Any) -> list[dict[str, Any]]:
    """Map timeline comment records to output format.

    Args:
        raw: Response from crm.timeline.comment.list.

    Returns:
        List of comment dicts with author_id, comment, created.
    """
    comments: list[dict[str, Any]] = []
    for item in _as_list(raw):
        files = item.get("FILES") or item.get("files") or []
        comments.append(
            {
                "author_id": int(item.get("AUTHOR_ID") or item.get("authorId") or 0),
                "comment": _clean_str(item.get("COMMENT") or item.get("comment")),
                "created": _clean_str(item.get("CREATED") or item.get("created")),
                # Только факт наличия вложения. Само содержимое не читаем: для
                # правила «приложи скриншот переписки» хватает того, что файл
                # есть, а картинку всё равно смотрит человек.
                "has_files": bool(_as_list(files)),
            }
        )
    return comments


def _coerce_float(value: Any) -> float:
    """Convert opportunity-like values to float.

    Args:
        value: Raw field value.

    Returns:
        Float value, 0.0 on failure.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_int(value: Any) -> int:
    """Convert ID-like values to int.

    Args:
        value: Raw field value.

    Returns:
        Integer value, 0 on failure.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _fetch_user_calls_for_audit(
    user_id: int,
    hours_ago: int = 720,
    crm_entity_type: str | None = None,
    crm_entity_id: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch calls trying multiple APIs for missed call detection.

    Priority:
    1. voximplant.statistic.get (CALL_FAILED_CODE)
    2. crm.activity.list (COMPLETED=N)
    3. crm.activity.list (DESCRIPTION text search)

    Args:
        user_id: Bitrix24 user ID.
        hours_ago: Lookback window in hours.
        crm_entity_type: Optional CRM entity type (LEAD, DEAL).
        crm_entity_id: Optional CRM entity ID.

    Returns:
        Normalized call list with status, call_type, start_date.
    """
    if not user_id:
        return []

    settings = get_settings()
    now = datetime.now(timezone.utc)
    if settings.report_since:
        date_from = f"{settings.report_since} 00:00:00"
    else:
        date_from = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    date_to = now.strftime("%Y-%m-%d %H:%M:%S")

    calls: list[dict[str, Any]] = []

    # === Approach 1: voximplant.statistic.get ===
    try:
        bx = _get_bitrix()
        vox_filter: dict[str, Any] = {
            "PORTAL_USER_ID": user_id,
            ">=CALL_START_DATE": date_from,
            "<=CALL_START_DATE": date_to,
        }
        if crm_entity_type and crm_entity_id:
            vox_filter["CRM_ENTITY_TYPE"] = crm_entity_type
            vox_filter["CRM_ENTITY_ID"] = crm_entity_id

        raw = bx.call(
            "voximplant.statistic.get",
            {
                "filter": vox_filter,
                "sort": "CALL_START_DATE",
                "order": "ASC",
            },
        )
        records = _as_list(raw)
        if records:
            logger.info(
                "voximplant OK: user_id=%s, records=%d",
                user_id,
                len(records),
            )
            for record in records:
                call_type_num = str(record.get("CALL_TYPE") or "")
                call_type = (
                    "incoming"
                    if call_type_num == "2"
                    else "outgoing"
                    if call_type_num == "1"
                    else "unknown"
                )
                failed_code = _coerce_int(record.get("CALL_FAILED_CODE"))
                duration = _coerce_int(record.get("CALL_DURATION"))
                if duration == 0 or failed_code != 200:
                    status = "missed"
                elif duration > 30:
                    status = "success"
                else:
                    status = "other"

                calls.append(
                    {
                        "call_id": str(
                            record.get("CALL_ID") or record.get("ID") or "",
                        ),
                        "duration": duration,
                        "start_date": str(record.get("CALL_START_DATE") or ""),
                        "status": status,
                        "call_type": call_type,
                        "crm_entity_type": str(
                            record.get("CRM_ENTITY_TYPE") or "",
                        ),
                        "crm_entity_id": _coerce_int(
                            record.get("CRM_ENTITY_ID"),
                        ),
                    }
                )
            return calls
        logger.debug("voximplant empty for user_id=%s", user_id)
    except Exception as exc:
        logger.debug("voximplant failed for user_id=%s: %s", user_id, exc)

    # === Approach 2: crm.activity.list — full list + classification ===
    try:
        raw3 = _bx_get_all_sync(
            "crm.activity.list",
            {
                "filter": {
                    "PROVIDER_TYPE_ID": "CALL",
                    "RESPONSIBLE_ID": user_id,
                    ">=CREATED": date_from,
                    "<=CREATED": date_to,
                },
                "select": [
                    "ID",
                    "DIRECTION",
                    "CREATED",
                    "SUBJECT",
                    "DESCRIPTION",
                    "COMPLETED",
                    "RESULT_CODE",
                    "RESULT_SUMMARY",
                    "OWNER_TYPE_ID",
                    "OWNER_ID",
                ],
            },
        )
        records3 = raw3 if isinstance(raw3, list) else _as_list(raw3)
        if records3:
            logger.info(
                "activity.list OK: user_id=%s, records=%d",
                user_id,
                len(records3),
            )
            for item in records3:
                if not isinstance(item, dict):
                    continue
                direction = str(item.get("DIRECTION") or "")
                call_type = (
                    "incoming"
                    if direction == "1"
                    else "outgoing"
                    if direction == "2"
                    else "unknown"
                )
                completed = str(item.get("COMPLETED") or "")
                description = str(item.get("DESCRIPTION") or "").lower()
                subject = str(item.get("SUBJECT") or "").lower()
                combined = description + " " + subject

                is_missed_text = (
                    "missed" in combined
                    or "пропущен" in combined
                    or "не отвечен" in combined
                )
                if is_missed_text:
                    status = "missed"
                elif call_type == "incoming":
                    if completed == "Y":
                        status = "success"
                    else:
                        # SUBJECT «Входящий от…» не отличает пропущенный от принятого
                        status = "other"
                elif call_type == "outgoing":
                    status = "success" if completed == "Y" else "other"
                else:
                    status = "success" if completed == "Y" else "other"

                owner_type = _coerce_int(item.get("OWNER_TYPE_ID"))
                owner_id = _coerce_int(item.get("OWNER_ID"))
                crm_entity_type = ""
                crm_entity_id = 0
                if owner_type == 2 and owner_id > 0:
                    crm_entity_type = "DEAL"
                    crm_entity_id = owner_id
                elif owner_type == 1 and owner_id > 0:
                    crm_entity_type = "LEAD"
                    crm_entity_id = owner_id

                calls.append(
                    {
                        "call_id": str(item.get("ID") or ""),
                        "duration": 0,
                        "start_date": str(item.get("CREATED") or ""),
                        "status": status,
                        "call_type": call_type,
                        "subject_preview": str(item.get("SUBJECT") or "")[:100],
                        "description": (str(item.get("DESCRIPTION") or ""))[:80],
                        "completed": completed[:1] or "?",
                        "result_code": str(item.get("RESULT_CODE") or ""),
                        "crm_entity_type": crm_entity_type,
                        "crm_entity_id": crm_entity_id,
                    }
                )
            missed_count = sum(1 for c in calls if c.get("status") == "missed")
            logger.info(
                "activity.list classified: user_id=%s, total=%d, missed=%d",
                user_id,
                len(calls),
                missed_count,
            )
    except Exception as exc:
        logger.debug("activity.list all failed: %s", exc)

    return calls


def _fetch_status_names(entity_id: str) -> dict[str, str]:
    """Справочник crm.status.list: код → человеческое имя.

    Не получилось прочитать — работаем по кодам, это не повод падать.
    """
    try:
        raw = _bx_get_all_sync(
            "crm.status.list",
            {"filter": {"ENTITY_ID": entity_id}},
        )
    except Exception:
        logger.warning("Status name fetch failed for %s — showing raw codes", entity_id)
        return {}
    names: dict[str, str] = {}
    for item in _as_list(raw):
        if not isinstance(item, dict):
            continue
        sid = _clean_str(item.get("STATUS_ID"))
        name = _clean_str(item.get("NAME"))
        if sid and name:
            names[sid] = name
    return names


def fetch_source_names() -> dict[str, str]:
    """Источник сделки: код → человеческое имя.

    Нужен отчёту: «источник 26» РОПу ничего не говорит, «Диспозл 5%» говорит
    всё.
    """
    return _fetch_status_names("SOURCE")


def fetch_contact_type_names() -> dict[str, str]:
    """Тип контакта: код → имя («UC_2G0TD3» → «Собственник»).

    Коды типов на портале самодельные, и угадывать, какой из них означает
    агента, нельзя — имя приходится спрашивать у Битрикса.
    """
    return _fetch_status_names("CONTACT_TYPE")


def _fetch_lead_status_names() -> dict[str, str]:
    """Fetch lead status ID → human-readable name mapping.

    Returns:
        Dict status_id → status_name.
    """
    try:
        raw = _bx_get_all_sync(
            "crm.status.list",
            {"filter": {"ENTITY_ID": "STATUS"}},
        )
        result: dict[str, str] = {}
        for item in raw if isinstance(raw, list) else _as_list(raw):
            if isinstance(item, dict):
                sid = str(item.get("STATUS_ID") or "")
                name = str(item.get("NAME") or "")
                if sid and name:
                    result[sid] = name
        return result
    except Exception:
        logger.exception("_fetch_lead_status_names failed")
        return {}


def _build_crm_link(entity_type: str, entity_id: int) -> str:
    """Build Bitrix24 CRM link for a lead or deal.

    Args:
        entity_type: "lead" or "deal".
        entity_id: CRM entity ID.

    Returns:
        Full URL to CRM entity card.
    """
    settings = get_settings()
    url = settings.b24_webhook_url
    domain = url.split("/rest/")[0] if "/rest/" in url else url.rstrip("/")

    if entity_type == "lead":
        return f"{domain}/crm/lead/details/{entity_id}/"
    return f"{domain}/crm/deal/details/{entity_id}/"


def _fetch_entity_timeline(
    entity_id: int,
    entity_type: str,
) -> tuple[int, list[dict[str, Any]], bool]:
    """Fetch timeline for a single entity (lead or deal).

    Designed for use with ThreadPoolExecutor — creates its own
    Bitrix client per call for thread safety.

    Args:
        entity_id: CRM entity ID (lead or deal).
        entity_type: "lead" or "deal".

    Returns:
        Tuple of (entity_id, timeline_comments_list, fetch_failed). An empty
        timeline and a failed fetch must stay distinguishable: "no comments"
        is a violation, "could not read comments" is not.
    """
    try:
        bx = _get_bitrix()
        raw = bx.get_all(
            "crm.timeline.comment.list",
            {
                "filter": {
                    "ENTITY_ID": entity_id,
                    "ENTITY_TYPE": entity_type,
                },
                # FILES — вложения комментария. Нужны правилу «брокер написал,
                # что написал клиенту» → пусть приложит скриншот переписки.
                "select": ["ID", "AUTHOR_ID", "COMMENT", "CREATED", "FILES"],
            },
        )
        return (entity_id, _extract_comments(raw), False)
    except Exception:
        logger.warning(
            "Timeline fetch failed for %s id=%s — card excluded from audit",
            entity_type,
            entity_id,
        )
        return (entity_id, [], True)


def _fetch_deal_activities(deal_id: int) -> tuple[int, list[dict[str, Any]], bool]:
    """Fetch open+completed CRM activities owned by a deal (timeline evidence).

    Thread-pool safe: own Bitrix client per call.

    Returns (deal_id, activities, fetch_failed) — see _fetch_entity_timeline.
    """
    try:
        bx = _get_bitrix()
        raw = bx.get_all(
            "crm.activity.list",
            {
                "filter": {
                    "OWNER_TYPE_ID": 2,
                    "OWNER_ID": deal_id,
                },
                "select": [
                    "ID",
                    "OWNER_ID",
                    "OWNER_TYPE_ID",
                    "RESPONSIBLE_ID",
                    "AUTHOR_ID",
                    "CREATED",
                    "START_TIME",
                    "END_TIME",
                    "DEADLINE",
                    "COMPLETED",
                    "SUBJECT",
                    "DESCRIPTION",
                    # TYPE_ID отличает звонок от задачи, DIRECTION — входящий
                    # от исходящего. Без них «брокер звонил» не отличить от
                    # «брокер поставил себе задачу позвонить».
                    "TYPE_ID",
                    "DIRECTION",
                ],
            },
        )
        return (deal_id, _as_list(raw), False)
    except Exception:
        logger.warning(
            "Activity fetch failed for deal id=%s — card excluded from audit",
            deal_id,
        )
        return (deal_id, [], True)


@tool
def get_all_leads_with_timeline() -> dict[str, Any]:
    """Получить ВСЕ лиды с полным таймлайном комментариев.

    Использует: crm.lead.list → на каждый лид crm.timeline.comment.list

    Returns:
        {"leads": [...], "total": int}, где каждый лид имеет поле "timeline".
    """
    try:
        settings = get_settings()
        list_params: dict[str, Any] = {
            "select": [
                "ID",
                "TITLE",
                "STATUS_ID",
                "ASSIGNED_BY_ID",
                "DATE_CREATE",
                "COMMENTS",
                "SOURCE_ID",
                "OPENED",
            ],
        }
        if settings.report_since:
            list_params["filter"] = {
                ">=DATE_CREATE": settings.report_since,
            }

        leads_raw = _bx_get_all_sync("crm.lead.list", list_params)
        leads = leads_raw if isinstance(leads_raw, list) else _as_list(leads_raw)
        status_names = _fetch_lead_status_names()
        lead_records: dict[int, dict[str, Any]] = {}
        for lead in leads:
            if not isinstance(lead, dict):
                continue
            lead_id = _coerce_int(lead.get("ID"))
            status_id = _clean_str(lead.get("STATUS_ID"))
            lead_records[lead_id] = {
                "lead_id": lead_id,
                "title": _clean_str(lead.get("TITLE")),
                "status_id": status_id,
                "status_name": status_names.get(status_id, ""),
                "assigned_by_id": _coerce_int(lead.get("ASSIGNED_BY_ID")),
                "date_create": _clean_str(lead.get("DATE_CREATE")),
                "comments_field": _clean_str(lead.get("COMMENTS")),
                "source_id": _clean_str(lead.get("SOURCE_ID")),
                "opened": _clean_str(lead.get("OPENED")).upper(),
                "timeline": [],
                "calls": [],
            }

        with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_entity_timeline, lid, "lead"): lid
                for lid in lead_records
            }
            for future in as_completed(futures):
                lid = futures[future]
                try:
                    _, timeline, failed = future.result()
                    lead_records[lid]["timeline"] = timeline
                    lead_records[lid]["evidence_incomplete"] = failed
                except Exception:
                    logger.warning("Timeline future failed for lead %s", lid)
                    lead_records[lid]["evidence_incomplete"] = True

        calls_cache: dict[int, list[dict[str, Any]]] = {}
        for lid, record in lead_records.items():
            assigned_id = _coerce_int(record.get("assigned_by_id"))
            if not assigned_id:
                continue
            if assigned_id not in calls_cache:
                try:
                    calls_cache[assigned_id] = _fetch_user_calls_for_audit(
                        assigned_id,
                        hours_ago=720,
                    )
                except Exception:
                    logger.warning("Call history fetch failed for user %s", assigned_id)
                    calls_cache[assigned_id] = []
            # Только звонки этого лида: _fetch_user_calls_for_audit отдаёт всю
            # историю сотрудника, и без сужения один пропущенный звонок
            # становился нарушением на каждом лиде брокера.
            record["calls"] = _filter_calls_for_entity(
                calls_cache[assigned_id], "lead", lid,
            )

        result = list(lead_records.values())
        return {"leads": result, "total": len(result)}
    except Exception as exc:
        logger.exception("get_all_leads_with_timeline failed")
        return {"error": str(exc), "leads": [], "total": 0}


@tool
def get_deals_by_funnel_with_timeline(category_id: int) -> dict[str, Any]:
    """Получить ВСЕ сделки указанной воронки с полным таймлайном.

    Использует: crm.deal.list (filter: CATEGORY_ID) → на каждую сделку
    crm.timeline.comment.list

    Args:
        category_id: ID воронки (0 = Продавцы, 18 = Покупатели).

    Returns:
        {"deals": [...], "category_id": int, "total": int}.
    """
    try:
        settings = get_settings()
        deal_filter: dict[str, Any] = {
            "CATEGORY_ID": category_id,
            "CLOSED": "N",
        }
        if settings.report_since:
            deal_filter[">=DATE_CREATE"] = settings.report_since

        deals_raw = _bx_get_all_sync(
            "crm.deal.list",
            {
                "filter": deal_filter,
                "select": [
                    "ID",
                    "TITLE",
                    "STAGE_ID",
                    "ASSIGNED_BY_ID",
                    "DATE_CREATE",
                    "OPPORTUNITY",
                    "CATEGORY_ID",
                    "SOURCE_ID",
                    *DEAL_AUDIT_UF_FIELD_CODES,
                ],
            },
        )
        deals = deals_raw if isinstance(deals_raw, list) else _as_list(deals_raw)
        stage_names = _fetch_funnel_stage_names(category_id)
        is_sellers = category_id == settings.sellers_category_id
        deal_records: dict[int, dict[str, Any]] = {}
        for deal in deals:
            if not isinstance(deal, dict):
                continue
            deal_id = _coerce_int(deal.get("ID"))
            stage_id = _clean_str(deal.get("STAGE_ID"))
            stage_name = stage_names.get(stage_id, stage_id)
            audit_rule = (
                _buyers_audit_rule(stage_id)
                if category_id == settings.buyers_category_id
                else None
            )
            deal_records[deal_id] = {
                "deal_id": deal_id,
                "title": _clean_str(deal.get("TITLE")),
                "stage_id": stage_id,
                "stage_name": stage_name,
                "audit_rule": audit_rule,
                "assigned_by_id": _coerce_int(deal.get("ASSIGNED_BY_ID")),
                "date_create": _clean_str(deal.get("DATE_CREATE")),
                "opportunity": _coerce_float(deal.get("OPPORTUNITY")),
                "category_id": category_id,
                "source_id": _clean_str(deal.get("SOURCE_ID")),
                "timeline": [],
                "deal_activities": [],
                "calls": [],
                "uf_fields": _build_deal_uf_fields(deal),
            }

        lost_stage = _lost_stage_id_for_category(category_id)
        if lost_stage:
            lost_filter: dict[str, Any] = {
                "CATEGORY_ID": category_id,
                "STAGE_ID": lost_stage,
            }
            if settings.report_since:
                lost_filter[">=DATE_MODIFY"] = settings.report_since
            try:
                lost_raw = _bx_get_all_sync(
                    "crm.deal.list",
                    {
                        "filter": lost_filter,
                        "select": [
                            "ID",
                            "TITLE",
                            "STAGE_ID",
                            "ASSIGNED_BY_ID",
                            "DATE_CREATE",
                            "OPPORTUNITY",
                            "CATEGORY_ID",
                            "SOURCE_ID",
                            *DEAL_AUDIT_UF_FIELD_CODES,
                        ],
                    },
                )
                for deal in (
                    lost_raw if isinstance(lost_raw, list) else _as_list(lost_raw)
                ):
                    if not isinstance(deal, dict):
                        continue
                    deal_id = _coerce_int(deal.get("ID"))
                    if deal_id <= 0 or deal_id in deal_records:
                        continue
                    stage_id = _clean_str(deal.get("STAGE_ID"))
                    stage_name = stage_names.get(stage_id, stage_id)
                    audit_rule = (
                        _buyers_audit_rule(stage_id)
                        if category_id == settings.buyers_category_id
                        else None
                    )
                    deal_records[deal_id] = {
                        "deal_id": deal_id,
                        "title": _clean_str(deal.get("TITLE")),
                        "stage_id": stage_id,
                        "stage_name": stage_name,
                        "audit_rule": audit_rule,
                        "assigned_by_id": _coerce_int(deal.get("ASSIGNED_BY_ID")),
                        "date_create": _clean_str(deal.get("DATE_CREATE")),
                        "opportunity": _coerce_float(deal.get("OPPORTUNITY")),
                        "category_id": category_id,
                        "source_id": _clean_str(deal.get("SOURCE_ID")),
                        "timeline": [],
                        "deal_activities": [],
                        "calls": [],
                        "uf_fields": _build_deal_uf_fields(deal),
                    }
            except Exception:
                logger.warning(
                    "Failed to load lost-stage deals category_id=%s stage=%s",
                    category_id,
                    lost_stage,
                )

        with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_entity_timeline, did, "deal"): did
                for did in deal_records
            }
            for future in as_completed(futures):
                did = futures[future]
                try:
                    _, timeline, failed = future.result()
                    deal_records[did]["timeline"] = timeline
                    deal_records[did]["evidence_incomplete"] = failed
                except Exception:
                    logger.warning("Timeline future failed for deal %s", did)
                    deal_records[did]["evidence_incomplete"] = True

        if is_sellers:
            with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
                futures = {
                    pool.submit(_fetch_deal_activities, did): did
                    for did in deal_records
                }
                for future in as_completed(futures):
                    did = futures[future]
                    try:
                        _, activities, failed = future.result()
                        deal_records[did]["deal_activities"] = activities
                        if failed:
                            deal_records[did]["evidence_incomplete"] = True
                    except Exception:
                        logger.warning("Activity future failed for deal %s", did)
                        deal_records[did]["deal_activities"] = []
                        deal_records[did]["evidence_incomplete"] = True

        open_activities_map: dict[int, list[dict[str, Any]]] = {}
        try:
            activities_raw = _bx_get_all_sync(
                "crm.activity.list",
                {
                    "filter": {
                        "COMPLETED": "N",
                    },
                    "select": [
                        "ID",
                        "OWNER_ID",
                        "SUBJECT",
                        "DESCRIPTION",
                        "TYPE_ID",
                        "OWNER_TYPE_ID",
                        "START_TIME",
                        "END_TIME",
                        "DEADLINE",
                        "COMPLETED",
                        "COMMUNICATIONS",
                    ],
                },
            )
            for activity in _as_list(activities_raw):
                owner_id = _coerce_int(activity.get("OWNER_ID"))
                owner_type_id = _coerce_int(activity.get("OWNER_TYPE_ID"))
                if owner_type_id == 2 and owner_id > 0:
                    open_activities_map.setdefault(owner_id, []).append(activity)

                comms = activity.get("COMMUNICATIONS")
                if isinstance(comms, list):
                    for comm in comms:
                        if not isinstance(comm, dict):
                            continue
                        etype = str(comm.get("ENTITY_TYPE_ID") or "").upper()
                        eid = _coerce_int(comm.get("ENTITY_ID"))
                        if etype == "DEAL" and eid > 0:
                            open_activities_map.setdefault(eid, []).append(activity)
        except Exception as exc:
            logger.warning("Failed to load open activities for deals: %s", exc)

        for did, record in deal_records.items():
            record["open_activities"] = open_activities_map.get(did, [])

        calls_cache: dict[int, list[dict[str, Any]]] = {}
        responsible_activities_cache: dict[int, list[dict[str, Any]]] = {}
        for did, record in deal_records.items():
            assigned_id = _coerce_int(record.get("assigned_by_id"))
            if not assigned_id:
                continue
            if assigned_id not in calls_cache:
                try:
                    calls_cache[assigned_id] = _fetch_user_calls_for_audit(
                        assigned_id,
                        hours_ago=720,
                    )
                except Exception:
                    calls_cache[assigned_id] = []
            # Звонки сужаются до конкретной сделки в обеих воронках: нарушение
            # выносится карточке, значит и опираться должно на её звонки.
            record["calls"] = _filter_calls_for_deal(calls_cache[assigned_id], did)

            if assigned_id not in responsible_activities_cache:
                try:
                    acts_raw = _bx_get_all_sync(
                        "crm.activity.list",
                        {
                            "filter": {
                                "RESPONSIBLE_ID": assigned_id,
                                "COMPLETED": "N",
                            },
                            "select": [
                                "ID",
                                "OWNER_ID",
                                "OWNER_TYPE_ID",
                                "SUBJECT",
                                "DESCRIPTION",
                                "START_TIME",
                                "END_TIME",
                                "DEADLINE",
                                "COMPLETED",
                            ],
                        },
                    )
                    responsible_activities_cache[assigned_id] = _as_list(acts_raw)
                except Exception:
                    responsible_activities_cache[assigned_id] = []
            record["responsible_open_activities"] = _activities_for_deal(
                did, responsible_activities_cache[assigned_id],
            )

        _attach_stage_entered_at(deal_records)

        result = list(deal_records.values())
        return {
            "deals": result,
            "category_id": category_id,
            "total": len(result),
        }
    except Exception as exc:
        logger.exception(
            "get_deals_by_funnel_with_timeline failed category_id=%s",
            category_id,
        )
        return {"error": str(exc), "deals": [], "category_id": category_id, "total": 0}


def get_general_base_deals_with_timeline() -> dict[str, Any]:
    """Open deals in воронка «Общая база» (category 26) with timeline and activities.

    Does not apply REPORT_SINCE on DATE_CREATE: old deals moved into the pool
    must still be audited. No call fetch. Sets gb_moved_at from stage history.
    """
    category_id = GENERAL_BASE_CATEGORY_ID
    try:
        deals_raw = _bx_get_all_sync(
            "crm.deal.list",
            {
                "filter": {
                    "CATEGORY_ID": category_id,
                    "CLOSED": "N",
                },
                "select": [
                    "ID",
                    "TITLE",
                    "STAGE_ID",
                    "ASSIGNED_BY_ID",
                    "DATE_CREATE",
                    "CATEGORY_ID",
                    "SOURCE_ID",
                ],
            },
        )
        deals = deals_raw if isinstance(deals_raw, list) else _as_list(deals_raw)
        stage_names = _fetch_funnel_stage_names(category_id)
        deal_records: dict[int, dict[str, Any]] = {}
        for deal in deals:
            if not isinstance(deal, dict):
                continue
            deal_id = _coerce_int(deal.get("ID"))
            if deal_id <= 0:
                continue
            stage_id = _clean_str(deal.get("STAGE_ID"))
            stage_name = stage_names.get(stage_id, stage_id)
            deal_records[deal_id] = {
                "deal_id": deal_id,
                "title": _clean_str(deal.get("TITLE")),
                "stage_id": stage_id,
                "stage_name": stage_name,
                "assigned_by_id": _coerce_int(deal.get("ASSIGNED_BY_ID")),
                "date_create": _clean_str(deal.get("DATE_CREATE")),
                "category_id": category_id,
                "source_id": _clean_str(deal.get("SOURCE_ID")),
                "timeline": [],
                "deal_activities": [],
                "open_activities": [],
            }

        with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_entity_timeline, did, "deal"): did
                for did in deal_records
            }
            for future in as_completed(futures):
                did = futures[future]
                try:
                    _, timeline, failed = future.result()
                    deal_records[did]["timeline"] = timeline
                    deal_records[did]["evidence_incomplete"] = failed
                except Exception:
                    logger.warning("Timeline future failed for deal %s", did)
                    deal_records[did]["evidence_incomplete"] = True

        with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_deal_activities, did): did
                for did in deal_records
            }
            for future in as_completed(futures):
                did = futures[future]
                try:
                    _, activities, failed = future.result()
                    deal_records[did]["deal_activities"] = activities
                    if failed:
                        deal_records[did]["evidence_incomplete"] = True
                except Exception:
                    logger.warning("Activity future failed for deal %s", did)
                    deal_records[did]["deal_activities"] = []
                    deal_records[did]["evidence_incomplete"] = True

        _attach_general_base_moved_at(deal_records)

        result = list(deal_records.values())
        return {
            "deals": result,
            "category_id": category_id,
            "total": len(result),
        }
    except Exception as exc:
        logger.exception("get_general_base_deals_with_timeline failed")
        return {"error": str(exc), "deals": [], "category_id": category_id, "total": 0}
