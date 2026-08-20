#!/usr/bin/env python3
"""Export all seller-funnel deals with timeline comments to CSV."""

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

FIELDNAMES = [
    "deal_id",
    "deal_link",
    "title",
    "stage_id",
    "stage_name",
    "source_id",
    "source_name",
    "date_create",
    "assigned_by_id",
    "opportunity",
    "timeline_comment_count",
    "comment_author_id",
    "comment_created",
    "comment_text",
]


def _source_name(source_id: str) -> str:
    return SELLERS_PAID_SOURCE_NAMES.get(source_id, source_id or "")


def _timeline_rows(deal: dict[str, Any]) -> list[dict[str, Any]]:
    deal_id = int(deal.get("deal_id") or 0)
    source_id = str(deal.get("source_id") or "").strip()
    timeline: list[dict[str, Any]] = deal.get("timeline") or []
    if not isinstance(timeline, list):
        timeline = []

    comments = [
        item for item in timeline
        if isinstance(item, dict) and str(item.get("comment") or "").strip()
    ]
    try:
        comments.sort(key=lambda x: str(x.get("created") or ""))
    except Exception:
        pass

    base = {
        "deal_id": deal_id,
        "deal_link": _build_crm_link("deal", deal_id) if deal_id > 0 else "",
        "title": str(deal.get("title") or "").strip(),
        "stage_id": str(deal.get("stage_id") or "").strip(),
        "stage_name": str(deal.get("stage_name") or "").strip(),
        "source_id": source_id,
        "source_name": _source_name(source_id),
        "date_create": str(deal.get("date_create") or "").strip(),
        "assigned_by_id": str(deal.get("assigned_by_id") or "").strip(),
        "opportunity": str(deal.get("opportunity") or "").strip(),
        "timeline_comment_count": len(comments),
    }

    if not comments:
        return [{
            **base,
            "comment_author_id": "",
            "comment_created": "",
            "comment_text": "",
        }]

    rows: list[dict[str, Any]] = []
    for item in comments:
        rows.append({
            **base,
            "comment_author_id": str(item.get("author_id") or "").strip(),
            "comment_created": str(item.get("created") or "").strip(),
            "comment_text": str(item.get("comment") or "").strip(),
        })
    return rows


def export(out_csv: Path) -> tuple[int, int]:
    settings = get_settings()
    setup_logging(settings.log_level)

    payload = get_deals_by_funnel_with_timeline.invoke(
        {"category_id": settings.sellers_category_id},
    )
    deals: list[dict[str, Any]] = payload.get("deals") or []
    if not isinstance(deals, list):
        deals = []

    rows: list[dict[str, Any]] = []
    for deal in deals:
        if not isinstance(deal, dict):
            continue
        rows.extend(_timeline_rows(deal))

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(
        "Exported %d deals (%d CSV rows) to %s",
        len(deals),
        len(rows),
        out_csv,
    )
    return len(deals), len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export seller-funnel deals with timeline to CSV.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/export_sellers_deals_with_timeline.csv"),
        help="Output CSV path.",
    )
    parser.add_argument(
        "--report-since",
        default="",
        help="Override REPORT_SINCE (YYYY-MM-DD). Empty = no date filter.",
    )
    args = parser.parse_args()

    os.environ["REPORT_SINCE"] = args.report_since

    setup_logging("INFO")
    deals_count, row_count = export(args.out)
    print(f"deals={deals_count} rows={row_count} file={args.out}")


if __name__ == "__main__":
    main()
