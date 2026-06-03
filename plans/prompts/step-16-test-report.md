# Промпт для Cursor — Этап 11: тестовый отчёт пользователю #154

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Создаёт: `send_report.py`. Запускает audit → отправляет violations пользователю #154.

---

## Задача: Создать и запустить `send_report.py`

Создай файл `send_report.py` в корне проекта:

```python
"""Run audit and send violations report to Bitrix24 user via im.notify."""
import asyncio
import json
import sys
from pathlib import Path

# Ensure src/ is on sys.path
_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from fast_bitrix24 import Bitrix  # noqa: E402
from config import get_settings, setup_logging  # noqa: E402
from graph import run_audit  # noqa: E402


def format_violations(violations: list[dict]) -> str:
    """Format violations list into a readable report."""
    if not violations:
        return "Нарушений регламента не найдено."

    lines = [f"Найдено нарушений: {len(violations)}", ""]

    high = [v for v in violations if v.get("severity") == "high"]
    medium = [v for v in violations if v.get("severity") == "medium"]
    low = [v for v in violations if v.get("severity") == "low"]

    lines.append(f"Критические (high): {len(high)}")
    lines.append(f"Средние (medium): {len(medium)}")
    lines.append(f"Низкие (low): {len(low)}")
    lines.append("")

    for v in violations:
        sev = v.get("severity", "?")
        icon = {"high": "[HIGH]", "medium": "[MED]", "low": "[LOW]"}.get(sev, "[?]")
        rule = v.get("rule", "?")
        desc = v.get("description", "—")
        lines.append(f"{icon} {rule}: {desc}")

    return "\n".join(lines)


def send_to_user(user_id: int, message: str) -> None:
    """Send message to Bitrix24 user via im.notify."""
    settings = get_settings()
    bx = Bitrix(settings.b24_webhook_url)

    # Truncate to 2000 chars (Bitrix24 limit)
    msg = message[:2000]
    if len(message) > 2000:
        msg = msg[:1990] + "...[обрезано]"

    result = bx.call(
        "im.notify",
        {
            "to": user_id,
            "message": msg,
            "type": "SYSTEM",
        },
    )
    print(f"Sent to user #{user_id}: {result}")


async def main() -> None:
    """Run audit and send report to user #154."""
    settings = get_settings()
    setup_logging(settings.log_level)

    print("Running audit (DRY_RUN mode — no mutations)...")
    result = await run_audit(settings, deal_ids=[])

    violations = result.get("violations", [])
    status = result.get("status", "?")
    messages = result.get("messages", [])

    print(f"Status: {status}")
    print(f"Steps: {len(messages)}")
    print(f"Violations: {len(violations)}")

    report = format_violations(violations)

    header = "b24-ai-auditor — тестовый отчёт\n\n"
    full_message = header + report

    print("\n=== REPORT ===")
    print(full_message)
    print("==============")

    send_to_user(154, full_message)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Проверка

После создания файла запусти в терминале:
```powershell
.\venv\Scripts\python send_report.py
```

**Ожидаемый результат:**
- Auditor соберёт данные (DRY_RUN=true — без мутаций)
- Analyst найдёт нарушения
- Скрипт сформатирует отчёт
- Отправит отчёт пользователю #154 через `im.notify`
- В консоли отобразится полный текст отчёта
