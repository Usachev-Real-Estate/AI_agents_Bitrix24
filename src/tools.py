"""Bitrix24 REST tools via fast_bitrix24."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from fast_bitrix24 import Bitrix
from langchain_core.tools import tool

from config import Settings, get_settings

logger = logging.getLogger(__name__)

_BX_EXECUTOR = ThreadPoolExecutor(max_workers=4)
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

# Воронка «Покупатели» (category_id=18): точные stage_id → правило аудита (1–5)
BUYERS_STAGE_AUDIT_RULE: dict[str, int] = {
    "C18:NEW": 1,
    "C18:UC_V0DMMX": 2,
    "C18:UC_UFPFKK": 3,
    "C18:UC_A15GLR": 4,
    "C18:LOSE": 5,
}
BUYERS_STAGE_NAMES: dict[str, str] = {
    "C18:NEW": "Первый контакт",
    "C18:UC_V0DMMX": "Подбор",
    "C18:UC_UFPFKK": "Показ",
    "C18:UC_A15GLR": "Показ проведен",
    "C18:LOSE": "Отложенный спрос",
}


def is_mutation_allowed(settings: Settings) -> bool:
    """Check whether mutating Bitrix24 API calls are permitted.

    Args:
        settings: Application settings.

    Returns:
        True if mutations are allowed (DRY_RUN is false).
    """
    return not settings.dry_run


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

    return _BX_EXECUTOR.submit(_run).result()


def _parse_b24_datetime(value: str | None) -> datetime | None:
    """Parse Bitrix24 datetime string to timezone-aware datetime.

    Args:
        value: Raw date string from API.

    Returns:
        Parsed datetime in UTC, or None if parsing failed.
    """
    if not value:
        return None
    normalized = value.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
        value = deal.get(code)
        if value in (None, "", [], False):
            labeled[label] = None
        else:
            labeled[label] = value
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
    return BUYERS_STAGE_AUDIT_RULE.get(stage_id)


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
            if stage_id and stage_name:
                code_to_name[stage_id] = stage_name

    for lead in leads or []:
        status_id = str(lead.get("status_id") or "")
        status_name = str(lead.get("status_name") or "")
        if status_id and status_name:
            code_to_name[status_id] = status_name

    details = violation.get("details")
    if isinstance(details, dict):
        stage_name = details.get("stage_name") or details.get("status_name")
        stage_id = str(details.get("stage_id") or details.get("status_id") or "")
        if stage_id and stage_name:
            code_to_name[stage_id] = str(stage_name)

    entity_id = _coerce_int(violation.get("entity_id", 0))
    entity_type = violation.get("entity_type")
    if entity_type == "deal":
        for deals in (buyers_deals or []), (sellers_deals or []):
            for deal in deals:
                if _coerce_int(deal.get("deal_id")) == entity_id:
                    sid = str(deal.get("stage_id") or "")
                    sname = str(deal.get("stage_name") or "")
                    if sid and sname:
                        code_to_name[sid] = sname
    elif entity_type == "lead":
        for lead in leads or []:
            if _coerce_int(lead.get("lead_id")) == entity_id:
                sid = str(lead.get("status_id") or "")
                sname = str(lead.get("status_name") or "")
                if sid and sname:
                    code_to_name[sid] = sname

    for code in sorted(code_to_name, key=len, reverse=True):
        if code and code in reason:
            reason = reason.replace(code, f"«{code_to_name[code]}»")

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
def get_deal_context(deal_id: int) -> dict[str, Any]:
    """Получить контекст сделки: поля + комментарии из таймлайна.

    Использует:
      - crm.deal.get(id=deal_id)
      - crm.timeline.comment.list(filter={"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal"})

    Args:
        deal_id: CRM deal identifier.

    Returns:
        Deal fields and timeline comments, or error dict on failure.
    """
    try:
        bx = _get_bitrix()
        deal_raw = bx.call("crm.deal.get", {"id": deal_id})
        if not isinstance(deal_raw, dict):
            return {"error": "Unexpected deal response format", "deal_id": deal_id}

        comments_raw = bx.call(
            "crm.timeline.comment.list",
            {
                "filter": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal"},
                "select": ["ID", "AUTHOR_ID", "COMMENT", "CREATED"],
            },
        )
        comments = _extract_comments(comments_raw)

        return {
            "deal_id": deal_id,
            "title": str(deal_raw.get("TITLE") or ""),
            "stage_id": str(deal_raw.get("STAGE_ID") or ""),
            "opportunity": _coerce_float(deal_raw.get("OPPORTUNITY")),
            "assigned_by_id": _coerce_int(deal_raw.get("ASSIGNED_BY_ID")),
            "date_create": str(deal_raw.get("DATE_CREATE") or ""),
            "source_id": str(deal_raw.get("SOURCE_ID") or ""),
            "comments": comments,
        }
    except Exception as exc:
        logger.exception("get_deal_context failed for deal_id=%s", deal_id)
        return {"error": str(exc), "deal_id": deal_id}


@tool
def check_calls(user_id: int, hours_ago: int = 24) -> dict[str, Any]:
    """Проверить исходящие звонки менеджера за период.

    Использует: crm.activity.list (звонки)

    Args:
        user_id: ID менеджера в Битрикс24.
        hours_ago: Проверить звонки за последние N часов (по умолчанию 24).

    Returns:
        Call statistics and call list, or error dict on failure.
    """
    try:
        now = datetime.now(timezone.utc)
        date_from = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        date_to = now.strftime("%Y-%m-%d %H:%M:%S")

        calls_out = _fetch_user_calls_for_audit(
            user_id,
            hours_ago=hours_ago,
            crm_entity_type="DEAL",
        )
        successful = sum(1 for c in calls_out if c.get("status") == "success")

        return {
            "user_id": user_id,
            "period_hours": hours_ago,
            "date_from": date_from,
            "date_to": date_to,
            "total_calls": len(calls_out),
            "successful_calls": successful,
            "calls": calls_out,
        }
    except Exception as exc:
        logger.exception("check_calls failed for user_id=%s", user_id)
        return {"error": str(exc), "user_id": user_id}


@tool
def check_lead_qualification(max_hours: int = 1) -> dict[str, Any]:
    """Проверить время квалификации необработанных лидов.

    Использует: crm.lead.list

    Args:
        max_hours: Максимально допустимое время (часы) до квалификации.

    Returns:
        Count of unprocessed leads and violation list, or error dict.
    """
    try:
        bx = _get_bitrix()
        leads = bx.get_all(
            "crm.lead.list",
            {
                "filter": {"STATUS_ID": "NEW"},
                "select": ["ID", "TITLE", "DATE_CREATE", "STATUS_ID", "ASSIGNED_BY_ID"],
            },
        )
        now = datetime.now(timezone.utc)
        violations: list[dict[str, Any]] = []

        for lead in leads:
            created = _parse_b24_datetime(str(lead.get("DATE_CREATE") or ""))
            if created is None:
                continue
            hours_since = (now - created).total_seconds() / 3600
            if hours_since > max_hours:
                violations.append(
                    {
                        "lead_id": _coerce_int(lead.get("ID")),
                        "title": str(lead.get("TITLE") or ""),
                        "hours_since_creation": round(hours_since, 2),
                        "status": str(lead.get("STATUS_ID") or ""),
                    }
                )

        return {
            "unprocessed_leads": len(leads),
            "max_hours": max_hours,
            "violations": violations,
        }
    except Exception as exc:
        logger.exception("check_lead_qualification failed")
        return {"error": str(exc)}


@tool
def create_violation_task(user_id: int, deal_id: int, description: str) -> dict[str, Any]:
    """Поставить задачу брокеру-нарушителю.

    Использует: tasks.task.add

    Args:
        user_id: ID брокера-нарушителя (ответственный).
        deal_id: ID связанной сделки.
        description: Описание нарушения.

    Returns:
        Created task info, dry_run_skipped, or error dict.
    """
    settings = get_settings()
    if not is_mutation_allowed(settings):
        logger.info(
            "DRY_RUN: skip create_violation_task user_id=%s deal_id=%s",
            user_id,
            deal_id,
        )
        return {"status": "dry_run_skipped", "would_create_for": user_id}

    try:
        bx = _get_bitrix()
        result = bx.call(
            "tasks.task.add",
            {
                "fields": {
                    "TITLE": f"Нарушение регламента по сделке #{deal_id}",
                    "DESCRIPTION": description,
                    "RESPONSIBLE_ID": user_id,
                    "UF_CRM_TASK": [f"D_{deal_id}"],
                },
            },
        )
        task_id = 0
        if isinstance(result, dict):
            task = result.get("task") or result
            if isinstance(task, dict):
                task_id = _coerce_int(task.get("id") or task.get("ID"))
            else:
                task_id = _coerce_int(result.get("task_id") or result.get("ID"))
        logger.info("Created violation task id=%s for deal_id=%s", task_id, deal_id)
        return {"task_id": task_id, "status": "created", "deal_id": deal_id}
    except Exception as exc:
        logger.exception("create_violation_task failed deal_id=%s", deal_id)
        return {"error": str(exc), "deal_id": deal_id}


@tool
def get_active_deals(limit: int = 50) -> dict[str, Any]:
    """Получить список активных сделок из CRM (не в финальных стадиях).

    Использует: crm.deal.list

    Args:
        limit: Максимальное количество возвращаемых сделок (по умолчанию 50).

    Returns:
        Список активных сделок с базовыми полями, или error dict.
    """
    try:
        bx = _get_bitrix()
        deals = bx.get_all(
            "crm.deal.list",
            {
                "filter": {
                    "!STAGE_ID": ["C2:WON", "C2:LOSE"],
                    "CLOSED": "N",
                },
                "select": [
                    "ID",
                    "TITLE",
                    "STAGE_ID",
                    "ASSIGNED_BY_ID",
                    "DATE_CREATE",
                    "OPPORTUNITY",
                ],
            },
        )
        if isinstance(deals, list):
            deals = sorted(
                [d for d in deals if isinstance(d, dict)],
                key=lambda d: str(d.get("DATE_CREATE") or ""),
                reverse=True,
            )
        else:
            deals = _as_list(deals)
        result: list[dict[str, Any]] = []
        for deal in deals[:limit]:
            result.append(
                {
                    "deal_id": _coerce_int(deal.get("ID")),
                    "title": str(deal.get("TITLE") or ""),
                    "stage_id": str(deal.get("STAGE_ID") or ""),
                    "assigned_by_id": _coerce_int(deal.get("ASSIGNED_BY_ID")),
                    "date_create": str(deal.get("DATE_CREATE") or ""),
                    "opportunity": _coerce_float(deal.get("OPPORTUNITY")),
                }
            )
        return {"deals": result, "total": len(result)}
    except Exception as exc:
        logger.exception("get_active_deals failed")
        return {"error": str(exc), "deals": [], "total": 0}


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
                    *DEAL_AUDIT_UF_FIELD_CODES,
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

        for did, record in deal_records.items():
            assigned_id = _coerce_int(record.get("assigned_by_id"))
            if not assigned_id:
                continue
            try:
                record["calls"] = _fetch_user_calls_for_audit(
                    assigned_id,
                    hours_ago=720,
                )
            except Exception:
                record["calls"] = []

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
