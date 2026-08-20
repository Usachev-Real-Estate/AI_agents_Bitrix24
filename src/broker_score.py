"""Broker scorecard: CRM rating breakdown over rolling period."""

import asyncio
import logging
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from fast_bitrix24 import Bitrix  # noqa: E402
from config import get_settings  # noqa: E402
from db import db_session, init_db  # noqa: E402
from broker_rating import (  # noqa: E402
    compute_all_ratings,
    compute_broker_rating,
    format_rating_scorecard,
    get_rating_period,
)
from broker_rating_collectors import (  # noqa: E402
    fetch_all_broker_tasks,
    fetch_important_feed_posts,
    list_eligible_brokers_for_rating,
)

logger = logging.getLogger(__name__)


def get_broker_info(broker_id: int) -> dict | None:
    """Имя и отдел из таблицы brokers."""
    try:
        with db_session() as conn:
            row = conn.execute(
                "SELECT responsible_id, responsible_name, department, department_id, "
                "lead_count, deal_count "
                "FROM brokers WHERE responsible_id = ?",
                (broker_id,),
            ).fetchone()
            if row:
                return {
                    "id": row[0],
                    "responsible_id": row[0],
                    "name": row[1] or f"ID: {row[0]}",
                    "responsible_name": row[1] or f"ID: {row[0]}",
                    "department": row[2] or "Без отдела",
                    "department_id": row[3],
                    "lead_count": row[4],
                    "deal_count": row[5],
                }
    except Exception as e:
        logger.error("DB Error getting broker info: %s", e)
    return None


async def build_scorecard_for_broker(
    broker_id: int,
    bx: Bitrix,
    settings,
) -> str | None:
    """Build rating scorecard for a broker."""
    init_db()
    since_iso, until_iso, since_dt, until_dt = get_rating_period()

    broker = get_broker_info(broker_id)
    if not broker:
        try:
            users = await bx.get_all("user.get", {"FILTER": {"ID": broker_id}})
            if users:
                u = users[0]
                name = ((u.get("NAME") or "") + " " + (u.get("LAST_NAME") or "")).strip()
                name = name or f"ID: {broker_id}"
                broker = {
                    "responsible_id": broker_id,
                    "responsible_name": name,
                    "department": "API (отдел неизвестен)",
                    "department_id": None,
                }
            else:
                return None
        except Exception as e:
            logger.error("Error fetching user from API: %s", e)
            return None

    important_posts = fetch_important_feed_posts(since_iso, settings)
    all_brokers = list_eligible_brokers_for_rating(settings)
    broker_ids = {int(b["responsible_id"]) for b in all_brokers}
    broker_ids.add(broker_id)
    tasks_by_broker = fetch_all_broker_tasks(list(broker_ids))

    if broker_id not in {int(b["responsible_id"]) for b in all_brokers}:
        all_brokers = list(all_brokers) + [broker]

    all_ratings = compute_all_ratings(
        since_iso,
        until_iso,
        brokers=all_brokers,
        tasks_by_broker=tasks_by_broker,
        important_posts=important_posts,
        settings=settings,
    )

    rating = next((r for r in all_ratings if r.responsible_id == broker_id), None)
    if not rating:
        rating = compute_broker_rating(
            broker,
            since_iso,
            until_iso,
            tasks=tasks_by_broker.get(broker_id, []),
            important_posts=important_posts,
            settings=settings,
        )

    return format_rating_scorecard(rating, since_dt, until_dt)


async def async_main():
    if len(sys.argv) < 2:
        print("Usage: python src/broker_score.py <BROKER_ID>")
        sys.exit(1)

    try:
        broker_id = int(sys.argv[1])
    except ValueError:
        print("Error: BROKER_ID must be an integer")
        sys.exit(1)

    settings = get_settings()
    init_db()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    bx = Bitrix(settings.b24_webhook_url)
    report = await build_scorecard_for_broker(broker_id, bx, settings)
    if not report:
        error_msg = f"❌ Брокер с ID {broker_id} не найден ни в БД, ни в API"
        print(error_msg)
        if not settings.dry_run:
            from notify import send_chat_message_chunked
            send_chat_message_chunked(settings.report_chat_id, error_msg)
        return

    print("\n" + report + "\n")

    if str(settings.dry_run).lower() != "true" and settings.dry_run is not True:
        from notify import send_chat_message_chunked
        send_chat_message_chunked(settings.report_chat_id, report)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
