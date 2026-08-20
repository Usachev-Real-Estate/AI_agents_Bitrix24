"""LangGraph workflow: v2 collectors -> analysts -> report dispatcher."""

import logging
import operator
from datetime import datetime, timezone
from functools import partial
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from config import Settings
from notify import _bx_call_sync, send_chat_message_chunked, send_user_chat_message
from tools import (
    _build_crm_link,
    _build_rop_map,
    _coerce_int,
    _is_general_base_violation,
    _is_seller_violation,
    _lead_status_id,
    _severity_icon,
    seller_violation_action,
    check_buyer_deal_violations,
    check_general_base_violations,
    check_lead_rule1_violations,
    check_lead_rule2_rule3_violations,
    check_missed_callback_violations,
    check_seller_deal_violations,
    get_all_leads_with_timeline,
    get_deals_by_funnel_with_timeline,
    get_general_base_deals_with_timeline,
    build_stage_name_index,
    count_incomplete,
    humanize_violation_reason,
    list_seller_meeting_reminders,
    process_deals_to_general_base,
    process_stale_new_leads,
    GENERAL_BASE_RULE_ACTION,
    LEAD_STATUS_NEW,
    LEAD_STATUS_SHARED,
    LEAD_STATUS_CONVERTED,
    LEAD_STATUS_JUNK,
    LEAD_STATUS_NECELEVOY,
    LEAD_STATUS_AGENT,
    NO_COMMENT_DAYS,
)

logger = logging.getLogger(__name__)

ALLOWED_VIOLATION_RULES = {
    "lead_rule_1",
    "lead_rule_2",
    "lead_rule_3",
    "lead_new_over_24h",
    "lead_missed_callback",
    "buyer_stage_1",
    "buyer_stage_2",
    "buyer_stage_3",
    "buyer_stage_4",
    "buyer_stage_5",
    "buyer_podbor_stale",
    "buyer_ofer_comment",
    "buyer_lost_no_reason",
    "buyer_agent_no_comment",
    "buyer_missed_callback",
    "seller_stage_stale",
    "seller_deferred_no_activity",
    "seller_negotiations_max",
    "seller_lost_no_reason",
    "seller_afina_id_missing",
    "general_base_no_plan",
}

NON_RULE_REASON_MARKERS = (
    "создан лид",
    "лид не создан",
    "создать лид",
    "вместо сделки",
)


class AuditState(TypedDict, total=False):
    """LangGraph state v2: multi-agent CRM audit."""

    raw_leads: list[dict[str, Any]]
    raw_buyers_deals: list[dict[str, Any]]
    raw_sellers_deals: list[dict[str, Any]]
    raw_general_base_deals: list[dict[str, Any]]
    violations: Annotated[list[dict[str, Any]], operator.add]
    current_time: str
    dry_run: bool
    status: str
    messages: Annotated[list[str], operator.add]
    report_sent: bool
    last_violation_count: int
    # Карточки, исключённые из аудита из-за нечитаемых доказательств.
    # Ключ обязан быть объявлен здесь: LangGraph отбрасывает всё,
    # чего нет в схеме состояния.
    skipped_incomplete: int


def _is_reportable_days(value: Any) -> bool:
    """True when a details day counter is a real number worth showing.

    NO_COMMENT_DAYS is the «no comment at all» sentinel and is never printed.
    Non-numeric values are rejected outright: comparing a str with an int
    raises TypeError, which used to abort the whole dispatcher send loop.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value < NO_COMMENT_DAYS


def _filter_zero_entity_violations(
    violations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop violations with entity_id=0 (dispatcher-only filter)."""
    return [v for v in violations if _coerce_int(v.get("entity_id", 0)) > 0]


def _is_supported_rule_violation(v: dict[str, Any]) -> bool:
    """Return True only for rules supported by the current audit policy."""
    rule = str(v.get("rule") or "").strip()
    if rule not in ALLOWED_VIOLATION_RULES:
        return False

    reason = str(v.get("reason") or "").strip().lower()
    if any(marker in reason for marker in NON_RULE_REASON_MARKERS):
        return False

    # Business rule: if deal owner differs from call receiver,
    # lead creation is valid and must not be treated as a violation.
    details = v.get("details")
    if isinstance(details, dict):
        deal_owner_id = _coerce_int(details.get("deal_responsible_id", 0))
        call_receiver_id = _coerce_int(details.get("call_responsible_id", 0))
        if deal_owner_id and call_receiver_id and deal_owner_id != call_receiver_id:
            return False

    return True


def _sanitize_violations(
    violations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only reportable, policy-compliant violations."""
    return [v for v in violations if _is_supported_rule_violation(v)]


async def lead_collector(state: AuditState, settings: Settings) -> AuditState:
    """Agent 1: collect all leads + timeline."""
    logger.info("Agent 1 (Lead Collector): fetching all leads with timeline")

    data = get_all_leads_with_timeline.invoke({})
    leads = data.get("leads", []) if isinstance(data, dict) else []

    logger.info("Agent 1: collected %d leads", len(leads))

    return {
        "raw_leads": leads,
        "messages": [f"lead_collector: {len(leads)} leads"],
    }


async def buyer_collector(state: AuditState, settings: Settings) -> AuditState:
    """Agent 2: collect buyer deals + timeline."""
    category_id = settings.buyers_category_id
    logger.info(
        "Agent 2 (Buyer Collector): fetching deals for category_id=%d",
        category_id,
    )

    data = get_deals_by_funnel_with_timeline.invoke({"category_id": category_id})
    deals = data.get("deals", []) if isinstance(data, dict) else []

    logger.info("Agent 2: collected %d buyer deals", len(deals))

    return {
        "raw_buyers_deals": deals,
        "messages": [f"buyer_collector: {len(deals)} deals (cat={category_id})"],
    }


async def seller_collector(state: AuditState, settings: Settings) -> AuditState:
    """Agent 3: collect seller deals + timeline."""
    category_id = settings.sellers_category_id
    logger.info(
        "Agent 3 (Seller Collector): fetching deals for category_id=%d",
        category_id,
    )

    data = get_deals_by_funnel_with_timeline.invoke({"category_id": category_id})
    deals = data.get("deals", []) if isinstance(data, dict) else []

    logger.info("Agent 3: collected %d seller deals", len(deals))

    return {
        "raw_sellers_deals": deals,
        "messages": [f"seller_collector: {len(deals)} deals (cat={category_id})"],
    }


async def general_base_collector(state: AuditState, settings: Settings) -> AuditState:
    """Collect open deals in воронка «Общая база» (category 26)."""
    logger.info("General Base Collector: fetching category 26 deals")

    data = get_general_base_deals_with_timeline()
    deals = data.get("deals", []) if isinstance(data, dict) else []

    logger.info("General Base Collector: collected %d deals", len(deals))

    return {
        "raw_general_base_deals": deals,
        "messages": [f"general_base_collector: {len(deals)} deals"],
    }


async def lead_analyst(state: AuditState, settings: Settings) -> AuditState:
    """Agent 4: analyze leads (rules 1–3) and move NEW > 24h to shared pool."""
    leads = state.get("raw_leads", [])
    logger.info(
        "Agent 4 (Lead Analyst): analyzing %d leads",
        len(leads),
    )

    if not leads:
        return {"violations": []}

    current_time_str = state.get("current_time", "")
    try:
        current_time = datetime.fromisoformat(current_time_str)
    except ValueError:
        current_time = datetime.now(timezone.utc)
    if not current_time.tzinfo:
        current_time = current_time.replace(tzinfo=timezone.utc)

    all_violations = check_lead_rule1_violations(leads, current_time)
    all_violations.extend(check_lead_rule2_rule3_violations(leads))
    all_violations.extend(process_stale_new_leads(leads, current_time))

    sanitized = _sanitize_violations(all_violations)
    logger.info(
        "Agent 4: found %d lead violations total (%d after policy filter)",
        len(all_violations),
        len(sanitized),
    )
    return {"violations": sanitized}


async def buyer_deal_analyst(state: AuditState, settings: Settings) -> AuditState:
    """Agent 5: analyze buyer deals for stage violations (deterministic rules 1–5)."""
    deals = state.get("raw_buyers_deals", [])
    logger.info("Agent 5 (Buyer Deal Analyst): checking %d deals", len(deals))

    if not deals:
        return {"violations": []}

    current_time = state.get("current_time", "")
    rop_map = _build_rop_map()
    all_violations = check_buyer_deal_violations(deals, current_time, rop_map)
    # Перенос в «Общую базу» выполняет диспетчер — после отсева уволенных
    # сотрудников и исключённых отделов. Иначе сделка уезжала бы молча,
    # не попав ни в один отчёт.
    logger.info("Agent 5: found %d buyer deal violations total", len(all_violations))
    return {"violations": all_violations}


async def seller_deal_analyst(state: AuditState, settings: Settings) -> AuditState:
    """Analyze seller deals: cadence, deferred activity, Afina ID, meeting reminders."""
    deals = state.get("raw_sellers_deals", [])
    logger.info("Seller Deal Analyst: checking %d deals", len(deals))

    if not deals:
        return {"violations": []}

    current_time = state.get("current_time", "")
    rop_map = _build_rop_map()
    all_violations = check_seller_deal_violations(deals, current_time, rop_map)
    logger.info("Seller Deal Analyst: found %d seller deal violations", len(all_violations))

    reminders = list_seller_meeting_reminders(deals, current_time, rop_map)
    if reminders:
        logger.info(
            "Seller Deal Analyst: %d meeting reminders (2h before 24h)",
            len(reminders),
        )
    if settings.dry_run:
        for item in reminders:
            logger.info(
                "DRY_RUN: would remind user %s about deal %s (%.1fh left)",
                item.get("assigned_by_id"),
                item.get("deal_id"),
                item.get("hours_left") or 0,
            )
    else:
        for item in reminders:
            uid = _coerce_int(item.get("assigned_by_id"))
            deal_id = _coerce_int(item.get("deal_id"))
            if uid <= 0 or deal_id <= 0:
                continue
            hours_left = item.get("hours_left") or 0
            link = _build_crm_link("deal", deal_id)
            msg = (
                "Напоминание: сделка на этапе «Назначение встречи» "
                f"без комментария брокера/РОПа и без непросроченного дела. "
                f"Осталось {hours_left:.0f} ч "
                "до перевода в воронку «Общая база».\n"
                f"Сделка #{deal_id} {item.get('title') or ''}\n{link}"
            )
            try:
                send_user_chat_message(uid, msg)
            except Exception:
                logger.exception(
                    "Failed to send meeting reminder to user %s deal %s",
                    uid,
                    deal_id,
                )

    return {"violations": all_violations}


async def general_base_analyst(state: AuditState, settings: Settings) -> AuditState:
    """Deals in Общая база: 2 days after transfer to plan an activity or comment."""
    deals = state.get("raw_general_base_deals", [])
    logger.info("General Base Analyst: checking %d deals", len(deals))

    if not deals:
        return {"violations": []}

    current_time = state.get("current_time", "")
    all_violations = check_general_base_violations(deals, current_time)
    sanitized = _sanitize_violations(all_violations)
    logger.info(
        "General Base Analyst: found %d violations (%d after policy filter)",
        len(all_violations),
        len(sanitized),
    )
    return {"violations": sanitized}


async def _generic_calls_controller(
    entities: list[dict[str, Any]],
    entity_type: str,
    logger_name: str,
) -> list[dict[str, Any]]:
    """Generic deterministic check for missed calls without callback."""
    logger.info("%s: analyzing %d entities for call patterns", logger_name, len(entities))

    if not entities:
        return []

    entities_with_calls = sum(1 for e in entities if e.get("calls"))
    logger.info(
        "%s: %d/%d entities have call data",
        logger_name,
        entities_with_calls,
        len(entities),
    )

    total_missed = 0
    entities_with_missed = 0
    for entity in entities:
        if any(c.get("status") == "missed" for c in entity.get("calls", [])):
            entities_with_missed += 1
            total_missed += sum(
                1 for c in entity.get("calls", []) if c.get("status") == "missed"
            )
    logger.info(
        "%s: %d/%d entities with missed calls (%d total missed)",
        logger_name,
        entities_with_missed,
        len(entities),
        total_missed,
    )

    all_violations = check_missed_callback_violations(entities, entity_type)

    logger.info("%s: found %d call violations total", logger_name, len(all_violations))
    return all_violations


async def buyer_calls_controller(state: AuditState, settings: Settings) -> AuditState:
    """Agent 6: check buyer deals for missed callbacks (deterministic)."""
    deals = state.get("raw_buyers_deals", [])
    violations = await _generic_calls_controller(deals, "deal", "Agent 6 (Buyer Calls)")
    return {"violations": violations}


async def missed_calls_controller(state: AuditState, settings: Settings) -> AuditState:
    """Agent 7: check leads for missed calls without callback (deterministic)."""
    leads = state.get("raw_leads", [])
    violations = await _generic_calls_controller(leads, "lead", "Agent 7 (Missed Calls)")
    return {"violations": violations}


async def _build_user_map(
    violations: list[dict[str, Any]],
    leads: list[dict[str, Any]],
    buyer_deals: list[dict[str, Any]],
    seller_deals: list[dict[str, Any]],
    general_base_deals: list[dict[str, Any]] | None = None,
) -> tuple[dict[int, str], dict[int, int], set[int]]:
    """Fetch user names and departments for all responsible_id in violations.

    Args:
        violations: All violations from analysts.
        leads: Raw leads data.
        buyer_deals: Raw buyer deals data.
        seller_deals: Raw seller deals data.
        general_base_deals: Raw Общая база deals.

    Returns:
        Tuple of user_id → display name, user_id → department_id, and set of inactive user_ids.
    """
    user_ids: set[int] = set()

    for v in violations:
        uid = _coerce_int(v.get("responsible_id", 0))
        if uid:
            user_ids.add(uid)

    for lead in leads:
        uid = _coerce_int(lead.get("assigned_by_id", 0))
        if uid:
            user_ids.add(uid)
    for deal in buyer_deals:
        uid = _coerce_int(deal.get("assigned_by_id", 0))
        if uid:
            user_ids.add(uid)
    for deal in seller_deals:
        uid = _coerce_int(deal.get("assigned_by_id", 0))
        if uid:
            user_ids.add(uid)
    for deal in general_base_deals or []:
        uid = _coerce_int(deal.get("assigned_by_id", 0))
        if uid:
            user_ids.add(uid)

    if not user_ids:
        return {}, {}, set()

    user_map: dict[int, str] = {}
    dept_id_map: dict[int, int] = {}
    inactive_users: set[int] = set()

    for uid in user_ids:
        try:
            raw = _bx_call_sync("user.get", {"ID": uid})
            user: dict[str, Any] | None = None
            if isinstance(raw, dict) and raw:
                user = raw
            elif isinstance(raw, list) and raw:
                user = raw[0] if isinstance(raw[0], dict) else None
            else:
                raw = _bx_call_sync("user.search", {"FILTER": {"ID": uid}})
                if isinstance(raw, list) and raw:
                    user = raw[0] if isinstance(raw[0], dict) else None

            if user:
                # Check active status
                active = user.get("ACTIVE")
                is_active = True
                if active is False or str(active).upper() == "N" or str(active).lower() == "false":
                    is_active = False

                if not is_active:
                    inactive_users.add(uid)

                first = str(user.get("NAME") or "")
                last = str(user.get("LAST_NAME") or "")
                name = f"{last} {first}".strip() or f"ID:{uid}"

                dept_raw = user.get("UF_DEPARTMENT")
                logger.debug(
                    "User %s: UF_DEPARTMENT=%s (type=%s)",
                    uid,
                    dept_raw,
                    type(dept_raw).__name__,
                )

                dept_name = ""
                if isinstance(dept_raw, list) and dept_raw:
                    for dept_id in dept_raw:
                        try:
                            dept_info = _bx_call_sync(
                                "department.get",
                                {"ID": dept_id},
                            )
                            dname = ""
                            if isinstance(dept_info, dict) and dept_info:
                                dname = str(dept_info.get("NAME") or "")
                            elif isinstance(dept_info, list) and dept_info:
                                first = dept_info[0]
                                if isinstance(first, dict):
                                    dname = str(first.get("NAME") or "")
                            logger.debug("Department %s → %s", dept_id, dname)
                            if dname:
                                dept_name = dname
                                dept_id_map[uid] = _coerce_int(dept_id)
                                break
                        except Exception:
                            pass
                    if not dept_name:
                        dept_name = f"Отдел#{dept_raw[0]}"
                        dept_id_map[uid] = _coerce_int(dept_raw[0])
                elif isinstance(dept_raw, (int, str)) and dept_raw:
                    try:
                        dept_info = _bx_call_sync(
                            "department.get",
                            {"ID": dept_raw},
                        )
                        dname = ""
                        if isinstance(dept_info, dict) and dept_info:
                            dname = str(dept_info.get("NAME") or "")
                        elif isinstance(dept_info, list) and dept_info:
                            first = dept_info[0]
                            if isinstance(first, dict):
                                dname = str(first.get("NAME") or "")
                        logger.debug("Department %s → %s", dept_raw, dname)
                        dept_name = dname or f"Отдел#{dept_raw}"
                        dept_id_map[uid] = _coerce_int(dept_raw)
                    except Exception:
                        dept_name = f"Отдел#{dept_raw}"
                        dept_id_map[uid] = _coerce_int(dept_raw)

                display = f"{name} ({dept_name})" if dept_name else name
                user_map[uid] = display
            else:
                user_map[uid] = f"ID:{uid}"
        except Exception as exc:
            logger.warning("Failed to fetch user %s: %s", uid, exc)
            user_map[uid] = f"ID:{uid}"

    logger.info(
        "User map: %d users, departments: %d, dept_id_map: %d, inactive: %d",
        len(user_map),
        sum(1 for v in user_map.values() if "(" in v),
        len(dept_id_map),
        len(inactive_users),
    )
    return user_map, dept_id_map, inactive_users


def _group_by_department(
    violations: list[dict[str, Any]],
    user_map: dict[int, str],
) -> dict[str, list[dict[str, Any]]]:
    """Group violations by department name.

    Args:
        violations: All violations.
        user_map: user_id → "Name (Department)".

    Returns:
        Dict department_name → list of violations.
    """
    groups: dict[str, list[dict[str, Any]]] = {}

    for v in violations:
        uid = _coerce_int(v.get("responsible_id", 0))
        user_display = user_map.get(uid, f"ID:{uid}")

        if "(" in user_display and ")" in user_display:
            dept = user_display.split("(")[-1].rstrip(")")
        else:
            dept = "Без отдела"

        groups.setdefault(dept, []).append(v)

    return groups


async def report_dispatcher(state: AuditState, settings: Settings) -> AuditState:
    """Send full violation reports to Bitrix24 chat, one message per department."""
    violations = state.get("violations", [])
    seen: set[tuple[Any, ...]] = set()
    unique_violations: list[dict[str, Any]] = []
    for v in violations:
        key = (
            v.get("entity_type"),
            _coerce_int(v.get("entity_id", 0)),
            v.get("rule"),
        )
        if key not in seen:
            seen.add(key)
            unique_violations.append(v)
    violations = _filter_zero_entity_violations(unique_violations)
    violations = _sanitize_violations(violations)

    last_count = state.get("last_violation_count", -1)
    current_count = len(violations)
    if last_count == current_count and state.get("report_sent"):
        logger.info(
            "Dispatcher: violations unchanged (%d), skipping",
            current_count,
        )
        return {"status": "skipped_unchanged"}
    logger.info(
        "Dispatcher: formatting %d violations by department (DRY_RUN=%s)",
        len(violations),
        settings.dry_run,
    )
    now = state.get("current_time", "")[:19]

    user_map, dept_id_map, inactive_users = await _build_user_map(
        violations,
        state.get("raw_leads", []),
        state.get("raw_buyers_deals", []),
        state.get("raw_sellers_deals", []),
        state.get("raw_general_base_deals", []),
    )

    # Filter out violations from inactive users and excluded departments
    active_violations = []
    for v in violations:
        uid = _coerce_int(v.get("responsible_id", 0))
        user_display = user_map.get(uid, "")

        dept = ""
        if "(" in user_display and ")" in user_display:
            dept = user_display.split("(")[-1].rstrip(")")

        if uid in inactive_users:
            logger.debug("Skipping violation for inactive user %d", uid)
            continue

        if dept in {"Бэк-офис", "Битрикс"}:
            logger.debug("Skipping violation for user %d from '%s'", uid, dept)
            continue

        active_violations.append(v)

    violations = active_violations

    # Мутации CRM выполняются здесь, на уже отфильтрованном наборе: то, что
    # переносится в «Общую базу», обязано совпадать с тем, что уходит в отчёт.
    process_deals_to_general_base(violations, "buyers")
    process_deals_to_general_base(violations, "sellers")

    dept_groups = _group_by_department(violations, user_map)
    seller_deals_count = len(state.get("raw_sellers_deals", []))
    buyers_deals = state.get("raw_buyers_deals", [])
    sellers_deals = state.get("raw_sellers_deals", [])
    gb_deals = state.get("raw_general_base_deals", [])
    raw_leads = state.get("raw_leads", [])
    new_leads_count = sum(
        1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_NEW
    )
    shared_leads_count = sum(
        1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_SHARED
    )
    qualified_count = sum(
        1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_CONVERTED
    )
    spam_count = sum(
        1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_JUNK
    )
    necelevoy_count = sum(
        1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_NECELEVOY
    )
    agent_count = sum(
        1 for lead in raw_leads if _lead_status_id(lead) == LEAD_STATUS_AGENT
    )
    # Качество прогона: карточки, по которым не удалось прочитать доказательства,
    # исключены из аудита. Без этой строки деградация Bitrix читается в отчёте
    # как улучшение дисциплины.
    skipped_quality = {
        "leads": count_incomplete(raw_leads),
        "buyers": count_incomplete(buyers_deals),
        "sellers": count_incomplete(sellers_deals),
        "general_base": count_incomplete(gb_deals),
    }
    skipped_total = sum(skipped_quality.values())
    if skipped_total:
        logger.warning(
            "Dispatcher: %d cards excluded from this audit (unreadable evidence): %s",
            skipped_total,
            skipped_quality,
        )

    stage_names = build_stage_name_index(
        buyers_deals=buyers_deals,
        sellers_deals=sellers_deals,
        general_base_deals=gb_deals,
        leads=raw_leads,
    )
    audited_buyers = sum(
        1 for d in buyers_deals if d.get("audit_rule") is not None
    )
    seller_violations = [v for v in violations if _is_seller_violation(v)]
    gb_violations = [v for v in violations if _is_general_base_violation(v)]
    seller_rule_counts = {
        "seller_stage_stale": 0,
        "seller_deferred_no_activity": 0,
        "seller_negotiations_max": 0,
        "seller_lost_no_reason": 0,
        "seller_afina_id_missing": 0,
    }
    for v in seller_violations:
        rule = str(v.get("rule") or "")
        if rule in seller_rule_counts:
            seller_rule_counts[rule] += 1
    total_chunks = 0

    try:
        if settings.dry_run:
            logger.info(
                "Dispatcher: DRY_RUN — skipping chat send (%d violations, %d depts)",
                len(violations),
                len(dept_groups),
            )
        else:
            summary = (
                f"b24-ai-auditor v2 — сводка\n"
                f"Дата: {now}\n\n"
                f"Всего нарушений: {len(violations)}\n"
                f"Отделов: {len(dept_groups)}\n"
                f"Лидов в CRM: {len(raw_leads)}\n"
                f"  NEW (rule_1): {new_leads_count}\n"
                f"  Квалифицирован: {qualified_count}\n"
                f"  Спам: {spam_count}\n"
                f"  Нецелевой: {necelevoy_count}\n"
                f"  Агент: {agent_count}\n"
                f"  Общие Лиды (без аудита): {shared_leads_count}\n"
                f"Сделок покупателей: {len(buyers_deals)} "
                f"(на аудите: {audited_buyers})\n"
                f"Сделок продавцов: {seller_deals_count}\n"
                f"Сделок «Общая база»: {len(gb_deals)}\n"
                f"  general_base_no_plan: {len(gb_violations)}\n"
            )
            if skipped_total:
                summary += (
                    f"\n⚠️ Не проверено из-за сбоев Bitrix: {skipped_total} "
                    f"(лиды {skipped_quality['leads']}, "
                    f"покупатели {skipped_quality['buyers']}, "
                    f"продавцы {skipped_quality['sellers']}, "
                    f"общая база {skipped_quality['general_base']})\n"
                    "Эти карточки в аудит не попали — нарушений по ним нет "
                    "не потому, что их нет.\n"
                )
            total_chunks += send_chat_message_chunked(settings.report_chat_id, summary)

            # Сводка по правилам продавцов — только админу (личный чат).
            seller_admin_summary = (
                f"b24-ai-auditor v2 — воронка «Продавцы»\n"
                f"Дата: {now}\n"
                f"Открытых сделок продавцов: {seller_deals_count}\n"
                f"Нарушений: {len(seller_violations)}\n"
                f"seller_stage_stale: "
                f"{seller_rule_counts['seller_stage_stale']}\n"
                f"seller_deferred_no_activity: "
                f"{seller_rule_counts['seller_deferred_no_activity']}\n"
                f"seller_negotiations_max: "
                f"{seller_rule_counts['seller_negotiations_max']}\n"
                f"seller_lost_no_reason: "
                f"{seller_rule_counts['seller_lost_no_reason']}\n"
                f"seller_afina_id_missing: "
                f"{seller_rule_counts['seller_afina_id_missing']}"
            )
            try:
                send_user_chat_message(
                    settings.contact_source_lock_notify_user,
                    seller_admin_summary,
                )
            except Exception:
                logger.exception(
                    "Dispatcher: failed to send seller summary to user %s",
                    settings.contact_source_lock_notify_user,
                )

            if gb_violations:
                gb_lines = [
                    "b24-ai-auditor v2 — воронка «Общая база»",
                    f"Дата: {now}",
                    f"Открытых сделок: {len(gb_deals)}",
                    f"Без плана > 2 дн.: {len(gb_violations)}",
                    "",
                ]
                for v in gb_violations:
                    entity_id = _coerce_int(v.get("entity_id", 0))
                    reason = humanize_violation_reason(v, name_index=stage_names)
                    link = _build_crm_link("deal", entity_id)
                    uid = _coerce_int(v.get("responsible_id", 0))
                    user_display = user_map.get(uid, f"ID:{uid}")
                    name_only = (
                        user_display.split(" (")[0]
                        if " (" in user_display
                        else user_display
                    )
                    days_info = ""
                    details = v.get("details", {})
                    if isinstance(details, dict):
                        days = details.get("days_on_stage")
                        if _is_reportable_days(days):
                            days_info = f" ({days} дн.)"
                    gb_lines.append(
                        f"🟡 Сделка #{entity_id} | {name_only} | "
                        f"{reason}{days_info}",
                    )
                    gb_lines.append(f"   → {GENERAL_BASE_RULE_ACTION}")
                    gb_lines.append(f"   {link}")
                gb_report = "\n".join(gb_lines)
                try:
                    total_chunks += send_chat_message_chunked(
                        settings.report_chat_id, gb_report,
                    )
                except Exception:
                    logger.exception(
                        "Dispatcher: failed to send general-base list to chat %s",
                        settings.report_chat_id,
                    )
                try:
                    send_user_chat_message(
                        settings.contact_source_lock_notify_user,
                        gb_report,
                    )
                except Exception:
                    logger.exception(
                        "Dispatcher: failed to send general-base list to user %s",
                        settings.contact_source_lock_notify_user,
                    )

            for dept_name in sorted(dept_groups):
                dept_violations = _filter_zero_entity_violations(dept_groups[dept_name])
                if not dept_violations:
                    logger.info(
                        "Dispatcher: skipping dept '%s' (no valid violations)",
                        dept_name,
                    )
                    continue

                lead_v = [v for v in dept_violations if v.get("entity_type") == "lead"]
                gb_deal_v = [
                    v for v in dept_violations if _is_general_base_violation(v)
                ]
                buyer_deal_v = [
                    v for v in dept_violations
                    if v.get("entity_type") == "deal"
                    and not _is_seller_violation(v)
                    and not _is_general_base_violation(v)
                ]
                seller_deal_v = [
                    v for v in dept_violations
                    if v.get("entity_type") == "deal" and _is_seller_violation(v)
                ]

                dept_id = None
                for v in dept_violations:
                    uid = _coerce_int(v.get("responsible_id", 0))
                    did = dept_id_map.get(uid)
                    if did and did in settings.dept_chat_map:
                        dept_id = did
                        break

                if dept_id is None:
                    logger.info(
                        "Dispatcher: skipping dept '%s' (no ROP chat mapping)",
                        dept_name,
                    )
                    continue

                rop_chat_id = settings.dept_chat_map[dept_id]

                dept_user_ids = set()
                for uid, display in user_map.items():
                    if f"({dept_name})" in display:
                        dept_user_ids.add(uid)

                violator_ids = {
                    _coerce_int(v.get("responsible_id", 0)) for v in dept_violations
                }
                clean_in_dept = dept_user_ids - violator_ids - inactive_users

                lines = [
                    f"b24-ai-auditor v2 — Отдел: {dept_name}",
                    f"Дата: {now}",
                    (
                        "Сотрудников с нарушениями: "
                        f"{len({v.get('responsible_id', 0) for v in dept_violations})}"
                    ),
                    f"Всего нарушений: {len(dept_violations)}",
                    f"  Лиды: {len(lead_v)}",
                    f"  Сделки покупателей: {len(buyer_deal_v)}",
                    f"  Сделки продавцов: {len(seller_deal_v)}",
                    f"  Сделки «Общая база»: {len(gb_deal_v)}",
                    f"  ✅ Без нарушений: {len(clean_in_dept)}",
                    "",
                ]

                def _append_violation_lines(
                    items: list[dict[str, Any]],
                    *,
                    seller: bool,
                ) -> None:
                    for v in items:
                        sev = str(v.get("severity", "?"))
                        icon = _severity_icon(sev, seller=seller)
                        entity_type = str(v.get("entity_type", "?"))
                        entity_id = _coerce_int(v.get("entity_id", 0))
                        reason = humanize_violation_reason(
                            v, name_index=stage_names,
                        )
                        link = _build_crm_link(entity_type, entity_id)
                        uid = _coerce_int(v.get("responsible_id", 0))
                        user_display = user_map.get(uid, f"ID:{uid}")
                        name_only = (
                            user_display.split(" (")[0]
                            if " (" in user_display
                            else user_display
                        )
                        etype_label = "Лид" if entity_type == "lead" else "Сделка"
                        days_info = ""
                        details = v.get("details", {})
                        if isinstance(details, dict):
                            days = details.get("days_on_stage") or details.get(
                                "days_since_last_comment",
                            )
                            if _is_reportable_days(days):
                                days_info = f" ({days} дн.)"

                        lines.append(
                            f"{icon} {etype_label} #{entity_id} | {name_only} | "
                            f"{reason}{days_info}",
                        )
                        if seller:
                            action = seller_violation_action(v)
                            if action:
                                lines.append(f"   → {action}")
                        elif _is_general_base_violation(v):
                            lines.append(f"   → {GENERAL_BASE_RULE_ACTION}")
                        lines.append(f"   {link}")

                lead_and_buyer = [
                    v for v in dept_violations
                    if v.get("entity_type") == "lead" or (
                        v.get("entity_type") == "deal"
                        and not _is_seller_violation(v)
                        and not _is_general_base_violation(v)
                    )
                ]
                _append_violation_lines(lead_and_buyer, seller=False)

                if seller_deal_v:
                    if lead_and_buyer:
                        lines.append("")
                    lines.append("— Сделки продавцов —")
                    _append_violation_lines(seller_deal_v, seller=True)

                if gb_deal_v:
                    if lead_and_buyer or seller_deal_v:
                        lines.append("")
                    lines.append("— Общая база —")
                    _append_violation_lines(gb_deal_v, seller=False)

                call_violations = [
                    v for v in dept_violations
                    if "missed_callback" in str(v.get("rule", ""))
                ]
                if call_violations:
                    lines.append("")
                    lines.append("📞 ЗВОНКИ:")
                    for v in call_violations:
                        uid = _coerce_int(v.get("responsible_id", 0))
                        name_only = user_map.get(uid, f"ID:{uid}").split(" (")[0]
                        entity_type = str(v.get("entity_type", "?"))
                        entity_id = _coerce_int(v.get("entity_id", 0))
                        link = _build_crm_link(entity_type, entity_id)
                        lines.append(f"   • {name_only} — {link}")

                report = "\n".join(lines)
                try:
                    chunks = send_chat_message_chunked(rop_chat_id, report)
                    total_chunks += chunks
                    logger.info(
                        "Dispatcher: dept '%s' report sent to ROP chat %d "
                        "(%d violations, %d chunks)",
                        dept_name,
                        rop_chat_id,
                        len(dept_violations),
                        chunks,
                    )
                except Exception:
                    logger.exception(
                        "Dispatcher: failed to send dept '%s' report to chat %d "
                        "— continuing with other departments",
                        dept_name,
                        rop_chat_id,
                    )

            logger.info(
                "Dispatcher: reports sent for %d departments",
                len(dept_groups),
            )

    except Exception:
        logger.exception(
            "Dispatcher: failed while sending reports (chat %d)",
            settings.report_chat_id,
        )

    if settings.dry_run:
        # DRY_RUN — режим чтения. Записанные нарушения питают рейтинг брокеров
        # (broker_rating.get_violations_for_broker), поэтому тестовый прогон
        # не должен оставлять следов в БД.
        logger.info(
            "Dispatcher: DRY_RUN — %d violations not persisted", current_count,
        )
        return {
            "messages": [
                f"dispatcher: dry-run, {len(dept_groups)} dept reports prepared "
                f"({len(violations)} violations)",
            ],
            "status": "completed_dry_run",
            "skipped_incomplete": skipped_total,
            # Проход диспетчера состоялся: флаг гасит повторный вызов
            # (см. защиту «violations unchanged» выше).
            "report_sent": True,
            "last_violation_count": current_count,
        }

    from db import (
        init_db,
        is_routine_audit_run,
        save_audit_run,
        save_violations,
        sync_violation_states,
        upsert_brokers,
    )

    init_db()
    is_routine = bool(settings.force_routine_audit) or is_routine_audit_run(
        settings.report_since, len(raw_leads), now
    )
    if settings.force_routine_audit and not is_routine_audit_run(
        settings.report_since, len(raw_leads), now
    ):
        logger.info("Dispatcher: FORCE_ROUTINE_AUDIT=true — saving violations")
    run_id = save_audit_run(
        now,
        len(raw_leads),
        len(buyers_deals),
        seller_deals_count,
        current_count,
        report_since=settings.report_since,
        is_routine=is_routine,
    )
    if is_routine:
        save_violations(run_id, violations, user_map, dept_id_map, now)
        # Область закрывается только если её коллектор реально вернул данные:
        # пустой сбор из-за сбоя API иначе отрапортует «всё исправлено».
        scopes_with_data = {
            scope
            for scope, records in (
                ("leads", raw_leads),
                ("buyers", buyers_deals),
                ("sellers", sellers_deals),
                ("general_base", gb_deals),
            )
            if records
        }
        lifecycle = sync_violation_states(
            violations,
            now,
            scopes_with_data=scopes_with_data,
            user_map=user_map,
            dept_id_map=dept_id_map,
        )
        logger.info("Dispatcher: violation lifecycle %s", lifecycle)
    else:
        logger.info(
            "Dispatcher: non-routine audit run %d — violations not saved to DB",
            run_id,
        )
    upsert_brokers(
        user_map,
        dept_id_map,
        raw_leads,
        buyers_deals,
        state.get("raw_sellers_deals", []),
        now,
    )
    logger.info("Dispatcher: saved audit run %d to database", run_id)

    return {
        "messages": [
            f"dispatcher: {len(dept_groups)} dept reports sent "
            f"({len(violations)} violations, {total_chunks} chunks)",
        ],
        "status": "completed",
        "skipped_incomplete": skipped_total,
        "report_sent": True,
        "last_violation_count": current_count,
    }


async def merge_node(state: AuditState) -> dict[str, Any]:
    """No-op merge: waits for all branches before dispatcher."""
    # Return empty update so fan-in does not collide on last_value channels.
    return {}


async def passthrough_node(state: AuditState) -> dict[str, Any]:
    """No-op step that keeps every branch the same length before `merge`.

    LangGraph schedules a node whenever any incoming edge is written. The
    seller and general-base branches were one hop shorter than the lead and
    buyer branches, so `merge` fired twice and the dispatcher ran twice —
    duplicate department reports whenever the violation count differed
    between the two passes.
    """
    return {}


def build_graph_v2(settings: Settings):
    """Build v2 audit graph: 4 collectors → analysts → merge → dispatcher."""
    graph = StateGraph(AuditState)

    graph.add_node("lead_collector", partial(lead_collector, settings=settings))
    graph.add_node("buyer_collector", partial(buyer_collector, settings=settings))
    graph.add_node("seller_collector", partial(seller_collector, settings=settings))
    graph.add_node("general_base_collector", partial(general_base_collector, settings=settings))
    graph.add_node("lead_analyst", partial(lead_analyst, settings=settings))
    graph.add_node("buyer_deal_analyst", partial(buyer_deal_analyst, settings=settings))
    graph.add_node("seller_deal_analyst", partial(seller_deal_analyst, settings=settings))
    graph.add_node("general_base_analyst", partial(general_base_analyst, settings=settings))
    graph.add_node("buyer_calls_controller", partial(buyer_calls_controller, settings=settings))
    graph.add_node("missed_calls_controller", partial(missed_calls_controller, settings=settings))
    graph.add_node("report_dispatcher", partial(report_dispatcher, settings=settings))
    graph.add_node("merge", merge_node)
    graph.add_node("seller_gate", passthrough_node)
    graph.add_node("general_base_gate", passthrough_node)

    graph.add_edge(START, "lead_collector")
    graph.add_edge(START, "buyer_collector")
    graph.add_edge(START, "seller_collector")
    graph.add_edge(START, "general_base_collector")

    graph.add_edge("lead_collector", "lead_analyst")
    graph.add_edge("lead_analyst", "missed_calls_controller")

    graph.add_edge("buyer_collector", "buyer_deal_analyst")
    graph.add_edge("buyer_deal_analyst", "buyer_calls_controller")

    graph.add_edge("seller_collector", "seller_deal_analyst")
    graph.add_edge("general_base_collector", "general_base_analyst")

    # Все четыре ветки приходят в merge на одной глубине.
    graph.add_edge("missed_calls_controller", "merge")
    graph.add_edge("buyer_calls_controller", "merge")
    graph.add_edge("seller_deal_analyst", "seller_gate")
    graph.add_edge("seller_gate", "merge")
    graph.add_edge("general_base_analyst", "general_base_gate")
    graph.add_edge("general_base_gate", "merge")

    graph.add_edge("merge", "report_dispatcher")
    graph.add_edge("report_dispatcher", END)

    return graph.compile()


async def run_audit_v2(settings: Settings) -> AuditState:
    """Run v2 audit: collectors → analysts → merge → report dispatcher.

    Args:
        settings: Application settings.

    Returns:
        Final AuditState with violations and status.
    """
    from datetime import datetime, timezone

    app = build_graph_v2(settings)
    initial: AuditState = {
        "raw_leads": [],
        "raw_buyers_deals": [],
        "raw_sellers_deals": [],
        "raw_general_base_deals": [],
        "violations": [],
        "current_time": datetime.now(timezone.utc).isoformat(),
        "dry_run": settings.dry_run,
        "messages": [],
        "report_sent": False,
        "last_violation_count": -1,
    }
    return await app.ainvoke(initial)
