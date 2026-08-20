"""Notify the admin when a scheduled job fails.

Cron writes to logs/cron.log and nothing else, so a crashed audit is invisible
until somebody notices the report never arrived. Called by scripts/cron_job.sh.

Usage: python src/job_alert.py <job-name> <exit-code> [log-tail]
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
from notify import send_user_chat_message_chunked  # noqa: E402

logger = logging.getLogger(__name__)

TAIL_LIMIT = 1200


def build_alert(job: str, exit_code: int, tail: str, when: datetime) -> str:
    """Compose the failure message sent to the admin."""
    lines = [
        f"🚨 Задача «{job}» завершилась с ошибкой (код {exit_code}).",
        f"Время: {when.strftime('%d.%m.%Y %H:%M UTC')}",
    ]
    text = (tail or "").strip()
    if text:
        lines.extend(["", "Последние строки вывода:", text[-TAIL_LIMIT:]])
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python src/job_alert.py <job-name> <exit-code> [log-tail]")
        return 2

    job = sys.argv[1]
    try:
        exit_code = int(sys.argv[2])
    except ValueError:
        exit_code = -1
    tail = sys.argv[3] if len(sys.argv) > 3 else ""

    settings = get_settings()
    setup_logging(settings.log_level)
    message = build_alert(job, exit_code, tail, datetime.now(timezone.utc))

    if settings.dry_run:
        logger.warning("DRY_RUN=true — alert not sent:\n%s", message)
        return 0

    recipient = int(settings.admin_user_id or 0)
    if recipient <= 0:
        logger.error("ADMIN_USER_ID is not set — cannot deliver failure alert")
        return 1
    try:
        send_user_chat_message_chunked(recipient, message)
        logger.info("Failure alert for '%s' sent to user %s", job, recipient)
    except Exception:
        logger.exception("Failed to deliver failure alert for '%s'", job)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
