"""Buyer-funnel agent: recover client state from CRM card evidence."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from config import Settings, get_settings
from db import get_client_state, init_db, save_client_state
from lead_quality_audit import _make_llm, _message_content_to_str
from masking import MaskMap, apply_mask, build_mask_map, unmask
from tools import (
    _as_list,
    _bx_get_all_sync,
    _clean_str,
    _coerce_int,
    _evidence_incomplete,
    _fetch_deal_activities,
    _fetch_entity_timeline,
)
from transcripts import STATUS_NOT_READY, STATUS_OK, fetch_and_cache

logger = logging.getLogger(__name__)

CALL_ACTIVITY_TYPE_ID = 2
VALID_RISKS = frozenset({"low", "medium", "high"})
VALID_NEXT_STEP_WHO = frozenset({"broker", "client", "unknown"})

BUYER_CLIENT_STATE_SYSTEM_PROMPT = """\
Ты аналитик CRM по сделкам покупателей недвижимости.

Задача: восстановить состояние клиента по карточке — что ищет, что происходит,
следующий шаг, риски. Ты НЕ оцениваешь брокера и НЕ придумываешь факты.

Правила:
1. Каждое утверждение подкрепляй дословной цитатой в массиве evidence.
   Без цитаты поле оставь пустым / unknown / добавь в missing.
2. «Не знаю» — нормальный ответ. Лучше низкий confidence и missing,
   чем правдоподобная выдумка.
3. Расшифровки звонков — сплошной текст без разделения спикеров.
   Если неясно, кто говорил, так и пиши; не приписывай реплику клиенту наугад.
4. null / отсутствие расшифровки означает «текст ещё не готов», а не «звонка не было».
5. recoverable=false — если по карточке нельзя восстановить картину клиента
   (типичный пример: «созвонился, договорились» без деталей).

Ответ — один JSON-объект (без markdown), строго по схеме:
{
  "client_goal": "string",
  "situation": "string",
  "last_event": {"what": "string", "when": "YYYY-MM-DD or unknown"},
  "next_step": {"what": "string", "when": "string", "who": "broker|client|unknown"},
  "blockers": ["string"],
  "risk": "low|medium|high",
  "recoverable": true,
  "missing": ["string"],
  "confidence": 0.0,
  "evidence": ["string"]
}
"""


class LLMClient(Protocol):
    def invoke(self, messages: list[Any]) -> Any:
        ...


def _parse_iso_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _event_sort_key(item: dict[str, Any]) -> str:
    for key in ("created", "when", "CREATED", "START_TIME", "fetched_at"):
        text = _clean_str(item.get(key))
        if text:
            return text
    return ""


def _normalize_timeline_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "comment",
        "id": _coerce_int(item.get("id") or item.get("ID")),
        "created": _clean_str(item.get("created") or item.get("CREATED")),
        "text": _clean_str(item.get("comment") or item.get("COMMENT")),
        "author_id": _coerce_int(item.get("author_id") or item.get("AUTHOR_ID")),
    }


def _normalize_activity_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "activity",
        "id": _coerce_int(item.get("ID") or item.get("id")),
        "created": _clean_str(
            item.get("CREATED") or item.get("START_TIME") or item.get("created"),
        ),
        "subject": _clean_str(item.get("SUBJECT") or item.get("subject")),
        "description": _clean_str(item.get("DESCRIPTION") or item.get("description")),
        "completed": _clean_str(item.get("COMPLETED") or item.get("completed")),
        "type_id": _coerce_int(item.get("TYPE_ID") or item.get("type_id")),
    }


def _normalize_transcript_item(item: dict[str, Any]) -> dict[str, Any]:
    status = _clean_str(item.get("status"))
    text = _clean_str(item.get("text"))
    return {
        "kind": "transcript",
        "id": _coerce_int(item.get("activity_id") or item.get("id")),
        "created": _clean_str(item.get("activity_created") or item.get("fetched_at")),
        "status": status,
        "text": text if status == STATUS_OK else "",
        "note": (
            "расшифровка не готова"
            if status == STATUS_NOT_READY
            else ("ошибка загрузки" if status == "error" else "")
        ),
    }


def build_evidence_events(
    timeline: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    transcripts: list[dict[str, Any]],
    mask_map: MaskMap,
) -> list[dict[str, Any]]:
    """Merge card evidence into a single chronologically sorted event list."""
    events: list[dict[str, Any]] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_timeline_item(item)
        if not normalized["text"]:
            continue
        normalized["text"] = apply_mask(normalized["text"], mask_map)
        events.append(normalized)
    for item in activities:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_activity_item(item)
        body = " — ".join(
            part for part in (normalized["subject"], normalized["description"]) if part
        )
        if not body:
            continue
        normalized["text"] = apply_mask(body, mask_map)
        events.append(normalized)
    for item in transcripts:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_transcript_item(item)
        if normalized["text"]:
            normalized["text"] = apply_mask(normalized["text"], mask_map)
        elif not normalized["note"]:
            continue
        events.append(normalized)
    events.sort(key=_event_sort_key)
    return events


def compute_content_hash(events: list[dict[str, Any]]) -> str:
    """Stable fingerprint of card evidence for skip-if-unchanged."""
    payload = [
        {
            "kind": e.get("kind"),
            "id": e.get("id"),
            "created": e.get("created"),
            "text": e.get("text"),
            "status": e.get("status"),
            "note": e.get("note"),
        }
        for e in events
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def filter_new_events(
    events: list[dict[str, Any]],
    analyzed_at: str | None,
) -> list[dict[str, Any]]:
    """Return events newer than the last successful analysis watermark."""
    if not analyzed_at:
        return list(events)
    watermark = _parse_iso_datetime(analyzed_at)
    if watermark is None:
        return list(events)
    fresh: list[dict[str, Any]] = []
    for event in events:
        created = _parse_iso_datetime(str(event.get("created") or ""))
        if created is None or created > watermark:
            fresh.append(event)
    return fresh


def trim_events_to_budget(
    events: list[dict[str, Any]],
    max_chars: int,
) -> tuple[list[dict[str, Any]], int]:
    """Keep the most recent events within a character budget.

    Returns (kept, dropped). The first analysis of a card sends its whole
    history, so a card with two dozen calls would otherwise blow past the
    model's context limit.
    """
    if max_chars <= 0:
        return list(events), 0
    kept: list[dict[str, Any]] = []
    used = 0
    for event in reversed(events):
        text = str(event.get("text") or "")
        size = len(text) + len(str(event.get("note") or ""))
        if kept and used + size > max_chars:
            break
        if not kept and size > max_chars:
            # Одна расшифровка длинного разговора может сама не влезть в лимит.
            # Обрезаем её, а не выбрасываем: начало звонка обычно и несёт суть.
            event = dict(event, text=text[:max_chars], truncated=True)
            size = max_chars
        used += size
        kept.append(event)
    kept.reverse()
    return kept, len(events) - len(kept)


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def verify_evidence(
    quotes: list[str],
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Split quotes into (found in the card, not found).

    The prompt demands verbatim citations, but nothing stopped the model from
    inventing one. A quote that is not in the source is the clearest signal
    that the rest of the answer was imagined too.
    """
    corpus = _normalize_for_match(
        " ".join(str(e.get("text") or "") for e in events),
    )
    verified: list[str] = []
    invented: list[str] = []
    for quote in quotes:
        needle = _normalize_for_match(quote)
        if needle and needle in corpus:
            verified.append(quote)
        else:
            invented.append(quote)
    return verified, invented


def _parse_state_json(content: str) -> dict[str, Any] | None:
    if "{" not in content:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    json_str = fence.group(1) if fence else content[content.index("{") :]
    try:
        parsed, _ = json.JSONDecoder().raw_decode(json_str)
    except ValueError:
        logger.warning("Client state: LLM response is not valid JSON (%d chars)", len(content))
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_state(raw: dict[str, Any]) -> dict[str, Any]:
    last_event = raw.get("last_event") if isinstance(raw.get("last_event"), dict) else {}
    next_step = raw.get("next_step") if isinstance(raw.get("next_step"), dict) else {}
    blockers = raw.get("blockers") if isinstance(raw.get("blockers"), list) else []
    missing = raw.get("missing") if isinstance(raw.get("missing"), list) else []
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
    risk = str(raw.get("risk") or "medium").strip().lower()
    if risk not in VALID_RISKS:
        risk = "medium"
    who = str(next_step.get("who") or "unknown").strip().lower()
    if who not in VALID_NEXT_STEP_WHO:
        who = "unknown"
    return {
        "client_goal": _clean_str(raw.get("client_goal")),
        "situation": _clean_str(raw.get("situation")),
        "last_event": {
            "what": _clean_str(last_event.get("what")),
            "when": _clean_str(last_event.get("when")) or "unknown",
        },
        "next_step": {
            "what": _clean_str(next_step.get("what")),
            "when": _clean_str(next_step.get("when")) or "unknown",
            "who": who,
        },
        "blockers": [_clean_str(x) for x in blockers if _clean_str(x)],
        "risk": risk,
        "recoverable": bool(raw.get("recoverable", True)),
        "missing": [_clean_str(x) for x in missing if _clean_str(x)],
        "confidence": max(0.0, min(1.0, _coerce_float(raw.get("confidence"), 0.0))),
        "evidence": [_clean_str(x) for x in evidence if _clean_str(x)],
    }


def unmask_state(state: dict[str, Any], mask_map: MaskMap) -> dict[str, Any]:
    """Human-facing copy with real names restored.

    The stored copy stays masked: it is fed back to the model as
    previous_state on the next run, and unmasking before saving would leak
    the contacts the masking exists to protect.
    """
    out = json.loads(json.dumps(state, ensure_ascii=False))
    for key in ("client_goal", "situation"):
        out[key] = unmask(out.get(key, ""), mask_map)
    for key in ("last_event", "next_step"):
        block = out.get(key)
        if isinstance(block, dict):
            block["what"] = unmask(block.get("what", ""), mask_map)
    for key in ("blockers", "missing", "evidence"):
        values = out.get(key)
        if isinstance(values, list):
            out[key] = [unmask(str(v), mask_map) for v in values]
    return out


def build_llm_payload(
    deal: dict[str, Any],
    previous_state: dict[str, Any] | None,
    new_events: list[dict[str, Any]],
    all_events: list[dict[str, Any]],
) -> str:
    """Human message body for incremental state update."""
    payload = {
        "deal_id": _coerce_int(deal.get("ID") or deal.get("id")),
        "title": _clean_str(deal.get("TITLE") or deal.get("title")),
        "stage_id": _clean_str(deal.get("STAGE_ID") or deal.get("stage_id")),
        "previous_state": previous_state,
        "new_events": new_events,
        "event_count_total": len(all_events),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def analyze_with_llm(
    deal: dict[str, Any],
    previous_state: dict[str, Any] | None,
    new_events: list[dict[str, Any]],
    all_events: list[dict[str, Any]],
    llm: LLMClient,
) -> dict[str, Any] | None:
    """Call LLM and return normalized client state."""
    human = build_llm_payload(deal, previous_state, new_events, all_events)
    response = llm.invoke([
        SystemMessage(content=BUYER_CLIENT_STATE_SYSTEM_PROMPT),
        HumanMessage(content=human),
    ])
    content = _message_content_to_str(getattr(response, "content", response))
    parsed = _parse_state_json(content)
    if not parsed:
        return None
    return _normalize_state(parsed)


def fetch_deal_contacts(deal: dict[str, Any]) -> list[dict[str, Any]]:
    """Load contacts linked to a deal for masking."""
    if isinstance(deal.get("contacts"), list):
        return [c for c in deal["contacts"] if isinstance(c, dict)]
    contact_ids: list[int] = []
    main_id = _coerce_int(deal.get("CONTACT_ID") or deal.get("contact_id"))
    if main_id > 0:
        contact_ids.append(main_id)
    deal_id = _coerce_int(deal.get("ID") or deal.get("id"))
    if deal_id > 0:
        raw = _bx_get_all_sync(
            "crm.deal.contact.items.get",
            {"id": deal_id},
        )
        for row in _as_list(raw):
            if isinstance(row, dict):
                cid = _coerce_int(row.get("CONTACT_ID") or row.get("contact_id"))
                if cid > 0:
                    contact_ids.append(cid)
    contacts: list[dict[str, Any]] = []
    seen: set[int] = set()
    for cid in contact_ids:
        if cid in seen:
            continue
        seen.add(cid)
        try:
            row = _bx_get_all_sync("crm.contact.get", {"id": cid})
        except Exception:
            logger.warning("Contact fetch failed for deal contact id=%s", cid)
            continue
        if isinstance(row, dict):
            contacts.append(row)
    return contacts


def prepare_deal_record(deal: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Attach timeline, activities, transcripts; mark incomplete evidence."""
    settings = settings or get_settings()
    deal_id = _coerce_int(deal.get("ID") or deal.get("id"))
    _, timeline, timeline_failed = _fetch_entity_timeline(deal_id, "deal")
    _, activities, activities_failed = _fetch_deal_activities(deal_id)
    record = dict(deal)
    record["timeline"] = timeline
    record["activities"] = activities
    record["evidence_incomplete"] = timeline_failed or activities_failed
    record["contacts"] = fetch_deal_contacts(deal)
    if not record["evidence_incomplete"]:
        record["transcripts"] = fetch_and_cache(deal_id, settings=settings)
    else:
        record["transcripts"] = []
    return record


def analyze_buyer_deal(
    deal: dict[str, Any],
    *,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    prepared: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Analyze one buyer deal; returns result envelope with state or skip reason."""
    settings = settings or get_settings()
    deal_id = _coerce_int(deal.get("ID") or deal.get("id"))
    envelope: dict[str, Any] = {
        "deal_id": deal_id,
        "skipped": False,
        "reason": "",
        "state": None,
        "content_hash": "",
    }
    if deal_id <= 0:
        envelope["skipped"] = True
        envelope["reason"] = "invalid_deal_id"
        return envelope

    if prepared:
        record = deal
    else:
        try:
            record = prepare_deal_record(deal, settings=settings)
        except Exception as exc:  # noqa: BLE001 — одна карточка не роняет батч
            logger.warning("Card collection failed for deal %s: %s", deal_id, exc)
            envelope["skipped"] = True
            envelope["reason"] = "collect_error"
            return envelope
    if _evidence_incomplete(record):
        envelope["skipped"] = True
        envelope["reason"] = "evidence_incomplete"
        return envelope

    mask_map = build_mask_map(record, record.get("contacts"))
    events = build_evidence_events(
        _as_list(record.get("timeline")),
        _as_list(record.get("activities")),
        _as_list(record.get("transcripts")),
        mask_map,
    )
    content_hash = compute_content_hash(events)
    envelope["content_hash"] = content_hash

    # Кэш читается и в DRY_RUN: чтение ничего не меняет, а без него тестовый
    # прогон заново гоняет модель по всем карточкам. Повторный разбор — по force.
    stored = get_client_state(deal_id)
    if stored and stored.get("content_hash") == content_hash and not force:
        envelope["skipped"] = True
        envelope["reason"] = "unchanged"
        envelope["state"] = json.loads(stored.get("state_json") or "{}")
        return envelope

    previous_state = None
    analyzed_at = None
    if stored:
        try:
            previous_state = json.loads(stored.get("state_json") or "{}")
        except json.JSONDecodeError:
            previous_state = None
        analyzed_at = str(stored.get("analyzed_at") or "")

    new_events = filter_new_events(events, analyzed_at)
    if previous_state is None:
        new_events = events
    new_events, dropped = trim_events_to_budget(
        new_events, int(settings.client_state_max_event_chars),
    )
    if dropped:
        logger.info(
            "Deal %s: %d oldest events dropped to fit the context budget",
            deal_id,
            dropped,
        )

    model = llm or _make_llm(settings)
    try:
        state = analyze_with_llm(record, previous_state, new_events, events, model)
    except Exception as exc:  # noqa: BLE001 — one card must not abort the batch
        logger.warning("Client state LLM failed for deal %s: %s", deal_id, exc)
        envelope["skipped"] = True
        envelope["reason"] = "llm_error"
        return envelope

    if state is None:
        logger.warning("Client state parse failed for deal %s", deal_id)
        envelope["skipped"] = True
        envelope["reason"] = "parse_error"
        return envelope

    verified, invented = verify_evidence(state.get("evidence", []), events)
    state["evidence"] = verified
    if invented:
        logger.warning(
            "Deal %s: %d quote(s) not found in the card — dropped as invented",
            deal_id,
            len(invented),
        )
    if not verified:
        # Ни одной подтверждённой цитаты — доверять такому выводу нельзя,
        # каким бы уверенным он ни выглядел.
        state["confidence"] = min(float(state.get("confidence") or 0.0), 0.3)
        if "подтверждённые цитаты" not in state["missing"]:
            state["missing"].append("подтверждённые цитаты")
    envelope["evidence_dropped"] = len(invented)

    # В БД уходит замаскированная копия: на следующем прогоне она вернётся
    # в модель как previous_state. Разворачиваем только то, что читают люди.
    envelope["state"] = unmask_state(state, mask_map)

    if not settings.dry_run:
        init_db()
        now_iso = datetime.now(timezone.utc).isoformat()
        save_client_state(
            deal_id=deal_id,
            state_json=json.dumps(state, ensure_ascii=False),
            confidence=float(state.get("confidence") or 0.0),
            content_hash=content_hash,
            analyzed_at=now_iso,
            model=settings.deepseek_model,
        )
    else:
        logger.info(
            "DRY_RUN: client state for deal %s — recoverable=%s confidence=%.2f",
            deal_id,
            state.get("recoverable"),
            float(state.get("confidence") or 0.0),
        )
    return envelope


def run_buyer_client_state(
    deals: list[dict[str, Any]] | None = None,
    *,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Batch runner for buyer deals. Reads open deals when list is omitted."""
    settings = settings or get_settings()
    if deals is None:
        raw = _bx_get_all_sync(
            "crm.deal.list",
            {
                "filter": {
                    "CATEGORY_ID": settings.buyers_category_id,
                    "CLOSED": "N",
                },
                "select": ["ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID", "CONTACT_ID"],
            },
        )
        deals = [d for d in _as_list(raw) if isinstance(d, dict)]

    stats = {
        "total": len(deals),
        "analyzed": 0,
        "skipped_unchanged": 0,
        "skipped_incomplete": 0,
        "skipped_other": 0,
        "errors": 0,
        "unrecoverable": 0,
        "evidence_dropped": 0,
        "results": [],
    }
    for deal in deals:
        try:
            result = analyze_buyer_deal(
                deal, settings=settings, llm=llm, force=force,
            )
        except Exception as exc:  # noqa: BLE001 — одна карточка не роняет батч
            logger.warning(
                "Client state failed for deal %s: %s",
                _coerce_int(deal.get("ID") or deal.get("id")),
                exc,
            )
            result = {
                "deal_id": _coerce_int(deal.get("ID") or deal.get("id")),
                "skipped": True,
                "reason": "unexpected_error",
                "state": None,
                "content_hash": "",
            }
        stats["results"].append(result)
        reason = result.get("reason") or ""
        if result.get("skipped"):
            if reason == "unchanged":
                stats["skipped_unchanged"] += 1
            elif reason == "evidence_incomplete":
                stats["skipped_incomplete"] += 1
            elif reason in {
                "llm_error", "parse_error", "collect_error", "unexpected_error",
            }:
                stats["errors"] += 1
            else:
                stats["skipped_other"] += 1
            continue
        stats["analyzed"] += 1
        stats["evidence_dropped"] += int(result.get("evidence_dropped") or 0)
        state = result.get("state") or {}
        if state.get("recoverable") is False:
            stats["unrecoverable"] += 1
    return stats
