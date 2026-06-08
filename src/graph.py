"""LangGraph workflow: v2 collectors -> analysts -> report dispatcher."""

import json
import logging
import operator
import re
from typing import Annotated, Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from config import Settings
from notify import _bx_call_sync, send_chat_message_chunked
from prompts import (
    BUYER_CALLS_PROMPT,
    LEAD_ANALYST_PROMPT,
    MISSED_CALLS_PROMPT,
)
from tools import (
    _build_crm_link,
    _coerce_int,
    check_buyer_deal_violations,
    get_all_leads_with_timeline,
    get_deals_by_funnel_with_timeline,
    humanize_violation_reason,
)

logger = logging.getLogger(__name__)

ANALYST_CHUNK_SIZE = 100
REPORT_CHAT_ID = 22358

DEPT_CHAT_MAP: dict[int, int] = {
    60: 17710,  # Кретов
    46: 17712,  # Горяинов
    42: 17716,  # Каратевский
    44: 17708,  # Трофимова
    50: 17714,  # Волкова
}


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


def _make_r1_llm(settings: Settings) -> ChatOpenAI:
    """Create DeepSeek-R1 LLM via RouterAI for reasoning (Analyst).

    Args:
        settings: Application settings.

    Returns:
        ChatOpenAI client configured for RouterAI.
    """
    return ChatOpenAI(
        api_key=settings.routerai_api_key,
        base_url=settings.routerai_base_url,
        model=settings.routerai_r1_model,
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
    """Agent 4: analyze leads for stage violations (chunked by 100)."""
    leads = state.get("raw_leads", [])
    logger.info(
        "Agent 4 (Lead Analyst): analyzing %d leads in chunks of %d",
        len(leads),
        ANALYST_CHUNK_SIZE,
    )

    if not leads:
        return {"violations": []}

    llm = _make_r1_llm(settings)
    current_time = state.get("current_time", "")
    all_violations: list[dict[str, Any]] = []

    for i in range(0, len(leads), ANALYST_CHUNK_SIZE):
        chunk = leads[i:i + ANALYST_CHUNK_SIZE]
        payload = {"leads": chunk, "current_time": current_time}

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
                min(i + ANALYST_CHUNK_SIZE, len(leads)),
                len(new_violations),
            )
        except Exception as exc:
            logger.warning(
                "Agent 4 chunk %d-%d failed: %s",
                i,
                min(i + ANALYST_CHUNK_SIZE, len(leads)),
                exc,
            )

    logger.info("Agent 4: found %d lead violations total", len(all_violations))
    return {"violations": all_violations}


async def buyer_deal_analyst(state: AuditState, settings: Settings) -> AuditState:
    """Agent 5: analyze buyer deals for stage violations (deterministic rules 1–5)."""
    deals = state.get("raw_buyers_deals", [])
    logger.info("Agent 5 (Buyer Deal Analyst): checking %d deals", len(deals))

    if not deals:
        return {"violations": []}

    current_time = state.get("current_time", "")
    all_violations = check_buyer_deal_violations(deals, current_time)

    logger.info("Agent 5: found %d buyer deal violations total", len(all_violations))
    return {"violations": all_violations}


async def buyer_calls_controller(state: AuditState, settings: Settings) -> AuditState:
    """Agent 6: check buyer deals for missed callbacks (chunked by 100)."""
    deals = state.get("raw_buyers_deals", [])
    logger.info(
        "Agent 6 (Buyer Calls): analyzing %d deals for call patterns",
        len(deals),
    )

    if not deals:
        return {"violations": []}

    entities_with_calls = sum(1 for e in deals if e.get("calls"))
    logger.info(
        "Agent 6: %d/%d deals have call data",
        entities_with_calls,
        len(deals),
    )

    total_missed = 0
    entities_with_missed = 0
    for entity in deals:
        if any(c.get("status") == "missed" for c in entity.get("calls", [])):
            entities_with_missed += 1
            total_missed += sum(
                1 for c in entity.get("calls", []) if c.get("status") == "missed"
            )
    logger.info(
        "Agent 6: %d/%d entities with missed calls (%d total missed)",
        entities_with_missed,
        len(deals),
        total_missed,
    )

    llm = _make_r1_llm(settings)
    current_time = state.get("current_time", "")
    all_violations: list[dict[str, Any]] = []
    call_fields = ("deal_id", "title", "assigned_by_id", "calls")

    for i in range(0, len(deals), ANALYST_CHUNK_SIZE):
        chunk = [
            {k: d[k] for k in call_fields if k in d}
            for d in deals[i:i + ANALYST_CHUNK_SIZE]
        ]
        payload = {"deals": chunk, "current_time": current_time}

        try:
            response = await llm.ainvoke([
                SystemMessage(content=BUYER_CALLS_PROMPT),
                HumanMessage(
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                ),
            ])
            content = _message_content_to_str(response.content)
            new_violations = _parse_violations_json(content)
            all_violations.extend(new_violations)
        except Exception as exc:
            logger.warning(
                "Agent 6 chunk %d-%d failed: %s",
                i,
                min(i + ANALYST_CHUNK_SIZE, len(deals)),
                exc,
            )

    logger.info("Agent 6: found %d call violations total", len(all_violations))
    return {"violations": all_violations}


async def missed_calls_controller(state: AuditState, settings: Settings) -> AuditState:
    """Agent 7: check leads for missed calls without callback (chunked by 100)."""
    leads = state.get("raw_leads", [])
    logger.info("Agent 7 (Missed Calls): analyzing %d leads", len(leads))

    if not leads:
        return {"violations": []}

    entities_with_calls = sum(1 for e in leads if e.get("calls"))
    logger.info(
        "Agent 7: %d/%d leads have call data",
        entities_with_calls,
        len(leads),
    )

    total_missed = 0
    entities_with_missed = 0
    for entity in leads:
        if any(c.get("status") == "missed" for c in entity.get("calls", [])):
            entities_with_missed += 1
            total_missed += sum(
                1 for c in entity.get("calls", []) if c.get("status") == "missed"
            )
    logger.info(
        "Agent 7: %d/%d entities with missed calls (%d total missed)",
        entities_with_missed,
        len(leads),
        total_missed,
    )

    llm = _make_r1_llm(settings)
    current_time = state.get("current_time", "")
    all_violations: list[dict[str, Any]] = []
    call_fields = ("lead_id", "title", "assigned_by_id", "calls")

    for i in range(0, len(leads), ANALYST_CHUNK_SIZE):
        chunk = [
            {k: lead[k] for k in call_fields if k in lead}
            for lead in leads[i:i + ANALYST_CHUNK_SIZE]
        ]
        payload = {"leads": chunk, "current_time": current_time}

        try:
            response = await llm.ainvoke([
                SystemMessage(content=MISSED_CALLS_PROMPT),
                HumanMessage(
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                ),
            ])
            content = _message_content_to_str(response.content)
            new_violations = _parse_violations_json(content)
            all_violations.extend(new_violations)
        except Exception as exc:
            logger.warning(
                "Agent 7 chunk %d-%d failed: %s",
                i,
                min(i + ANALYST_CHUNK_SIZE, len(leads)),
                exc,
            )

    logger.info("Agent 7: found %d missed call violations total", len(all_violations))

    return {"violations": all_violations}


async def _build_user_map(
    violations: list[dict[str, Any]],
    leads: list[dict[str, Any]],
    buyer_deals: list[dict[str, Any]],
    seller_deals: list[dict[str, Any]],
) -> tuple[dict[int, str], dict[int, int]]:
    """Fetch user names and departments for all responsible_id in violations.

    Args:
        violations: All violations from analysts.
        leads: Raw leads data.
        buyer_deals: Raw buyer deals data.
        seller_deals: Raw seller deals data.

    Returns:
        Tuple of user_id → display name, and user_id → department_id.
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
        return {}, {}

    user_map: dict[int, str] = {}
    dept_id_map: dict[int, int] = {}

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
        "User map: %d users, departments: %d, dept_id_map: %d",
        len(user_map),
        sum(1 for v in user_map.values() if "(" in v),
        len(dept_id_map),
    )
    return user_map, dept_id_map


def _format_report(
    title: str,
    violations: list[dict[str, Any]],
    now: str,
    user_map: dict[int, str],
    extra: str = "",
    *,
    buyers_deals: list[dict[str, Any]] | None = None,
    sellers_deals: list[dict[str, Any]] | None = None,
    leads: list[dict[str, Any]] | None = None,
) -> str:
    """Format a single-domain violations report.

    Args:
        title: Report section title.
        violations: List of violation dicts.
        now: Current timestamp string.
        extra: Optional extra info line.
        user_map: user_id → display name with department.

    Returns:
        Formatted report string.
    """
    lines = [
        f"b24-ai-auditor v2 — {title}",
        f"Дата: {now}",
        "",
    ]

    if extra:
        lines.append(extra)

    if not violations:
        lines.append("Нарушений не найдено.")
        return "\n".join(lines)

    very_high = [v for v in violations if v.get("severity") == "very high"]
    high = [v for v in violations if v.get("severity") == "high"]
    medium = [v for v in violations if v.get("severity") == "medium"]

    lines.append(f"Нарушений: {len(violations)}")
    if very_high:
        lines.append(f"  🔴🔴 Критические: {len(very_high)}")
    if high:
        lines.append(f"  🔴 Высокие: {len(high)}")
    if medium:
        lines.append(f"  🟡 Средние: {len(medium)}")
    lines.append("")

    severity_order = {"very high": 0, "high": 1, "medium": 2}
    sorted_v = sorted(
        violations,
        key=lambda v: severity_order.get(v.get("severity", "medium"), 99),
    )

    for i, v in enumerate(sorted_v, 1):
        sev = v.get("severity", "?")
        icon = {"very high": "🔴🔴", "high": "🔴", "medium": "🟡"}.get(sev, "⚪")
        entity_type = str(v.get("entity_type", "?"))
        entity_id = _coerce_int(v.get("entity_id", 0))
        responsible = _coerce_int(v.get("responsible_id", 0))
        rule = v.get("rule", "?")
        reason = humanize_violation_reason(
            v,
            buyers_deals=buyers_deals,
            sellers_deals=sellers_deals,
            leads=leads,
        )

        link = _build_crm_link(entity_type, entity_id)
        prefix = entity_type[0].upper() if entity_type else "?"
        user_display = user_map.get(responsible, f"ID:{responsible}")

        lines.append(
            f"{i}. {icon} {prefix}#{entity_id} {user_display} [{rule}] {reason}",
        )
        lines.append(f"   {link}")
        lines.append("")

    return "\n".join(lines)


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

    user_map, dept_id_map = await _build_user_map(
        violations,
        state.get("raw_leads", []),
        state.get("raw_buyers_deals", []),
        state.get("raw_sellers_deals", []),
    )

    dept_groups = _group_by_department(violations, user_map)
    seller_deals_count = len(state.get("raw_sellers_deals", []))
    buyers_deals = state.get("raw_buyers_deals", [])
    sellers_deals = state.get("raw_sellers_deals", [])
    raw_leads = state.get("raw_leads", [])
    total_chunks = 0

    try:
        summary = (
            f"b24-ai-auditor v2 — сводка\n"
            f"Дата: {now}\n\n"
            f"Всего нарушений: {len(violations)}\n"
            f"Отделов: {len(dept_groups)}\n"
            f"Лидов в CRM: {len(state.get('raw_leads', []))}\n"
            f"Сделок покупателей: {len(state.get('raw_buyers_deals', []))}\n"
            f"Сделок продавцов: {seller_deals_count}\n"
        )
        total_chunks += send_chat_message_chunked(REPORT_CHAT_ID, summary)

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
                if did and did in DEPT_CHAT_MAP:
                    dept_id = did
                    break

            if dept_id is None:
                logger.info(
                    "Dispatcher: skipping dept '%s' (no ROP chat mapping)",
                    dept_name,
                )
                continue

            rop_chat_id = DEPT_CHAT_MAP[dept_id]

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
                    if days is not None and days != "":
                        days_info = f" ({days} дн.)"

                lines.append(
                    f"{icon} {etype_label} #{entity_id} | {name_only} | "
                    f"{reason}{days_info}",
                )
                lines.append(f"   {link}")

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

            # Блок "📞 ЗВОНКИ" временно отключён

        logger.info(
            "Dispatcher: reports sent for %d departments",
            len(dept_groups),
        )
    except Exception:
        logger.exception(
            "Dispatcher: failed to send reports to chat %d",
            REPORT_CHAT_ID,
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

    async def lead_col(state: AuditState) -> AuditState:
        return await lead_collector(state, settings)

    async def buyer_col(state: AuditState) -> AuditState:
        return await buyer_collector(state, settings)

    async def seller_col(state: AuditState) -> AuditState:
        return await seller_collector(state, settings)

    async def lead_an(state: AuditState) -> AuditState:
        return await lead_analyst(state, settings)

    async def buyer_deal_an(state: AuditState) -> AuditState:
        return await buyer_deal_analyst(state, settings)

    async def buyer_calls(state: AuditState) -> AuditState:
        return await buyer_calls_controller(state, settings)

    async def missed_calls(state: AuditState) -> AuditState:
        return await missed_calls_controller(state, settings)

    async def dispatcher(state: AuditState) -> AuditState:
        return await report_dispatcher(state, settings)

    graph.add_node("lead_collector", lead_col)
    graph.add_node("buyer_collector", buyer_col)
    graph.add_node("seller_collector", seller_col)
    graph.add_node("lead_analyst", lead_an)
    graph.add_node("buyer_deal_analyst", buyer_deal_an)
    graph.add_node("buyer_calls_controller", buyer_calls)
    graph.add_node("missed_calls_controller", missed_calls)
    graph.add_node("report_dispatcher", dispatcher)
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
