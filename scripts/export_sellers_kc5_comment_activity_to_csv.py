#!/usr/bin/env python3
"""
Export seller-funnel deals where:
  - SOURCE_ID is "КЦ - 5%" (code "24")
  - there is a deal timeline comment (non-empty)
  - there is at least one CRM activity ("дело") linked to the deal

Saves results to data/*.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
from tools import (  # noqa: E402
    SELLERS_PAID_SOURCE_NAMES,
    _build_crm_link,
    get_deals_by_funnel_with_timeline,
)

logger = logging.getLogger(__name__)


def _first_non_empty_comment(timeline: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the latest non-empty timeline comment record (by `created` string)."""
    non_empty = [t for t in timeline if str(t.get("comment") or "").strip()]
    if not non_empty:
        return None
    try:
        non_empty.sort(key=lambda x: str(x.get("created") or ""))
    except Exception:
        pass
    return non_empty[-1]


def _activity_has_evidence(activity: dict[str, Any]) -> bool:
    subject = str(activity.get("subject") or activity.get("SUBJECT") or "").strip()
    desc = str(activity.get("description") or activity.get("DESCRIPTION") or "").strip()
    return bool(subject or desc)


def _activity_summary(activity: dict[str, Any]) -> tuple[str, str, str]:
    subject = str(activity.get("subject") or activity.get("SUBJECT") or "").strip()
    description = (
        str(activity.get("description") or activity.get("DESCRIPTION") or "").strip()
    )
    created = str(activity.get("created") or activity.get("CREATED") or "").strip()
    return subject, description, created


def export(
    source_id: str,
    out_csv: Path,
) -> int:
    settings = get_settings()
    setup_logging(settings.log_level)

    sellers_category_id = settings.sellers_category_id
    deals_payload = get_deals_by_funnel_with_timeline.invoke(
        {"category_id": sellers_category_id},
    )
    deals: list[dict[str, Any]] = deals_payload.get("deals") or []
    if not isinstance(deals, list):
        deals = []

    source_name = SELLERS_PAID_SOURCE_NAMES.get(source_id, source_id)

    rows: list[dict[str, Any]] = []
    for d in deals:
        deal_id = d.get("deal_id") or d.get("ID") or d.get("id")
        deal_id_int = str(deal_id or "").strip()
        if not deal_id_int:
            continue

        src = str(d.get("source_id") or d.get("SOURCE_ID") or "").strip()
        if src != str(source_id).strip():
            # Be strict: user asks specifically "КЦ-5%" which maps to SOURCE_ID=24.
            continue

        timeline: list[dict[str, Any]] = d.get("timeline") or []
        if not isinstance(timeline, list):
            timeline = []
        comment_rec = _first_non_empty_comment(timeline)
        if not comment_rec:
            continue

        activities: list[dict[str, Any]] = d.get("deal_activities") or []
        if not isinstance(activities, list):
            activities = []
        evidence_activities = [a for a in activities if isinstance(a, dict) and _activity_has_evidence(a)]
        if not evidence_activities:
            continue

        # Keep CSV compact: store first N subjects and a JSON-ish activity ids list.
        act_subjects = []
        act_ids = []
        for a in evidence_activities[:20]:
            subj, _desc, _created = _activity_summary(a)
            if subj:
                act_subjects.append(subj)
            aid = a.get("ID") or a.get("id")
            if aid is not None:
                act_ids.append(str(aid))

        comment_text = str(comment_rec.get("comment") or "").strip()

        rows.append(
            {
                "deal_id": deal_id_int,
                "deal_link": _build_crm_link("deal", int(deal_id_int))
                if str(deal_id_int).isdigit()
                else "",
                "title": str(d.get("title") or d.get("TITLE") or "").strip(),
                "stage_id": str(d.get("stage_id") or d.get("STAGE_ID") or "").strip(),
                "stage_name": str(d.get("stage_name") or "").strip(),
                "source_id": src,
                "source_name": source_name,
                "date_create": str(d.get("date_create") or d.get("DATE_CREATE") or "").strip(),
                "assigned_by_id": str(d.get("assigned_by_id") or d.get("ASSIGNED_BY_ID") or "").strip(),
                "comment_author_id": str(comment_rec.get("author_id") or comment_rec.get("AUTHOR_ID") or "").strip(),
                "comment_created": str(comment_rec.get("created") or comment_rec.get("CREATED") or "").strip(),
                "comment_text": comment_text,
                "deal_activity_count": len(evidence_activities),
                "deal_activity_subjects": "; ".join(act_subjects),
                "deal_activity_ids": ";".join(act_ids),
            }
        )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "deal_id",
        "deal_link",
        "title",
        "stage_id",
        "stage_name",
        "source_id",
        "source_name",
        "date_create",
        "assigned_by_id",
        "comment_author_id",
        "comment_created",
        "comment_text",
        "deal_activity_count",
        "deal_activity_subjects",
        "deal_activity_ids",
    ]

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    logger.info("Exported %d deals to %s", len(rows), out_csv)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-id",
        default="24",
        help='SOURCE_ID code for "КЦ - 5%" (default: 24).',
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/export_sellers_kc5_comment_activity.csv"),
        help="Output CSV path.",
    )
    parser.add_argument(
        "--report-since",
        default="",
        help="Optional override for Settings.REPORT_SINCE (YYYY-MM-DD).",
    )
    args = parser.parse_args()

    if args.report_since:
        # Settings are read from env on initialization, so patch env first.
        os.environ["REPORT_SINCE"] = args.report_since

    setup_logging("INFO")
    logger.info(
        "Start export: sellers_category_id via Settings, source_id=%s, out=%s",
        args.source_id,
        args.out,
    )
    export(args.source_id, args.out)


if __name__ == "__main__":
    main()

