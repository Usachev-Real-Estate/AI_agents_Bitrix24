# Промпт для Cursor — Этап 10: боевой прогон + send_status.py

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Создаёт: `send_status.py`. Обновляет: `.env` (опционально).

---

## Задача 1: Боевой прогон (DRY_RUN=false)

Перед запуском убедись что в `.env`:
```ini
DRY_RUN=false
```

Затем выполни в терминале:
```powershell
.\make.cmd run
```

**Ожидаемый результат:**
- Auditor соберёт все активные сделки и лиды
- Analyst найдёт нарушения (если есть)
- Dispatcher **реально создаст задачи** нарушителям и **отправит отчёт** в MANAGEMENT_CHAT_ID (если задан)

⚠️ **Внимание:** это мутирующая операция! Будут созданы реальные задачи в Битрикс24.

---

## Задача 2: Создать `send_status.py` в корне проекта

Создай файл `send_status.py`:

```python
"""Send audit status summary to a specific Bitrix24 user via im.notify."""
import sys
from pathlib import Path

# Ensure src/ is on sys.path
_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from fast_bitrix24 import Bitrix  # noqa: E402
from config import get_settings, setup_logging  # noqa: E402

STATUS_MESSAGE = """\
b24-ai-auditor — статус проекта

Bitrix24 API: подключение работает
RouterAI (DeepSeek-v4-flash): 3/3 вызова 200 OK
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

    bx = Bitrix(settings.b24_webhook_url)

    result = bx.call(
        "im.notify",
        {
            "to": 154,
            "message": STATUS_MESSAGE,
            "type": "SYSTEM",
        },
    )
    print(f"Sent to user #154: {result}")


if __name__ == "__main__":
    main()
```

---

## Задача 3: Запустить send_status.py

В терминале:
```powershell
.\venv\Scripts\python send_status.py
```

Пользователь #154 получит уведомление в Битрикс24.
