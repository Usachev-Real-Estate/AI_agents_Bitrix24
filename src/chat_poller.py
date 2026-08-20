"""Chat command poller: reads chat 22358 for broker score requests."""

import asyncio
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure src/ is on sys.path when running as script
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from fast_bitrix24 import Bitrix  # noqa: E402
from config import get_settings  # noqa: E402
from notify import send_chat_message_chunked  # noqa: E402
from broker_score import build_scorecard_for_broker  # noqa: E402

logger = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────
COMMAND_PATTERN = re.compile(
    r"(?:провер(?:ь|ка)|!score|/score)\s+(\d+)",
    re.IGNORECASE,
)
COMMAND_WEEKLY = re.compile(
    r"^(?:/weekly|!weekly|!неделя|недельный отчёт)$",
    re.IGNORECASE,
)
COMMAND_DEPT = re.compile(
    r"(?:/dept|!dept|!отдел)\s+(.+)",
    re.IGNORECASE,
)
COMMAND_RATING = re.compile(
    r"^(?:/rating|!rating|рейтинг)(?:\s+(.+))?$",
    re.IGNORECASE,
)
LAST_ID_FILE = Path("data/chat_last_id.txt")


# ── Хелперы ────────────────────────────────────────────
def get_last_processed_id() -> int:
    try:
        return int(LAST_ID_FILE.read_text().strip())
    except Exception:
        return 0


def set_last_processed_id(msg_id: int) -> None:
    LAST_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_ID_FILE.write_text(str(msg_id))


# ── Чат ────────────────────────────────────────────────
async def fetch_new_messages(bx: Bitrix, chat_id: int, since_id: int) -> list[dict]:
    """Получить новые сообщения из чата."""
    try:
        # im.dialog.messages.get return structure:
        # {"messages": [...]}
        # We need to fetch messages and filter by ID > since_id
        response = await bx.call("im.dialog.messages.get", {
            "DIALOG_ID": f"chat{chat_id}",
            "LIMIT": 50,
        })

        # fast_bitrix24 might wrap the result in order0000000000
        if isinstance(response, dict) and "order0000000000" in response:
            response = response["order0000000000"]

        messages = response.get("messages", []) if isinstance(response, dict) else []
        if not messages and isinstance(response, list):
            # Sometimes fast_bitrix24 unwraps the list directly
            messages = response

        new_messages = [m for m in messages if int(m.get("id", 0)) > since_id]
        # Sort by ID ascending
        new_messages.sort(key=lambda m: int(m.get("id", 0)))
        return new_messages
    except Exception as e:
        logger.error(f"Error fetching messages: {e}")
        return []


def find_command(messages: list[dict]) -> list[tuple[int, int]]:
    """Найти команды в сообщениях.
    Returns: [(broker_id, message_id), ...]"""
    commands = []
    for m in messages:
        text = m.get("text", "") or m.get("message", "")
        msg_id = int(m.get("id", 0))
        match = COMMAND_PATTERN.search(text)
        if match:
            broker_id = int(match.group(1))
            commands.append((broker_id, msg_id))
    return commands


# ── Главная ────────────────────────────────────────────
async def async_main():
    settings = get_settings()

    # Enable logging output to console
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    bx = Bitrix(settings.b24_webhook_url)

    chat_id = settings.report_chat_id
    last_id = get_last_processed_id()

    logger.info(f"Polling chat {chat_id} for commands since ID {last_id}")

    messages = await fetch_new_messages(bx, chat_id, last_id)
    if not messages:
        logger.info("No new messages.")
        return

    logger.info(f"Found {len(messages)} new messages.")

    commands = find_command(messages)
    if not commands:
        logger.info("No commands found in new messages.")

    for broker_id, msg_id in commands:
        logger.info(f"Processing command for broker {broker_id} from message {msg_id}")

        report = await build_scorecard_for_broker(broker_id, bx, settings)
        if not report:
            msg = f"❌ Брокер с ID {broker_id} не найден или произошла ошибка"
            if not settings.dry_run:
                send_chat_message_chunked(chat_id, msg)
            continue

        if not settings.dry_run:
            send_chat_message_chunked(chat_id, report)
        else:
            logger.info(f"DRY_RUN=True, skipping sending message. Report:\n{report}")

    # Check for /weekly command
    for m in messages:
        text = m.get("text", "") or m.get("message", "")
        if COMMAND_WEEKLY.search(text.strip()):
            logger.info("Weekly report requested from chat")
            from weekly_report import format_weekly_report, filter_weekly_scope
            from db import get_weekly_stats, init_db, purge_test_violations
            init_db()
            purge_test_violations()
            now = datetime.now(timezone.utc)
            week_start_dt = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            week_start = week_start_dt.isoformat()
            week_end = now.isoformat()
            violators, clean = get_weekly_stats(week_start, week_end, latest_only=True)
            violators, clean = filter_weekly_scope(violators, clean)

            prev_week_start = (week_start_dt - timedelta(days=7)).isoformat()
            prev_week_end = week_start_dt.isoformat()
            prev_violators, _ = get_weekly_stats(prev_week_start, prev_week_end, latest_only=True)
            prev_violators, _ = filter_weekly_scope(prev_violators, [])
            prev_total = sum(v['total_violations'] for v in prev_violators)

            report = format_weekly_report(
                violators, clean, week_start, now,
                prev_total_violations=prev_total,
            )
            if not settings.dry_run:
                send_chat_message_chunked(chat_id, report)

    # Check for /dept command
    for m in messages:
        text = m.get("text", "") or m.get("message", "")
        match = COMMAND_DEPT.search(text.strip())
        if match:
            dept_query = match.group(1).strip()
            logger.info("Department report requested: %s", dept_query)
            from weekly_report import format_weekly_report, filter_weekly_scope
            from db import get_weekly_stats, init_db, purge_test_violations
            init_db()
            purge_test_violations()
            now = datetime.now(timezone.utc)
            week_start_dt = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            week_start = week_start_dt.isoformat()
            week_end = now.isoformat()
            violators, clean = get_weekly_stats(week_start, week_end, latest_only=True)
            violators, clean = filter_weekly_scope(violators, clean)

            # Filter by department name (fuzzy match)
            dept_violators = [
                v for v in violators
                if dept_query.lower() in (v.get("department") or "").lower()
            ]
            dept_clean = [
                c for c in clean
                if dept_query.lower() in (c.get("department") or "").lower()
            ]

            if not dept_violators and not dept_clean:
                msg = f"❌ Отдел '{dept_query}' не найден или нет данных"
                if not settings.dry_run:
                    send_chat_message_chunked(chat_id, msg)
                continue

            dept_name = dept_violators[0]["department"] if dept_violators else (
                dept_clean[0]["department"] if dept_clean else dept_query
            )

            prev_week_start = (week_start_dt - timedelta(days=7)).isoformat()
            prev_week_end = week_start_dt.isoformat()
            prev_violators, _ = get_weekly_stats(prev_week_start, prev_week_end, latest_only=True)
            prev_violators, _ = filter_weekly_scope(prev_violators, [])
            dept_prev_violators = [
                v for v in prev_violators
                if dept_query.lower() in (v.get("department") or "").lower()
            ]
            dept_prev_total = sum(v['total_violations'] for v in dept_prev_violators)

            report = format_weekly_report(
                dept_violators, dept_clean, week_start, now, dept_name=dept_name,
                prev_total_violations=dept_prev_total,
            )
            if not settings.dry_run:
                send_chat_message_chunked(chat_id, report)

    # Check for /rating command
    for m in messages:
        text = m.get("text", "") or m.get("message", "")
        match = COMMAND_RATING.search(text.strip())
        if match:
            dept_query = (match.group(1) or "").strip()
            logger.info("Rating report requested%s", f": {dept_query}" if dept_query else "")
            from weekly_report import build_rating_section
            from db import init_db
            init_db()
            if dept_query:
                rating_text, _ = build_rating_section(dept_name=dept_query)
            else:
                rating_text, _ = build_rating_section()
            if not settings.dry_run:
                send_chat_message_chunked(chat_id, rating_text)
            else:
                logger.info("DRY_RUN=True, rating report:\n%s", rating_text)

    # Обновить last_id до максимального обработанного
    if messages:
        max_id = max(int(m.get("id", 0)) for m in messages)
        if max_id > last_id:
            set_last_processed_id(max_id)
            logger.info(f"Updated last_id to {max_id}")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
