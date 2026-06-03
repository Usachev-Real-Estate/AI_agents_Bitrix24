"""Test Bitrix24 connectivity via fast_bitrix24."""

import logging
import sys
from pathlib import Path
from typing import Any

# Ensure src/ is on sys.path
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from fast_bitrix24 import Bitrix  # noqa: E402
from config import get_settings, setup_logging  # noqa: E402
from tools import _as_list  # noqa: E402

logger = logging.getLogger(__name__)


def _format_deal(deal: dict[str, Any]) -> str:
    """Format a single deal record for console output.

    Args:
        deal: Deal dict from Bitrix24 API.

    Returns:
        Human-readable one-line summary.
    """
    return (
        f"ID={deal.get('ID')} | {deal.get('TITLE', '—')} | "
        f"stage={deal.get('STAGE_ID')} | sum={deal.get('OPPORTUNITY', '—')}"
    )


def _extract_tasks(payload: Any) -> list[dict[str, Any]]:
    """Normalize tasks.task.list response to a list of task dicts.

    Args:
        payload: Raw API response (list or nested dict).

    Returns:
        Up to a few task records as dicts.
    """
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    if not isinstance(payload, dict):
        return []
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        return [t for t in tasks if isinstance(t, dict)]
    if isinstance(tasks, dict):
        return [t for t in tasks.values() if isinstance(t, dict)]
    return []


def test_crm_deals(bx: Bitrix) -> bool:
    """Test: fetch 5 open deals. Returns True on success.

    Args:
        bx: Bitrix24 REST client.

    Returns:
        True if deals were retrieved successfully.
    """
    try:
        result = bx.call(
            "crm.deal.list",
            {
                "filter": {"CLOSED": "N"},
                "select": ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY"],
                "order": {"ID": "DESC"},
                "start": 0,
            },
        )
        deals: list[Any]
        if isinstance(result, list):
            deals = result
        elif isinstance(result, dict):
            inner = result.get("deals", result)
            deals = inner if isinstance(inner, list) else []
        else:
            deals = []

        deals = [d for d in deals[:5] if isinstance(d, dict)]
        if not deals:
            logger.warning("CRM: сделки не найдены (пустой список)")
            print("  CRM: открытых сделок нет или фильтр не вернул данных")
            return True

        print(f"  CRM: получено сделок: {len(deals)}")
        for deal in deals:
            print(f"    - {_format_deal(deal)}")
        logger.info("CRM check OK: %d deals", len(deals))
        return True
    except Exception as exc:
        logger.exception("CRM check failed")
        print(f"  CRM: ошибка — {exc}")
        return False


def test_tasks(bx: Bitrix) -> bool:
    """Test: fetch 3 tasks. Returns True on success.

    Args:
        bx: Bitrix24 REST client.

    Returns:
        True if tasks were retrieved successfully.
    """
    try:
        result = bx.call(
            "tasks.task.list",
            {
                "order": {"ID": "DESC"},
                "select": ["ID", "TITLE", "STATUS", "RESPONSIBLE_ID"],
                "start": 0,
            },
        )
        tasks = _extract_tasks(result)[:3]
        if not tasks:
            logger.warning("Tasks: задачи не найдены (пустой список)")
            print("  Tasks: задач нет или недостаточно прав")
            return True

        print(f"  Tasks: получено задач: {len(tasks)}")
        for task in tasks:
            print(
                f"    - ID={task.get('id') or task.get('ID')} | "
                f"{task.get('title') or task.get('TITLE', '—')} | "
                f"status={task.get('status') or task.get('STATUS')}"
            )
        logger.info("Tasks check OK: %d tasks", len(tasks))
        return True
    except Exception as exc:
        logger.exception("Tasks check failed")
        print(f"  Tasks: ошибка — {exc}")
        return False


def test_user(bx: Bitrix) -> bool:
    """Test: fetch current user info. Returns True on success.

    Args:
        bx: Bitrix24 REST client.

    Returns:
        True if current user data was retrieved.
    """
    try:
        user = bx.call("user.current", raw=True)
        if not isinstance(user, dict):
            logger.error("User: неожиданный формат ответа: %s", type(user))
            print(f"  User: неожиданный ответ ({type(user).__name__})")
            return False

        name = " ".join(
            part
            for part in (
                user.get("NAME"),
                user.get("LAST_NAME"),
            )
            if part
        ).strip() or "—"
        print(
            f"  User: ID={user.get('ID')} | {name} | "
            f"email={user.get('EMAIL', '—')}"
        )
        logger.info("User check OK: id=%s", user.get("ID"))
        return True
    except Exception as exc:
        logger.exception("User check failed")
        print(f"  User: ошибка — {exc}")
        return False


def test_activity_calls(bx: Bitrix) -> bool:
    """Test: crm.activity.list for CALL activities via webhook.

    Args:
        bx: Bitrix24 REST client.

    Returns:
        True if the method is callable (even with empty result).
    """
    try:
        result = bx.get_all(
            "crm.activity.list",
            {
                "filter": {
                    "PROVIDER_TYPE_ID": "CALL",
                    ">=CREATED": "2024-01-01 00:00:00",
                },
                "select": ["ID", "DIRECTION", "COMPLETED", "CREATED"],
            },
        )
        records = _as_list(result)
        print(
            f"  Activity (calls): {type(result).__name__}, records: {len(records)}",
        )
        logger.info("Activity calls check OK: %d records", len(records))
        return True
    except Exception as exc:
        logger.exception("Activity calls check failed")
        print(f"  Activity (calls): ошибка — {exc}")
        return False


def main() -> None:
    """Run all connectivity tests."""
    settings = get_settings()
    setup_logging(settings.log_level)

    bx = Bitrix(settings.b24_webhook_url)

    logger.info("Testing Bitrix24 connectivity...")
    webhook_preview = settings.b24_webhook_url[:50]
    logger.info("Webhook: %s...", webhook_preview)

    results = {
        "CRM (deals)": test_crm_deals(bx),
        "Tasks": test_tasks(bx),
        "User": test_user(bx),
        "Activity (calls)": test_activity_calls(bx),
    }

    print("\n" + "=" * 50)
    print("РЕЗУЛЬТАТЫ ПРОВЕРКИ:")
    for name, ok in results.items():
        status = "OK" if ok else "FAIL"
        print(f"  {name}: {status}")
    print("=" * 50)

    all_ok = all(results.values())
    if not all_ok:
        print("\n[FAIL] Некоторые проверки не пройдены.")
        print("Проверьте:")
        print("  1. B24_WEBHOOK_URL в .env")
        print("  2. Права вебхука: crm, tasks, user")
        print("  3. Доступность Битрикс24")
        sys.exit(1)
    else:
        print("\n[OK] Все проверки пройдены. Подключение работает.")


if __name__ == "__main__":
    main()
