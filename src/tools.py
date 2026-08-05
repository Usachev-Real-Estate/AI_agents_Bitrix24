"""Bitrix24 REST tools via fast_bitrix24."""

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from fast_bitrix24 import Bitrix
from langchain_core.tools import tool

from config import BX_EXECUTOR, Settings, get_settings

logger = logging.getLogger(__name__)

MAX_TIMELINE_WORKERS = 10

# Поля сделки для аудита воронки «Покупатели» (портал b24-po7frr)
DEAL_AUDIT_UF_FIELD_CODES = (
    "UF_CRM_1659375809326",  # Дата встречи
    "UF_CRM_1774361998551",  # Результат показа
)
DEAL_AUDIT_UF_LABELS: dict[str, str] = {
    "UF_CRM_1659375809326": "Дата встречи",
    "UF_CRM_1774361998551": "Результат показа",
}

# Воронка «Покупатели» (category_id=18): stage_id → правило аудита (1–5)
# Обновлено под воронку 2026-06: Подбор → Показ → Переговоры → … → Отложенный спрос
BUYERS_STAGE_AUDIT_RULE: dict[str, int] = {
    "C18:NEW": 2,           # Подбор — >5 дней без комментария (бывш. UC_V0DMMX)
    "C18:UC_UFPFKK": 3,     # Показ — >3 дней без комментария
    "C18:UC_DVW1P9": 4,     # Переговоры — >5 дней без комментария
    "C18:UC_L8NX87": 4,     # Дожим!!! — >5 дней без комментария
    "C18:UC_8Z3SP6": 4,     # Офер
    "C18:LOSE": 5,          # Отложенный спрос — >7 дней без коммент. ответственного
}
# Закрытые / служебные стадии — без аудита
BUYERS_SKIP_AUDIT_STAGES = frozenset({
    "C18:UC_RUCRAH",  # Задаток
    "C18:UC_8X12HI",  # Сделка
    "C18:WON",        # Договор закрыт
    "C18:APOLOGY",    # Сделка проиграна
    "C18:UC_2ZBA0G",  # Агент
})
BUYERS_STAGE_NAMES: dict[str, str] = {
    "C18:NEW": "Подбор",
    "C18:UC_UFPFKK": "Показ",
    "C18:UC_DVW1P9": "Переговоры",
    "C18:UC_L8NX87": "Дожим!!!",
    "C18:UC_8Z3SP6": "Офер",
    "C18:UC_RUCRAH": "Задаток",
    "C18:UC_8X12HI": "Сделка",
    "C18:WON": "Договор закрыт",
    "C18:LOSE": "Отложенный спрос",
    "C18:APOLOGY": "Сделка проиграна",
    "C18:UC_2ZBA0G": "Агент",
}

# Воронка «Продавцы» (category_id=0): платные источники КЦ/Диспозл
SELLERS_PAID_SOURCE_IDS = frozenset({"24", "25", "26"})
SELLERS_PAID_SOURCE_NAMES: dict[str, str] = {
    "24": "КЦ - 5%",
    "25": "Диспозл 10%",
    "26": "Диспозл 5%",
}
SELLER_STAGE_NEW = "NEW"  # Назначение встречи
SELLER_STAGE_DEFERRED = "LOSE"  # Отложенная продажа
SELLERS_STAGE_NAMES: dict[str, str] = {
    "NEW": "Назначение встречи",
    "FINAL_INVOICE": "Подготовка объекта в рекламу",
    "UC_A94BGF": "Закрытая продажа (На сайт)",
    "UC_FADPBF": "Поиск клиента",
    "WON": "Договор закрыт",
    "LOSE": "Отложенная продажа",
    "APOLOGY": "Сделка проиграна",
}
SELLER_MEETING_GRACE_HOURS = 24

# Что сделать — текст рядом с нарушением в отчёте отдела
SELLER_RULE_ACTIONS: dict[str, str] = {
    "seller_meeting_no_outgoing": (
        "Сделать исходящий звонок с рабочего номера телефона "
        "и перенести сделку на другой этап"
    ),
    "seller_meeting_not_advanced": (
        "Сделать исходящий звонок с рабочего номера телефона "
        "и перенести сделку на другой этап"
    ),
    "seller_source_no_outgoing": (
        "Сделать исходящий звонок с рабочего номера телефона "
        "и перенести сделку на другой этап"
    ),
    "seller_deferred_no_comment": "Добавить комментарий в карточку сделки",
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


def _get_bitrix() -> Bitrix:
    """Create Bitrix REST client from current settings.

    Returns:
        Configured Bitrix instance.
    """
    return Bitrix(get_settings().b24_webhook_url)


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
        for key in ("result", "comments", "calls", "leads", "tasks"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
            if isinstance(inner, dict):
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


def _fetch_funnel_stage_names(category_id: int) -> dict[str, str]:
    """Load stage_id → Russian name map for a deal category."""
    try:
        raw = _get_bitrix().call(
            "crm.dealcategory.stage.list",
            {"entityTypeId": 2, "id": category_id},
        )
        stages = raw if isinstance(raw, list) else _as_list(raw)
        return {
            _clean_str(item.get("STATUS_ID")): _clean_str(item.get("NAME"))
            for item in stages
            if item.get("STATUS_ID")
        }
    except Exception:
        logger.warning(
            "Failed to load stage names for category_id=%s",
            category_id,
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


def _days_since_last_comment_by_authors(
    timeline: list[dict[str, Any]],
    current: datetime,
    allowed_author_ids: set[int],
) -> int:
    """Days since last comment from allowed authors, or 999 if none."""
    authored = [
        item for item in timeline
        if _coerce_int(item.get("author_id")) in allowed_author_ids
        and str(item.get("comment") or "").strip()
    ]
    if not authored:
        return 999
    latest = max(authored, key=lambda item: str(item.get("created") or ""))
    created = _parse_datetime(latest.get("created"))
    if created is None:
        return 999
    return int(_days_between(created, current))


def _build_rop_map() -> dict[int, int]:
    """Build department_id → ROP user_id mapping."""
    try:
        rop_users = _bx_get_all_sync("user.get", {
            "FILTER": {
                "WORK_POSITION": "Руководитель отдела продаж (РОП)",
                "ACTIVE": True,
            },
        })
        rop_map: dict[int, int] = {}
        for user in _as_list(rop_users):
            if not isinstance(user, dict):
                continue
            uid = _coerce_int(user.get("ID"))
            depts = user.get("UF_DEPARTMENT", [])
            if isinstance(depts, list) and depts:
                dept_id = _coerce_int(depts[0])
                if dept_id:
                    rop_map[dept_id] = uid
        return rop_map
    except Exception:
        logger.warning("Failed to build ROP map, ROP comments won't be counted")
        return {}


def _build_broker_dept_map(broker_ids: set[int]) -> dict[int, int]:
    """Build broker user_id → primary department_id mapping."""
    if not broker_ids:
        return {}
    broker_dept_map: dict[int, int] = {}
    try:
        for bid in broker_ids:
            user_raw = _bx_get_all_sync("user.get", {"ID": bid})
            user = user_raw[0] if isinstance(user_raw, list) and user_raw else user_raw
            if isinstance(user, dict):
                depts = user.get("UF_DEPARTMENT", [])
                if isinstance(depts, list) and depts:
                    broker_dept_map[bid] = _coerce_int(depts[0])
    except Exception:
        logger.warning("Failed to load broker departments for ROP check")
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
            r"(\d{1,2})\s+(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)(?:\s+(\d{4}))?",
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


def seller_violation_action(violation: dict[str, Any]) -> str:
    """Return «что сделать» text for a seller violation (empty if unknown)."""
    rule = str(violation.get("rule") or "").strip()
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


def _filter_calls_for_deal(
    calls: list[dict[str, Any]],
    deal_id: int,
) -> list[dict[str, Any]]:
    """Keep only calls linked to the given deal."""
    if deal_id <= 0:
        return []
    linked: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        etype = str(call.get("crm_entity_type") or "").upper()
        eid = _coerce_int(call.get("crm_entity_id"))
        if etype in {"DEAL", "2"} and eid == deal_id:
            linked.append(call)
    return linked


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


def check_seller_deal_violations(
    deals: list[dict[str, Any]],
    current_time: str,
    rop_map: dict[int, int] | None = None,
    broker_dept_map: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic audit of seller-funnel deals (paid sources + deferred).

    Rules:
    - seller_meeting_no_outgoing: NEW + paid + >24h + no outgoing + no comment
    - seller_meeting_not_advanced: NEW + paid + has outgoing (still on NEW)
    - seller_deferred_no_comment: LOSE without broker/ROP comment
    - seller_source_no_outgoing: paid + >24h + no outgoing + no comment (non-NEW)
    """
    now = _parse_datetime(current_time) or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    violations: list[dict[str, Any]] = []

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
        hours = round(_hours_since_create(deal, now), 2)
        has_outgoing = _deal_has_outgoing_call(deal)
        has_comment = _has_comment_by_authors(
            deal.get("timeline") or [], allowed_authors,
        )
        is_paid = source_id in SELLERS_PAID_SOURCE_IDS
        base_details: dict[str, Any] = {
            "deal_id": _coerce_int(deal.get("deal_id")),
            "title": str(deal.get("title") or ""),
            "stage_id": stage_id,
            "stage_name": stage_name,
            "source_id": source_id,
            "source_name": source_name,
            "funnel": "sellers",
            "category_id": _coerce_int(deal.get("category_id")),
            "hours_since_creation": hours,
            "has_outgoing_call": has_outgoing,
            "has_broker_or_rop_comment": has_comment,
        }

        if stage_id == SELLER_STAGE_DEFERRED:
            days_since = _days_since_last_comment_by_authors(
                deal.get("timeline") or [], now, allowed_authors,
            )
            if days_since >= 999:
                violations.append(_violation(
                    deal,
                    "seller_deferred_no_comment",
                    (
                        f"На этапе «{stage_name}» нет комментария "
                        "ответственного или РОПа."
                    ),
                    {**base_details, "days_since_last_comment": days_since},
                    severity="medium",
                ))

        if not is_paid:
            continue

        if stage_id == SELLER_STAGE_NEW:
            if has_outgoing:
                violations.append(_violation(
                    deal,
                    "seller_meeting_not_advanced",
                    (
                        f"На этапе «{stage_name}» уже был исходящий звонок — "
                        "сделку нужно перевести дальше."
                    ),
                    base_details,
                    severity="high",
                ))
            elif hours > SELLER_MEETING_GRACE_HOURS and not has_comment:
                violations.append(_violation(
                    deal,
                    "seller_meeting_no_outgoing",
                    (
                        f"Сделка на этапе «{stage_name}» более "
                        f"{SELLER_MEETING_GRACE_HOURS} ч без исходящего звонка."
                    ),
                    base_details,
                    severity="high",
                ))
            continue

        if (
            hours > SELLER_MEETING_GRACE_HOURS
            and not has_outgoing
            and not has_comment
        ):
            violations.append(_violation(
                deal,
                "seller_source_no_outgoing",
                (
                    f"На этапе «{stage_name}» нет исходящего звонка брокера "
                    f"(более {SELLER_MEETING_GRACE_HOURS} ч)."
                ),
                base_details,
                severity="high",
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
            violations.append({
                "entity_type": entity_type,
                "entity_id": entity.get(id_field),
                "responsible_id": entity.get("assigned_by_id"),
                "severity": "very high",
                "rule": f"{entity_type}_missed_callback",
                "reason": "Пропущенный звонок без обратного",
                "details": {},
            })

    return violations


def _lead_needs_llm_check(lead: dict[str, Any]) -> bool:
    """Возвращает True, если лид требует LLM-анализа (rule_2 или rule_3)."""
    status_id = _lead_status_id(lead)

    # rule_2 (Спам / JUNK) — LLM проверяет обоснование
    if _is_lead_spam_status(status_id):
        return True

    # rule_3 (Нецелевой и прочие промежуточные стадии)
    if status_id in LEAD_SKIP_LLM_STATUS_IDS:
        return False

    return True


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


def check_buyer_deal_violations(
    deals: list[dict[str, Any]],
    current_time: str,
    rop_map: dict[int, int] | None = None,
    broker_dept_map: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic audit of buyer funnel deals (rules 1–5 by audit_rule).

    Each deal is checked against exactly one rule matching its audit_rule field.
    Comments count only from the responsible broker or their ROP.
    """
    now = _parse_datetime(current_time) or datetime.now(timezone.utc)
    violations: list[dict[str, Any]] = []

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
        days_on_stage = round(_days_between(deal.get("date_create"), now), 2)
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
            if days_on_stage > 1:
                violations.append(_violation(
                    deal,
                    "buyer_stage_1",
                    f"Сделка находится на этапе «{stage_name}» более 1 дня.",
                    {**base_details, "days_on_stage": days_on_stage},
                ))

        elif rule_num == 2:
            days_since_comment = _days_since_last_comment_by_authors(
                timeline, now, allowed_comment_authors,
            )
            if days_since_comment > 2:
                deal_open_activities = deal.get("open_activities") or []
                if not isinstance(deal_open_activities, list):
                    deal_open_activities = []
                responsible_open_activities = deal.get("responsible_open_activities") or []
                if not isinstance(responsible_open_activities, list):
                    responsible_open_activities = []

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

                if days_since_comment >= 999:
                    if has_overdue_contact_plan:
                        reason = (
                            f"На этапе «{stage_name}» нет комментария более 2 дней, "
                            "а запланированное дело по связи с клиентом просрочено."
                        )
                    else:
                        reason = (
                            f"Сделка находится на этапе «{stage_name}» более 2 дней "
                            "без комментария."
                        )
                else:
                    if has_overdue_contact_plan:
                        reason = (
                            f"Сделка находится на этапе «{stage_name}», последний комментарий "
                            f"более {days_since_comment} дней назад, "
                            "а запланированное дело по связи с клиентом просрочено."
                        )
                    else:
                        reason = (
                            f"Сделка находится на этапе «{stage_name}», последний комментарий "
                            f"более {days_since_comment} дней назад."
                        )
                violations.append(_violation(
                    deal,
                    "buyer_stage_2",
                    reason,
                    {
                        **base_details,
                        "days_since_last_comment": days_since_comment,
                        "has_contact_plan_activity": has_contact_plan,
                        "has_overdue_contact_plan_activity": has_overdue_contact_plan,
                    },
                ))

        elif rule_num == 3:
            uf_fields = deal.get("uf_fields") if isinstance(deal.get("uf_fields"), dict) else {}
            show_date_uf = _show_date_from_uf(uf_fields)
            show_date_comment = _show_date_from_timeline_comments(
                timeline, now, allowed_comment_authors,
            )
            show_date = show_date_uf or show_date_comment
            has_show_plan_comment = _has_show_plan_comment(
                timeline, allowed_comment_authors,
            )
            open_activities = deal.get("open_activities") or []
            if not isinstance(open_activities, list):
                open_activities = []
            has_planned_activity = bool(open_activities)
            has_show_date = show_date is not None
            is_show_date_passed = bool(show_date and show_date <= now)
            overdue_activities = [
                activity for activity in open_activities
                if (due := _open_activity_due_datetime(activity)) is not None and due <= now
            ]
            has_overdue_activity = bool(overdue_activities)
            has_active_planned_activity = has_planned_activity and not has_overdue_activity

            should_flag = False
            if has_overdue_activity:
                should_flag = True
            elif not has_show_date and not has_active_planned_activity:
                should_flag = not has_show_plan_comment
            elif is_show_date_passed and not has_active_planned_activity:
                should_flag = True

            if should_flag:
                reason_parts: list[str] = []
                if not has_planned_activity:
                    reason_parts.append("нет запланированного дела")
                if not has_show_date:
                    reason_parts.append("не заполнена дата показа")
                if has_overdue_activity:
                    reason_parts.append("запланированное дело просрочено")
                if is_show_date_passed:
                    reason_parts.append("дата показа уже прошла")
                reasons_text = "; ".join(reason_parts)
                reason = (
                    f"На этапе «{stage_name}» {reasons_text}. "
                    "Сделку нужно перенести на другой этап."
                )
                violations.append(_violation(
                    deal,
                    "buyer_stage_3",
                    reason,
                    {
                        **base_details,
                        "has_planned_activity": has_planned_activity,
                        "has_show_date": has_show_date,
                        "has_overdue_activity": has_overdue_activity,
                        "is_show_date_passed": is_show_date_passed,
                        "has_active_planned_activity": has_active_planned_activity,
                        "show_date_source": (
                            "uf_fields"
                            if show_date_uf is not None
                            else "timeline_comment"
                            if show_date_comment is not None
                            else "missing"
                        ),
                        "has_show_plan_comment": has_show_plan_comment,
                    },
                    severity="high",
                ))

        elif rule_num == 4:
            days_since_comment = _days_since_last_comment_by_authors(
                timeline, now, allowed_comment_authors,
            )
            if days_since_comment > 5:
                if days_since_comment >= 999:
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
            days_since = _days_since_last_comment_by_authors(
                timeline, now, allowed_comment_authors,
            )
            if days_since > 7:
                violations.append(_violation(
                    deal,
                    "buyer_stage_5",
                    f"На этапе «{stage_name}» нет комментария брокера или РОП более 7 дней.",
                    {
                        **base_details,
                        "days_since_last_comment": days_since,
                    },
                ))

    return violations


def humanize_violation_reason(
    violation: dict[str, Any],
    *,
    buyers_deals: list[dict[str, Any]] | None = None,
    sellers_deals: list[dict[str, Any]] | None = None,
    leads: list[dict[str, Any]] | None = None,
) -> str:
    """Replace CRM stage/status codes in violation reason with Russian names."""
    reason = str(violation.get("reason") or "—")
    code_to_name: dict[str, str] = dict(BUYERS_STAGE_NAMES)

    for deals in (buyers_deals or []), (sellers_deals or []):
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

    details = violation.get("details")
    if isinstance(details, dict):
        stage_name = details.get("stage_name") or details.get("status_name")
        stage_id = str(details.get("stage_id") or details.get("status_id") or "")
        if stage_id and stage_name and stage_id != stage_name:
            code_to_name[stage_id] = str(stage_name)

    entity_id = _coerce_int(violation.get("entity_id", 0))
    entity_type = violation.get("entity_type")
    if entity_type == "deal":
        for deals in (buyers_deals or []), (sellers_deals or []):
            for deal in deals:
                if _coerce_int(deal.get("deal_id")) == entity_id:
                    sid = str(deal.get("stage_id") or "")
                    sname = str(deal.get("stage_name") or "")
                    if sid and sname and sid != sname:
                        code_to_name[sid] = sname
    elif entity_type == "lead":
        for lead in leads or []:
            if _coerce_int(lead.get("lead_id")) == entity_id:
                sid = str(lead.get("status_id") or "")
                sname = str(lead.get("status_name") or "")
                if sid and sname and sid != sname:
                    code_to_name[sid] = sname

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
        comments.append(
            {
                "author_id": int(item.get("AUTHOR_ID") or item.get("authorId") or 0),
                "comment": _clean_str(item.get("COMMENT") or item.get("comment")),
                "created": _clean_str(item.get("CREATED") or item.get("created")),
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
) -> tuple[int, list[dict[str, Any]]]:
    """Fetch timeline for a single entity (lead or deal).

    Designed for use with ThreadPoolExecutor — creates its own
    Bitrix client per call for thread safety.

    Args:
        entity_id: CRM entity ID (lead or deal).
        entity_type: "lead" or "deal".

    Returns:
        Tuple of (entity_id, timeline_comments_list).
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
                "select": ["ID", "AUTHOR_ID", "COMMENT", "CREATED"],
            },
        )
        return (entity_id, _extract_comments(raw))
    except Exception:
        logger.debug(
            "Timeline fetch failed for %s id=%s",
            entity_type,
            entity_id,
        )
        return (entity_id, [])


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
                    _, timeline = future.result()
                    lead_records[lid]["timeline"] = timeline
                except Exception:
                    pass

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
                    calls_cache[assigned_id] = []
            record["calls"] = calls_cache[assigned_id]

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
                "calls": [],
                "uf_fields": _build_deal_uf_fields(deal),
            }

        with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_entity_timeline, did, "deal"): did
                for did in deal_records
            }
            for future in as_completed(futures):
                did = futures[future]
                try:
                    _, timeline = future.result()
                    deal_records[did]["timeline"] = timeline
                except Exception:
                    pass

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
            user_calls = calls_cache[assigned_id]
            # Sellers audit needs deal-linked calls; buyers keep broker-wide list
            # for missed-callback logic (existing behavior).
            if is_sellers:
                record["calls"] = _filter_calls_for_deal(user_calls, did)
            else:
                record["calls"] = user_calls

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
            record["responsible_open_activities"] = responsible_activities_cache[assigned_id]

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
