"""Quality audit of Spam / Non-target leads: leaked + wrong qualification."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from config import Settings, get_settings, setup_logging  # noqa: E402
from llm import make_llm  # noqa: E402
from db import (  # noqa: E402
    delete_lead_quality_finding,
    init_db,
    list_lead_quality_findings,
    mark_lead_quality_notified,
    save_lead_quality_finding,
    was_lead_quality_finding_recorded,
)
from notify import _bx_call_sync, send_user_chat_message_chunked  # noqa: E402
from prompts import LEAD_QUALITY_ANALYST_PROMPT  # noqa: E402
from tools import (  # noqa: E402
    LEAD_STATUS_JUNK,
    LEAD_STATUS_NECELEVOY,
    MAX_TIMELINE_WORKERS,
    _as_list,
    _build_crm_link,
    _bx_get_all_sync,
    _clean_str,
    _coerce_int,
    _fetch_entity_timeline,
    _fetch_lead_status_names,
)

logger = logging.getLogger(__name__)

QUALITY_RULES = frozenset({"lead_leaked", "lead_wrong_qualification"})
STATUS_FILTER = [LEAD_STATUS_JUNK, LEAD_STATUS_NECELEVOY]
TRIVIAL_JUSTIFICATION = frozenset(
    {"спам", "нецелевой", "не целевой", "нецелевои", "junk", "spam"},
)

# Служебные / фейковые звонки — не нарушения качества квалификации
_SELF_CALL_OR_RATING_PATTERNS = (
    re.compile(r"звонок\s+сам\s+себе", re.I),
    re.compile(r"сам\s+себе", re.I),
    re.compile(r"себе\s+на\s+(свой\s+)?(номер|телефон)", re.I),
    re.compile(r"личн(ый|ого)\s+звонок", re.I),
    re.compile(r"тестов(ый|ого)\s+звонок", re.I),
    re.compile(r"самозвон", re.I),
    re.compile(r"прозвон", re.I),
    re.compile(r"проверк[аеи]\s+(лини|связ|звон)", re.I),
    re.compile(r"повышен\w*\s+рейтинг", re.I),
    re.compile(r"поднят\w*\s+рейтинг", re.I),
    re.compile(r"для\s+рейтинг", re.I),
    re.compile(r"накрутк\w*\s+звон", re.I),
    re.compile(r"рейтинг\s+црм", re.I),
    re.compile(r"ради\s+рейтинг", re.I),
)

# Агент/риелтор «пробивал» номер — не нарушение качества
_AGENT_PROBE_PATTERNS = (
    re.compile(r"агент\w*\s+пробив", re.I),  # агент пробивала / агенты пробивали
    re.compile(r"пробив\w*\s+агент", re.I),  # пробивала агент / пробивка агента
    re.compile(r"агент\w*\s+прозвон", re.I),
    re.compile(r"прозвон\w*\s+агент", re.I),
    re.compile(r"агент\w*\s+звон", re.I),  # агент звонил / агентский звонок
    re.compile(r"звон\w*\s+агент", re.I),  # звонила агент / звонок агента
    re.compile(r"агентск\w+\s+(звон|пробив|прозвон|номер)", re.I),
    re.compile(r"это\s+агент\b", re.I),
    re.compile(r"от\s+агент(а|ства|ов)?\b", re.I),
    re.compile(r"лид\s+от\s+агент", re.I),
    re.compile(r"риелтор\w*\s+пробив", re.I),
    re.compile(r"пробив\w*\s+риелтор", re.I),
    re.compile(r"риелтор\w*\s+звон", re.I),
    re.compile(r"звон\w*\s+риелтор", re.I),
)


def _message_content_to_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content or "")


def _parse_violations_json(content: str) -> list[dict[str, Any]]:
    if "{" not in content:
        return []
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence:
        json_str = fence.group(1)
    else:
        json_str = content[content.index("{") :]
    try:
        # raw_decode разбирает JSON-префикс и игнорирует хвост: одно лишнее
        # слово после объекта раньше обнуляло весь чанк находок.
        parsed, _ = json.JSONDecoder().raw_decode(json_str)
    except ValueError:
        logger.warning(
            "Lead quality: LLM response is not valid JSON (%d chars)", len(content),
        )
        return []
    if not isinstance(parsed, dict):
        return []
    violations = parsed.get("violations", [])
    if not isinstance(violations, list):
        return []
    return [v for v in violations if isinstance(v, dict)]


def _sanitize_quality_violations(
    violations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for v in violations:
        rule = str(v.get("rule") or "").strip()
        if rule not in QUALITY_RULES:
            continue
        entity_id = _coerce_int(v.get("entity_id"))
        if entity_id <= 0:
            continue
        reason = str(v.get("reason") or "").strip()
        if not reason:
            continue
        v["entity_type"] = "lead"
        v["entity_id"] = entity_id
        v["responsible_id"] = _coerce_int(v.get("responsible_id"))
        v["severity"] = "high"
        v["rule"] = rule
        v["reason"] = reason
        out.append(v)
    return out


def _timeline_text(timeline: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in timeline or []:
        if not isinstance(item, dict):
            continue
        comment = str(item.get("comment") or "").strip()
        if comment:
            parts.append(comment)
    return "\n".join(parts)


def _combined_text(lead: dict[str, Any]) -> str:
    comments = str(lead.get("comments_field") or "").strip()
    timeline = _timeline_text(lead.get("timeline") or [])
    return f"{comments}\n{timeline}".strip()


def _is_trivial_text(text: str) -> bool:
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    if not cleaned:
        return True
    if cleaned in TRIVIAL_JUSTIFICATION:
        return True
    # BitrixGPT wrapper with only trivial word inside
    plain = re.sub(r"bitrixgpt", "", cleaned, flags=re.I).strip(" :.-")
    return plain in TRIVIAL_JUSTIFICATION or len(plain) < 8


def is_self_call_or_rating_boost(text: str) -> bool:
    """True if comments indicate self-call / rating boost / similar service note."""
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return False
    return any(p.search(cleaned) for p in _SELF_CALL_OR_RATING_PATTERNS)


def is_agent_probe_note(text: str) -> bool:
    """True if comments say an agent/realtor was probing the number."""
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return False
    # bare «агент» only as a short broker mark, not inside long BitrixGPT text
    plain = cleaned.lower().strip(" :;-—!.…")
    if plain in {"агент", "агенты", "агентский", "риелтор", "риэлтор"}:
        return True
    if any(p.search(cleaned) for p in _AGENT_PROBE_PATTERNS):
        return True
    # «агент» + пробив/звон within short note (catch typos / free wording)
    if len(cleaned) <= 80 and re.search(r"\bагент", cleaned, re.I):
        if re.search(r"пробив|прозвон|звон|номер", cleaned, re.I):
            return True
    return False


_BITRIXGPT_BLOCK_RE = re.compile(
    r"\[p\]\s*BitrixGPT\b.*?\[/p\]|BitrixGPT\b.*?(?=\n\n|\Z)",
    re.I | re.S,
)
_SPAM_MARK_RE = re.compile(
    r"^(это\s+)?(полный\s+|явный\s+|очевидный\s+)?спам+[!.…]*$",
    re.I,
)


def _strip_bitrixgpt(text: str) -> str:
    """Remove BitrixGPT summary blocks; keep broker notes."""
    cleaned = _BITRIXGPT_BLOCK_RE.sub(" ", text or "")
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_spam_mark(text: str) -> bool:
    """True if text is a short broker mark that the lead is spam."""
    cleaned = _strip_bitrixgpt(text).lower().strip(" :;-—")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return False
    if re.search(r"\bне\s+спам\b", cleaned) or re.search(r"\bnot\s+spam\b", cleaned):
        return False
    if cleaned in {"спам", "spam", "это спам", "это spam"}:
        return True
    if _SPAM_MARK_RE.match(cleaned):
        return True
    # short note dominated by «спам»
    if "спам" in cleaned and len(cleaned) <= 40:
        return True
    return False


def has_spam_qualification_mark(lead: dict[str, Any]) -> bool:
    """True if broker marked the lead as spam in timeline or COMMENTS."""
    for item in lead.get("timeline") or []:
        if isinstance(item, dict) and _is_spam_mark(str(item.get("comment") or "")):
            return True
    comments = str(lead.get("comments_field") or "")
    broker_notes = _strip_bitrixgpt(comments)
    if _is_spam_mark(broker_notes):
        return True
    # also accept a separate short line «Спам» next to BitrixGPT in raw COMMENTS
    for line in re.split(r"[\n\r]+", comments):
        if _is_spam_mark(line):
            return True
    return False


def lead_has_incoming_activity(lead_id: int) -> bool:
    """True if lead has an incoming CALL activity (DIRECTION=1)."""
    if lead_id <= 0:
        return False
    try:
        raw = _bx_get_all_sync(
            "crm.activity.list",
            {
                "filter": {
                    "OWNER_TYPE_ID": 1,
                    "OWNER_ID": lead_id,
                    "PROVIDER_TYPE_ID": "CALL",
                },
                "select": ["ID", "DIRECTION", "SUBJECT"],
            },
        )
    except Exception:
        logger.debug("activity.list failed for lead_id=%s", lead_id, exc_info=True)
        return False
    for item in raw if isinstance(raw, list) else _as_list(raw):
        if not isinstance(item, dict):
            continue
        if str(item.get("DIRECTION") or "") == "1":
            return True
        subject = str(item.get("SUBJECT") or "").lower()
        if "входящ" in subject:
            return True
    return False


def _unwrap_bx(payload: Any) -> Any:
    if isinstance(payload, dict) and "order0000000000" in payload:
        return payload["order0000000000"]
    return payload


def phone_search_variants(raw: str) -> list[str]:
    """Build phone variants for crm.duplicate.findbycomm."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return []
    variants: set[str] = {digits}
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        body = digits[1:]
        variants.update({f"7{body}", f"8{body}", f"+7{body}"})
    elif len(digits) == 10:
        variants.update({f"7{digits}", f"8{digits}", f"+7{digits}"})
    return sorted(variants)


def fetch_lead_phones(lead_id: int) -> list[str]:
    """Return phone VALUE list from crm.lead.get."""
    if lead_id <= 0:
        return []
    try:
        got = _unwrap_bx(_bx_call_sync("crm.lead.get", {"id": lead_id}))
    except Exception:
        logger.debug("crm.lead.get failed lead_id=%s", lead_id, exc_info=True)
        return []
    if not isinstance(got, dict):
        return []
    phones: list[str] = []
    for item in got.get("PHONE") or []:
        if isinstance(item, dict):
            value = str(item.get("VALUE") or "").strip()
            if value:
                phones.append(value)
    return phones


def _entity_has_deals(*, contact_id: int = 0, company_id: int = 0) -> bool:
    """True if contact/company already has at least one deal in any funnel."""
    filt: dict[str, Any] = {}
    if contact_id > 0:
        filt["CONTACT_ID"] = contact_id
    elif company_id > 0:
        filt["COMPANY_ID"] = company_id
    else:
        return False
    try:
        rows = _bx_get_all_sync(
            "crm.deal.list",
            {"filter": filt, "select": ["ID"]},
        )
    except Exception:
        logger.debug("deal.list failed filter=%s", filt, exc_info=True)
        return False
    items = rows if isinstance(rows, list) else _as_list(rows)
    return bool(items)


def phones_have_deal_in_funnel(phones: list[str]) -> bool:
    """True if any phone is linked to a contact/company that has a deal."""
    values: list[str] = []
    seen: set[str] = set()
    for phone in phones:
        for variant in phone_search_variants(phone):
            if variant not in seen:
                seen.add(variant)
                values.append(variant)
    if not values:
        return False

    contact_ids: set[int] = set()
    company_ids: set[int] = set()
    for i in range(0, len(values), 20):
        batch = values[i : i + 20]
        try:
            raw = _unwrap_bx(
                _bx_call_sync(
                    "crm.duplicate.findbycomm",
                    {"type": "PHONE", "values": batch},
                )
            )
        except Exception:
            logger.debug("findbycomm failed values=%s", batch[:3], exc_info=True)
            continue
        if not isinstance(raw, dict):
            continue
        for cid in raw.get("CONTACT") or []:
            contact_ids.add(_coerce_int(cid))
        for cid in raw.get("COMPANY") or []:
            company_ids.add(_coerce_int(cid))

    for cid in contact_ids:
        if cid > 0 and _entity_has_deals(contact_id=cid):
            return True
    for cid in company_ids:
        if cid > 0 and _entity_has_deals(company_id=cid):
            return True
    return False


def enrich_leads_phone_deal_flags(records: dict[int, dict[str, Any]]) -> None:
    """Set phones + has_deal_by_phone on each lead record (in-place)."""
    if not records:
        return
    with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
        futures = {
            pool.submit(fetch_lead_phones, lid): lid for lid in records
        }
        for future in as_completed(futures):
            lid = futures[future]
            try:
                phones = future.result()
            except Exception:
                phones = []
            records[lid]["phones"] = phones

    # Cache deal existence by normalized primary phone key
    deal_cache: dict[str, bool] = {}
    for lid, lead in records.items():
        phones = lead.get("phones") or []
        if not phones:
            lead["has_deal_by_phone"] = False
            continue
        cache_key = ",".join(sorted({re.sub(r"\D", "", p) for p in phones if p}))
        if cache_key in deal_cache:
            lead["has_deal_by_phone"] = deal_cache[cache_key]
            continue
        has_deal = phones_have_deal_in_funnel(phones)
        deal_cache[cache_key] = has_deal
        lead["has_deal_by_phone"] = has_deal


def is_excluded_quality_lead(lead: dict[str, Any]) -> bool:
    """True if lead must not be treated as quality violation."""
    if lead.get("has_deal_by_phone"):
        return True
    if has_spam_qualification_mark(lead):
        return True
    text = _combined_text(lead)
    if is_self_call_or_rating_boost(text):
        return True
    if is_agent_probe_note(text):
        return True
    return False


def is_quality_candidate(lead: dict[str, Any]) -> bool:
    """Send to LLM if there is meaningful text or an incoming call signal."""
    if is_excluded_quality_lead(lead):
        return False
    text = _combined_text(lead)
    if text and not _is_trivial_text(text):
        return True
    if lead.get("has_incoming_call"):
        return True
    return False


def filter_excluded_findings(
    findings: list[dict[str, Any]],
    leads_by_id: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop LLM findings for excluded leads (spam mark / self-call / deal)."""
    kept: list[dict[str, Any]] = []
    for v in findings:
        lid = _coerce_int(v.get("entity_id"))
        lead = leads_by_id.get(lid) or {}
        if is_excluded_quality_lead(lead):
            logger.info("Skip excluded quality finding lead_id=%s rule=%s", lid, v.get("rule"))
            continue
        kept.append(v)
    return kept


def collect_spam_nontarget_leads(since: str) -> list[dict[str, Any]]:
    """Fetch JUNK / Non-target leads created since date, with timeline + incoming flag."""
    status_names = _fetch_lead_status_names()
    leads_raw = _bx_get_all_sync(
        "crm.lead.list",
        {
            "filter": {
                "STATUS_ID": STATUS_FILTER,
                ">=DATE_CREATE": since,
            },
            "select": [
                "ID",
                "TITLE",
                "STATUS_ID",
                "ASSIGNED_BY_ID",
                "DATE_CREATE",
                "COMMENTS",
                "SOURCE_ID",
            ],
        },
    )
    leads = leads_raw if isinstance(leads_raw, list) else _as_list(leads_raw)
    records: dict[int, dict[str, Any]] = {}
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        lead_id = _coerce_int(lead.get("ID"))
        if lead_id <= 0:
            continue
        status_id = _clean_str(lead.get("STATUS_ID"))
        records[lead_id] = {
            "lead_id": lead_id,
            "title": _clean_str(lead.get("TITLE")),
            "status_id": status_id,
            "status_name": status_names.get(status_id, status_id),
            "assigned_by_id": _coerce_int(lead.get("ASSIGNED_BY_ID")),
            "date_create": _clean_str(lead.get("DATE_CREATE")),
            "comments_field": _clean_str(lead.get("COMMENTS")),
            "source_id": _clean_str(lead.get("SOURCE_ID")),
            "timeline": [],
            "has_incoming_call": False,
            "phones": [],
            "has_deal_by_phone": False,
        }

    if not records:
        return []

    with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_entity_timeline, lid, "lead"): ("timeline", lid)
            for lid in records
        }
        futures.update(
            {
                pool.submit(lead_has_incoming_activity, lid): ("incoming", lid)
                for lid in records
            }
        )
        for future in as_completed(futures):
            kind, lid = futures[future]
            try:
                result = future.result()
            except Exception:
                continue
            if kind == "timeline":
                _, timeline = result
                records[lid]["timeline"] = timeline
            else:
                records[lid]["has_incoming_call"] = bool(result)

    enrich_leads_phone_deal_flags(records)
    return list(records.values())


def analyze_leads_with_llm(
    leads: list[dict[str, Any]],
    settings: Settings,
    current_time: str,
) -> list[dict[str, Any]]:
    """Run DeepSeek quality analyst in chunks."""
    if not leads:
        return []
    llm = make_llm(settings)
    chunk_size = max(1, int(settings.lead_quality_chunk_size))
    fields = (
        "lead_id",
        "title",
        "status_id",
        "status_name",
        "assigned_by_id",
        "date_create",
        "comments_field",
        "timeline",
        "has_incoming_call",
        "has_deal_by_phone",
        "phones",
    )
    all_violations: list[dict[str, Any]] = []
    for i in range(0, len(leads), chunk_size):
        chunk = [
            {k: lead[k] for k in fields if k in lead}
            for lead in leads[i : i + chunk_size]
        ]
        payload = {"leads": chunk, "current_time": current_time}
        try:
            response = llm.invoke(
                [
                    SystemMessage(content=LEAD_QUALITY_ANALYST_PROMPT),
                    HumanMessage(
                        content=json.dumps(payload, ensure_ascii=False, default=str),
                    ),
                ]
            )
            content = _message_content_to_str(response.content)
            parsed = _sanitize_quality_violations(_parse_violations_json(content))
            all_violations.extend(parsed)
            logger.info(
                "Lead quality chunk %d-%d: %d findings",
                i,
                min(i + chunk_size, len(leads)),
                len(parsed),
            )
        except Exception:
            logger.exception(
                "Lead quality LLM chunk failed: %d-%d",
                i,
                min(i + chunk_size, len(leads)),
            )
    return all_violations


def _resolve_names(user_ids: set[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    if not user_ids:
        return names
    try:
        users = _bx_get_all_sync(
            "user.get",
            {"filter": {"ACTIVE": True}, "select": ["ID", "NAME", "LAST_NAME"]},
        ) or []
    except Exception:
        users = []
    for u in users:
        uid = _coerce_int(u.get("ID"))
        if uid in user_ids:
            names[uid] = f"{u.get('LAST_NAME') or ''} {u.get('NAME') or ''}".strip()
    for uid in user_ids:
        if uid not in names:
            names[uid] = f"ID {uid}"
    return names


def format_quality_report(
    findings: list[dict[str, Any]],
    *,
    checked: int,
    candidates: int,
    since: str,
    name_map: dict[int, str],
) -> str:
    """Build Russian report for REPORT_CHAT."""
    lines = [
        "b24-ai-auditor — Контроль Спам / Нецелевой",
        f"Период: с {since}",
        f"Проверено лидов: {checked}, кандидатов на LLM: {candidates}, "
        f"новых находок: {len(findings)}",
        "",
    ]
    if not findings:
        lines.append("Новых слитых / неверно квалифицированных лидов не найдено.")
        return "\n".join(lines)

    by_rule = Counter(str(v.get("rule")) for v in findings)
    lines.append(
        f"Слитые (lead_leaked): {by_rule.get('lead_leaked', 0)}; "
        f"неверная квалификация: {by_rule.get('lead_wrong_qualification', 0)}"
    )
    lines.append("")

    for v in sorted(
        findings,
        key=lambda x: (_coerce_int(x.get("responsible_id")), _coerce_int(x.get("entity_id"))),
    ):
        lid = _coerce_int(v.get("entity_id"))
        rid = _coerce_int(v.get("responsible_id"))
        rule = str(v.get("rule") or "")
        label = (
            "Слитый"
            if rule == "lead_leaked"
            else "Неверная квалификация"
            if rule == "lead_wrong_qualification"
            else rule
        )
        details = v.get("details") if isinstance(v.get("details"), dict) else {}
        status_name = str(
            details.get("status_name")
            or v.get("status_name")
            or ""
        )
        link = _build_crm_link("lead", lid)
        lines.append(
            f"🔴 Лид #{lid} | {name_map.get(rid, f'ID {rid}')} | "
            f"{label}"
            + (f" | {status_name}" if status_name else "")
        )
        lines.append(f"   {v.get('reason')}")
        lines.append(f"   {link}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def purge_excluded_stored_findings(
    leads_by_id: dict[int, dict[str, Any]],
) -> int:
    """Remove DB findings for excluded leads (spam mark / self-call / deal).

    Returns the number of deleted rows.
    """
    deleted = 0
    for row in list_lead_quality_findings():
        lid = _coerce_int(row.get("lead_id"))
        rule = str(row.get("rule") or "")
        lead = leads_by_id.get(lid)
        if not lead:
            continue
        if is_excluded_quality_lead(lead):
            deleted += delete_lead_quality_finding(lid, rule)
    return deleted


def run_lead_quality_audit(settings: Settings | None = None) -> dict[str, Any]:
    """Collect → filter → LLM → dedup → report."""
    settings = settings or get_settings()
    if not settings.lead_quality_enabled:
        logger.info("LEAD_QUALITY_ENABLED=false — skip")
        return {"status": "skipped", "checked": 0, "findings": 0}

    init_db()
    since = (settings.lead_quality_since or "2026-07-01").strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    logger.info(
        "Lead quality audit: since=%s DRY_RUN=%s chunk=%s",
        since,
        settings.dry_run,
        settings.lead_quality_chunk_size,
    )

    leads = collect_spam_nontarget_leads(since)
    leads_by_id = {_coerce_int(lead["lead_id"]): lead for lead in leads}
    purged = purge_excluded_stored_findings(leads_by_id)
    if purged:
        logger.info("Purged %d stored findings (spam / self-call / deal)", purged)

    candidates = [lead for lead in leads if is_quality_candidate(lead)]
    logger.info(
        "Lead quality: fetched=%d candidates=%d",
        len(leads),
        len(candidates),
    )

    raw_findings = filter_excluded_findings(
        analyze_leads_with_llm(candidates, settings, now_iso),
        leads_by_id,
    )

    new_findings: list[dict[str, Any]] = []
    for v in raw_findings:
        lid = _coerce_int(v.get("entity_id"))
        rule = str(v.get("rule") or "")
        if was_lead_quality_finding_recorded(lid, rule):
            continue
        details = v.get("details") if isinstance(v.get("details"), dict) else {}
        status_name = str(details.get("status_name") or "")
        inserted = save_lead_quality_finding(
            lid,
            rule,
            _coerce_int(v.get("responsible_id")),
            str(v.get("reason") or ""),
            status_name,
            now_iso,
            notified_at=None,
        )
        if inserted:
            new_findings.append(v)

    name_map = _resolve_names(
        {_coerce_int(v.get("responsible_id")) for v in new_findings}
    )
    report = format_quality_report(
        new_findings,
        checked=len(leads),
        candidates=len(candidates),
        since=since,
        name_map=name_map,
    )

    sent = False
    admin_id = settings.contact_source_lock_notify_user
    if settings.dry_run:
        logger.info(
            "DRY_RUN: would send quality report to admin user %s (%d chars)",
            admin_id,
            len(report),
        )
        print(report)
    else:
        try:
            send_user_chat_message_chunked(admin_id, report)
            sent = True
            for v in new_findings:
                mark_lead_quality_notified(
                    _coerce_int(v.get("entity_id")),
                    str(v.get("rule") or ""),
                    now_iso,
                )
        except Exception:
            logger.exception("Failed to send lead quality report to admin %s", admin_id)

    return {
        "status": "ok",
        "checked": len(leads),
        "candidates": len(candidates),
        "findings": len(new_findings),
        "sent": sent,
        "dry_run": settings.dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Lead quality audit (Spam/Non-target)")
    parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level)
    result = run_lead_quality_audit(settings)
    logger.info("Lead quality done: %s", result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
