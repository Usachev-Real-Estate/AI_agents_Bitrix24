"""Fetch and cache Bitrix call transcripts for CRM deals."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Settings, get_settings
from db import get_call_transcript, init_db, upsert_call_transcript
from notify import _bx_call_sync
from tools import _as_list, _bx_get_all_sync, _clean_str, _coerce_int

logger = logging.getLogger(__name__)

CALL_ACTIVITY_TYPE_ID = 2
STATUS_OK = "ok"
STATUS_NOT_READY = "not_ready"
STATUS_ERROR = "error"


def list_call_activities(deal_id: int) -> list[dict[str, Any]]:
    """Return call activities linked to a deal."""
    raw = _bx_get_all_sync(
        "crm.activity.list",
        {
            "filter": {
                "OWNER_TYPE_ID": 2,
                "OWNER_ID": deal_id,
                "TYPE_ID": CALL_ACTIVITY_TYPE_ID,
            },
            "select": ["ID", "SUBJECT", "CREATED", "DIRECTION", "COMPLETED"],
        },
    )
    return [a for a in _as_list(raw) if isinstance(a, dict)]


def _parse_fetched_at(value: str) -> datetime | None:
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


def _retry_due(
    row: dict[str, Any] | None,
    retry_hours: float,
    now: datetime,
) -> bool:
    """True when a cached not_ready/error row may be fetched again."""
    if not row:
        return True
    status = str(row.get("status") or "")
    if status == STATUS_OK:
        return False
    fetched_at = _parse_fetched_at(str(row.get("fetched_at") or ""))
    if fetched_at is None:
        return True
    return now - fetched_at >= timedelta(hours=retry_hours)


def fetch_transcript_from_api(activity_id: int) -> tuple[str | None, str]:
    """Return (text, status). null API result → not_ready, not «no call»."""
    try:
        result = _bx_call_sync(
            "crm.activity.call.getTranscript",
            {"activityId": activity_id},
        )
    except Exception as exc:  # noqa: BLE001 — one failed call must not abort the deal
        logger.warning(
            "Transcript fetch error for activity %s: %s",
            activity_id,
            exc,
        )
        return None, STATUS_ERROR
    if not result:
        return None, STATUS_NOT_READY
    if isinstance(result, dict):
        text = _clean_str(
            result.get("transcription") or result.get("TRANSCRIPTION"),
        )
        if text:
            return text, STATUS_OK
        return None, STATUS_NOT_READY
    text = _clean_str(result)
    return (text, STATUS_OK) if text else (None, STATUS_NOT_READY)


def fetch_and_cache(
    deal_id: int,
    *,
    settings: Settings | None = None,
    dry_run: bool | None = None,
) -> list[dict[str, Any]]:
    """Load call transcripts for a deal, using SQLite cache when possible."""
    settings = settings or get_settings()
    write_db = not (settings.dry_run if dry_run is None else dry_run)
    if write_db:
        init_db()

    retry_hours = float(settings.client_state_transcript_retry_hours)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    activities = list_call_activities(deal_id)
    out: list[dict[str, Any]] = []

    for activity in activities:
        activity_id = _coerce_int(activity.get("ID"))
        if activity_id <= 0:
            continue
        cached = get_call_transcript(activity_id)
        if cached and cached.get("status") == STATUS_OK:
            out.append(cached)
            continue
        if cached and not _retry_due(cached, retry_hours, now):
            out.append(cached)
            continue

        text, status = fetch_transcript_from_api(activity_id)
        row = {
            "activity_id": activity_id,
            "deal_id": deal_id,
            "text": text or "",
            "status": status,
            "fetched_at": now_iso,
            "chars": len(text or ""),
            "activity_created": _clean_str(activity.get("CREATED")),
        }
        if write_db:
            upsert_call_transcript(
                activity_id=activity_id,
                deal_id=deal_id,
                text=row["text"],
                status=status,
                fetched_at=now_iso,
                chars=row["chars"],
            )
        out.append(row)

    return out
