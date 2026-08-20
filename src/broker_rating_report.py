"""Publish broker CRM rating to REPORT_CHAT_ID (daily / on demand)."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from broker_rating import persist_ratings_snapshot  # noqa: E402
from config import get_settings, setup_logging  # noqa: E402
from db import init_db  # noqa: E402
from notify import send_chat_message_chunked  # noqa: E402
from weekly_report import build_rating_section  # noqa: E402

logger = logging.getLogger(__name__)


def run(*, send: bool = True, full_list: bool = True) -> tuple[str, list]:
    """Build rating, persist snapshot, optionally send to report chat."""
    init_db()
    settings = get_settings()
    text, ratings = build_rating_section(full_list=full_list)
    snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if ratings:
        persist_ratings_snapshot(ratings, snapshot_date)
        logger.info(
            "Rating snapshot saved: date=%s brokers=%s",
            snapshot_date,
            len(ratings),
        )

    if send:
        if settings.dry_run:
            logger.info("DRY_RUN=True — skip sending rating to chat")
        else:
            send_chat_message_chunked(settings.report_chat_id, text)
            logger.info("Rating sent to chat %s", settings.report_chat_id)
    return text, ratings


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish broker CRM rating")
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Only compute and save snapshot, do not send to chat",
    )
    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level)
    text, ratings = run(send=not args.no_send)
    print(text)
    print(f"---\nБрокеров: {len(ratings)}")


if __name__ == "__main__":
    main()
