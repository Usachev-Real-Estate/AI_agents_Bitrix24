"""Send audit status summary to a specific Bitrix24 user via im.notify.personal.add."""

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
from notify import send_personal_notification  # noqa: E402

STATUS_MESSAGE = """\
b24-ai-auditor — статус проекта

Bitrix24 API: подключение работает
DeepSeek API (deepseek-v4-flash): подключение настроено
Auditor: сбор данных через get_all() (все страницы)
Analyst: поиск нарушений по 4 правилам
Dispatcher: создание задач + отчёт руководству
Логирование: logs/audit.log
Деплой: Docker + cron готов

MANAGEMENT_CHAT_ID: не задан (отчёты руководству не отправляются)

Проект готов к тестовой эксплуатации."""


def main() -> None:
    """Send status to user #154."""
    settings = get_settings()
    setup_logging(settings.log_level)

    notify_id = send_personal_notification(154, STATUS_MESSAGE)
    print(f"Sent to user #154, notification id={notify_id}")


if __name__ == "__main__":
    main()
