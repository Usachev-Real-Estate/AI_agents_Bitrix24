"""LangGraph workflow: v2 collectors -> analysts -> report dispatcher."""

import json
import logging
import operator
import re
from datetime import datetime, timezone
from functools import partial
from typing import Annotated, Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from config import Settings
from notify import _bx_call_sync, send_chat_message_chunked
from prompts import (
    LEAD_ANALYST_PROMPT,
)
from tools import (
    _build_crm_link,
    _build_rop_map,
    _coerce_int,
    _lead_needs_llm_check,
    _lead_status_id,
    check_buyer_deal_violations,
    check_lead_rule1_violations,
    check_missed_callback_violations,
    get_all_leads_with_timeline,
    get_deals_by_funnel_with_timeline,
    humanize_violation_reason,
    LEAD_STATUS_NEW,
    LEAD_STATUS_SHARED,
    LEAD_STATUS_CONVERTED,
    LEAD_STATUS_JUNK,
    LEAD_STATUS_NECELEVOY,
    LEAD_STATUS_AGENT,
)

logger = logging.getLogger(__name__)

ALLOWED_VIOLATION_RULES = {
    "lead_rule_1",
    "lead_rule_2",
    "lead_rule_3",
    "lead_missed_callback",
    "buyer_stage_1",
    "buyer_stage_2",
    "buyer_stage_3",
    "buyer_stage_4",
    "buyer_stage_5",
    "buyer_missed_callback",
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
    violations: Annotated[list[dict[str, Any]], operator.add]
    current_time: str
    dry_run: bool
    status: str
    messages: Annotated[list[str], operator.add]
    report_sent: bool
    last_violation_count: int


def _make_llm(settings: Settings) -> ChatOpenAI:
    """Create DeepSeek LLM for analyst nodes.

    Args:
        settings: Application settings.

    Returns:
        ChatOpenAI client configured for DeepSeek API.
    """
    return ChatOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        temperature=0.1,
    )


def _message_content_to_str(content: Any) -> str:
    """Convert LLM message content to a plain string.

    Args:
        content: Message content (str or multimodal blocks).

    Returns:
        Concatenated text content.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def _parse_violations_json(content: str) -> list[dict[str, Any]]:
    """Extract violations list from Analyst LLM response.

    Args:
        content: Raw model response text.

    Returns:
        Parsed violations or empty list on failure.
    """
    if "{" not in content:
        return []
    json_str = content
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence:
        json_str = fence.group(1)
    else:
        json_start = content.index("{")
        json_str = content[json_start:]
    parsed = json.loads(json_str)
    violations = parsed.get("violations", [])
    if isinstance(violations, list):
        return [v for v in violations if isinstance(v, dict)]
    return []


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


async def lead_analyst(state: AuditState, settings: Settings) -> AuditState:
    """Agent 4: analyze leads for stage violations."""
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

    # Детерминированная проверка rule_1
    rule1_violations = check_lead_rule1_violations(leads, current_time)

    # Фильтруем лиды, требующие LLM (rule_2: SPAM, rule_3: Нецелевой)
    llm_leads = [lead for lead in leads if _lead_needs_llm_check(lead)]

    logger.info(
        "Agent 4: %d leads require LLM checking",
        len(llm_leads),
    )

    if not llm_leads:
        return {"violations": rule1_violations}

    llm = _make_llm(settings)
    all_violations: list[dict[str, Any]] = list(rule1_violations)

    llm_fields = (
        "lead_id",
        "title",
        "status_id",
        "status_name",
        "assigned_by_id",
        "comments_field",
        "timeline",
    )

    for i in range(0, len(llm_leads), settings.analyst_chunk_size):
        chunk = [
            {k: lead[k] for k in llm_fields if k in lead}
            for lead in llm_leads[i:i + settings.analyst_chunk_size]
        ]
        payload = {"leads": chunk, "current_time": current_time_str}

        try:
            response = await llm.ainvoke([
                SystemMessage(content=LEAD_ANALYST_PROMPT),
                HumanMessage(
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                ),
            ])
            content = _message_content_to_str(response.content)
            new_violations = _parse_violations_json(content)
            all_violations.extend(new_violations)
            logger.debug(
                "Agent 4 chunk %d-%d: %d violations",
                i,
                min(i + settings.analyst_chunk_size, len(llm_leads)),
                len(new_violations),
            )
        except Exception as exc:
            logger.warning(
                "Agent 4 chunk %d-%d failed: %s",
                i,
                min(i + settings.analyst_chunk_size, len(llm_leads)),
                exc,
            )

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

    logger.info("Agent 5: found %d buyer deal violations total", len(all_violations))
    return {"violations": all_violations}


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
) -> tuple[dict[int, str], dict[int, int], set[int]]:
    """Fetch user names and departments for all responsible_id in violations.

    Args:
        violations: All violations from analysts.
        leads: Raw leads data.
        buyer_deals: Raw buyer deals data.
        seller_deals: Raw seller deals data.

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
        return state
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

    dept_groups = _group_by_department(violations, user_map)
    seller_deals_count = len(state.get("raw_sellers_deals", []))
    buyers_deals = state.get("raw_buyers_deals", [])
    sellers_deals = state.get("raw_sellers_deals", [])
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
    audited_buyers = sum(
        1 for d in buyers_deals if d.get("audit_rule") is not None
    )
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
            )
            total_chunks += send_chat_message_chunked(settings.report_chat_id, summary)

            for dept_name in sorted(dept_groups):
                dept_violations = _filter_zero_entity_violations(dept_groups[dept_name])
                if not dept_violations:
                    logger.info(
                        "Dispatcher: skipping dept '%s' (no valid violations)",
                        dept_name,
                    )
                    continue

                lead_v = [v for v in dept_violations if v.get("entity_type") == "lead"]
                deal_v = [v for v in dept_violations if v.get("entity_type") == "deal"]

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
                    f"  Сделки: {len(deal_v)}",
                    f"  ✅ Без нарушений: {len(clean_in_dept)}",
                    "",
                ]

                for v in dept_violations:
                    sev = v.get("severity", "?")
                    icon = {"very high": "🔴🔴", "high": "🔴", "medium": "🟡"}.get(
                        sev,
                        "⚪",
                    )
                    entity_type = str(v.get("entity_type", "?"))
                    entity_id = _coerce_int(v.get("entity_id", 0))
                    reason = humanize_violation_reason(
                        v,
                        buyers_deals=buyers_deals,
                        sellers_deals=sellers_deals,
                        leads=raw_leads,
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
                        if days is not None and days != "" and days < 999:
                            days_info = f" ({days} дн.)"

                    lines.append(
                        f"{icon} {etype_label} #{entity_id} | {name_only} | "
                        f"{reason}{days_info}",
                    )
                    lines.append(f"   {link}")

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
                chunks = send_chat_message_chunked(rop_chat_id, report)
                total_chunks += chunks

                logger.info(
                    "Dispatcher: dept '%s' report sent to ROP chat %d (%d violations, %d chunks)",
                    dept_name,
                    rop_chat_id,
                    len(dept_violations),
                    chunks,
                )

            logger.info(
                "Dispatcher: reports sent for %d departments",
                len(dept_groups),
            )

        from db import init_db, is_routine_audit_run, save_audit_run, save_violations, upsert_brokers

        init_db()
        is_routine = is_routine_audit_run(settings.report_since, len(raw_leads), now)
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
            now
        )
        logger.info("Dispatcher: saved audit run %d to database", run_id)

    except Exception:
        logger.exception(
            "Dispatcher: failed to send reports to chat %d",
            settings.report_chat_id,
        )

    return {
        "messages": [
            f"dispatcher: {len(dept_groups)} dept reports sent "
            f"({len(violations)} violations, {total_chunks} chunks)",
        ],
        "status": "completed",
        "report_sent": True,
        "last_violation_count": current_count,
    }


async def merge_node(state: AuditState) -> AuditState:
    """No-op merge: waits for all branches before dispatcher."""
    return state


def build_graph_v2(settings: Settings):
    """Build v2 audit graph: 3 collectors → 4 analysts → merge → dispatcher."""
    graph = StateGraph(AuditState)

    graph.add_node("lead_collector", partial(lead_collector, settings=settings))
    graph.add_node("buyer_collector", partial(buyer_collector, settings=settings))
    graph.add_node("seller_collector", partial(seller_collector, settings=settings))
    graph.add_node("lead_analyst", partial(lead_analyst, settings=settings))
    graph.add_node("buyer_deal_analyst", partial(buyer_deal_analyst, settings=settings))
    graph.add_node("buyer_calls_controller", partial(buyer_calls_controller, settings=settings))
    graph.add_node("missed_calls_controller", partial(missed_calls_controller, settings=settings))
    graph.add_node("report_dispatcher", partial(report_dispatcher, settings=settings))
    graph.add_node("merge", merge_node)

    graph.add_edge(START, "lead_collector")
    graph.add_edge(START, "buyer_collector")
    graph.add_edge(START, "seller_collector")

    graph.add_edge("lead_collector", "lead_analyst")
    graph.add_edge("lead_analyst", "missed_calls_controller")

    graph.add_edge("buyer_collector", "buyer_deal_analyst")
    graph.add_edge("buyer_deal_analyst", "buyer_calls_controller")

    graph.add_edge("missed_calls_controller", "merge")
    graph.add_edge("buyer_calls_controller", "merge")
    graph.add_edge("seller_collector", "merge")

    graph.add_edge("merge", "report_dispatcher")
    graph.add_edge("report_dispatcher", END)

    return graph.compile()


async def run_audit_v2(settings: Settings) -> AuditState:
    """Run v2 audit: 3 collectors → 4 analysts → merge → report dispatcher.

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
        "violations": [],
        "current_time": datetime.now(timezone.utc).isoformat(),
        "dry_run": settings.dry_run,
        "messages": [],
        "report_sent": False,
        "last_violation_count": -1,
    }
    return await app.ainvoke(initial)
