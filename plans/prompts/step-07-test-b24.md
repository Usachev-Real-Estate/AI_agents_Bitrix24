# Промпт для Cursor — Создание `src/test_b24.py` (Этап 1)

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).

---

Создай файл `src/test_b24.py` — скрипт для проверки подключения к Битрикс24 через fastbitrix24.

## Контекст

Проект `b24-ai-auditor`, Python 3.11+, Windows. Уже есть:
- `src/config.py` — `Settings` из pydantic-settings, поля: `b24_webhook_url`, `b24_user_id`, `dry_run`, `log_level`
- `.env` — переменные окружения (B24_WEBHOOK_URL, B24_USER_ID, etc.)
- `requirements.txt` — `fast-bitrix24>=1.0.0` (импорт: `from fast_bitrix24 import Bitrix`)

## Требования к `src/test_b24.py`

Скрипт должен:

1. Импортировать `get_settings`, `setup_logging` из `config`
2. Загрузить настройки
3. Создать клиент `Bitrix` с webhook URL
4. Выполнить ТРИ проверки:
   - **Проверка 1 (crm):** Получить 5 последних сделок через `crm.deal.list` с фильтром `filter={"CLOSED": "N"}` и параметрами `select=["ID", "TITLE", "STAGE_ID", "OPPORTUNITY"]`, `order={"ID": "DESC"}`, `limit=5`
   - **Проверка 2 (tasks):** Получить список задач через `tasks.task.list` с `limit=3`
   - **Проверка 3 (user):** Получить данные текущего пользователя через `user.current`
5. Для каждой проверки вывести результат в читаемом виде или сообщение об ошибке
6. В конце вывести сводку: OK/FALL для каждой проверки
7. При ошибках — понятное сообщение с предложением проверить `.env` и права вебхука

## Структура скрипта

```python
"""Test Bitrix24 connectivity via fastbitrix24."""

import logging
import sys
from pathlib import Path

# Ensure src/ is on sys.path
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from fast_bitrix24 import Bitrix
from config import get_settings, setup_logging

logger = logging.getLogger(__name__)


def test_crm_deals(bx: Bitrix) -> bool:
    """Test: fetch 5 open deals. Returns True on success."""
    ...


def test_tasks(bx: Bitrix) -> bool:
    """Test: fetch 3 tasks. Returns True on success."""
    ...


def test_user(bx: Bitrix) -> bool:
    """Test: fetch current user info. Returns True on success."""
    ...


def main() -> None:
    """Run all connectivity tests."""
    settings = get_settings()
    setup_logging(settings.log_level)
    
    bx = Bitrix(settings.b24_webhook_url)
    
    logger.info("Testing Bitrix24 connectivity...")
    logger.info("Webhook: %s...", settings.b24_webhook_url[:50])
    
    results = {
        "CRM (deals)": test_crm_deals(bx),
        "Tasks": test_tasks(bx),
        "User": test_user(bx),
    }
    
    print("\n" + "=" * 50)
    print("РЕЗУЛЬТАТЫ ПРОВЕРКИ:")
    for name, ok in results.items():
        status = "✅ OK" if ok else "❌ FAIL"
        print(f"  {name}: {status}")
    print("=" * 50)
    
    all_ok = all(results.values())
    if not all_ok:
        print("\n❌ Некоторые проверки не пройдены.")
        print("Проверьте:")
        print("  1. B24_WEBHOOK_URL в .env")
        print("  2. Права вебхука: crm, tasks, user")
        print("  3. Доступность Битрикс24")
        sys.exit(1)
    else:
        print("\n✅ Все проверки пройдены. Подключение работает.")


if __name__ == "__main__":
    main()
```

**Важно:**
- Импорт: `from fast_bitrix24 import Bitrix` (пакет `fast-bitrix24` в requirements.txt)
- Реализуй все три функции полностью (не заглушки)
- Используй реальные вызовы API через `bx.call()`
- Обрабатывай исключения в каждой функции
- Кодировка: UTF-8, переносы: LF
