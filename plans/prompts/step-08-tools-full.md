# Промпт для Cursor — Полная реализация `src/tools.py` (Этап 2)

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).  
> **Внимание:** Этот промпт заменит текущий `src/tools.py` полностью.

---

Полностью перепиши файл `src/tools.py`. Убери заглушки и реализуй 5 реальных инструментов для работы с Битрикс24 через `fastbitrix24`.

## Контекст

- Проект: `b24-ai-auditor` (Python 3.11+, Windows)
- Пакет: `fast_bitrix24` (импорт: `from fast_bitrix24 import Bitrix`)
- Конфигурация: `from config import get_settings, Settings`
- Уже есть: `is_mutation_allowed(settings)` — проверяет `DRY_RUN`
- Все инструменты используют `@tool` декоратор из `langchain_core.tools`

## Требования к инструментам

### Инструмент 1: `get_deal_context`

```python
@tool
def get_deal_context(deal_id: int) -> dict[str, Any]:
    """Получить контекст сделки: поля + комментарии из таймлайна.

    Использует:
      - crm.deal.get(id=deal_id)
      - crm.timeline.comment.list(filter={"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal"})

    Returns:
        {
            "deal_id": int,
            "title": str,
            "stage_id": str,
            "opportunity": float,
            "assigned_by_id": int,
            "date_create": str,
            "source_id": str,
            "comments": [
                {"author_id": int, "comment": str, "created": str},
                ...
            ]
        }
    """
```

**Детали реализации:**
- Создай экземпляр `Bitrix` внутри функции: `bx = Bitrix(get_settings().b24_webhook_url)`
- Для `crm.deal.get` — используй `bx.call('crm.deal.get', {'id': deal_id})`
- Для `crm.timeline.comment.list` — используй `bx.call('crm.timeline.comment.list', {'filter': {'ENTITY_ID': deal_id, 'ENTITY_TYPE': 'deal'}, 'select': ['ID', 'AUTHOR_ID', 'COMMENT', 'CREATED']})`
- Обработай оба ответа и собери в итоговый словарь
- При ошибке API — залогируй и верни `{"error": str(exc), "deal_id": deal_id}`
- Импорты: `from fast_bitrix24 import Bitrix`, `from config import get_settings`

### Инструмент 2: `check_calls`

```python
@tool
def check_calls(user_id: int, hours_ago: int = 24) -> dict[str, Any]:
    """Проверить исходящие звонки менеджера за период.

    Использует: voximplant.statistic.get

    Args:
        user_id: ID менеджера в Битрикс24.
        hours_ago: Проверить звонки за последние N часов (по умолчанию 24).

    Returns:
        {
            "user_id": int,
            "period_hours": int,
            "total_calls": int,
            "successful_calls": int,
            "calls": [
                {"call_id": str, "duration": int, "start_date": str, "status": str},
                ...
            ]
        }
    """
```

**Детали реализации:**
- Вычисли `date_from` и `date_to` как ISO-строки относительно текущего времени
- `bx.call('voximplant.statistic.get', {'filter': {'CALL_TYPE': '1', 'CRM_ENTITY_TYPE': 'DEAL', 'PORTAL_USER_ID': user_id, 'CALL_DURATION': 30}, 'sort': 'CALL_START_DATE', 'order': 'DESC'})`
- Отфильтруй успешные звонки: `CALL_FAILED_CODE == 200` и длительность > 30 секунд
- Обработай возможные ошибки API (метод может быть недоступен на некоторых тарифах)

### Инструмент 3: `check_lead_qualification`

```python
@tool
def check_lead_qualification(max_hours: int = 1) -> dict[str, Any]:
    """Проверить время квалификации необработанных лидов.

    Использует: crm.lead.list

    Args:
        max_hours: Максимально допустимое время (часы) до квалификации.

    Returns:
        {
            "unprocessed_leads": int,
            "violations": [
                {"lead_id": int, "title": str, "hours_since_creation": float, "status": str},
                ...
            ]
        }
    """
```

**Детали реализации:**
- `bx.call('crm.lead.list', {'filter': {'STATUS_ID': 'NEW'}, 'select': ['ID', 'TITLE', 'DATE_CREATE', 'STATUS_ID', 'ASSIGNED_BY_ID']})`
- Для каждого лида вычисли `delta = datetime.now(UTC) - date_create`
- Если `delta.total_seconds() / 3600 > max_hours` — это нарушение
- Верни список нарушений

### Инструмент 4: `create_violation_task`

```python
@tool
def create_violation_task(user_id: int, deal_id: int, description: str) -> dict[str, Any]:
    """Поставить задачу брокеру-нарушителю.

    Использует: tasks.task.add

    Args:
        user_id: ID брокера-нарушителя (ответственный).
        deal_id: ID связанной сделки.
        description: Описание нарушения.

    Returns:
        {"task_id": int, "status": "created"} или {"status": "dry_run_skipped"}
    """
```

**Детали реализации:**
- Проверить `is_mutation_allowed(get_settings())` — если DRY_RUN, вернуть `{"status": "dry_run_skipped", "would_create_for": user_id}`
- `bx.call('tasks.task.add', {'fields': {'TITLE': f'Нарушение регламента по сделке #{deal_id}', 'DESCRIPTION': description, 'RESPONSIBLE_ID': user_id, 'UF_CRM_TASK': [f'D_{deal_id}']}})`
- Залогировать создание задачи

### Инструмент 5: `send_management_report`

```python
@tool
def send_management_report(chat_id: str, report_text: str) -> dict[str, Any]:
    """Отправить сводку нарушений в чат руководителей.

    Использует: im.notify.system.add

    Args:
        chat_id: ID чата в Битрикс24 (формат: "chat12345").
        report_text: Текст сводки.

    Returns:
        {"status": "sent", "chat_id": str} или {"status": "dry_run_skipped"}
    """
```

**Детали реализации:**
- Проверить `is_mutation_allowed(get_settings())`
- `bx.call('im.notify.system.add', {'CHAT_ID': chat_id, 'MESSAGE': report_text})`
- Обрезать сообщение до 2000 символов (лимит Битрикс24)

## Общие требования

1. **Кодировка:** UTF-8, переносы: LF
2. **Импорты** в начале файла:
   ```python
   """Bitrix24 REST tools via fast_bitrix24."""
   
   import logging
   from datetime import datetime, timedelta, timezone
   from typing import Any
   
   from fast_bitrix24 import Bitrix
   from langchain_core.tools import tool
   
   from config import get_settings, Settings
   ```
3. **is_mutation_allowed** — оставить без изменений
4. **Docstrings** — Google style для каждой функции
5. **Логирование:** `logger = logging.getLogger(__name__)`
6. **Обработка ошибок:** каждая функция должна ловить исключения от API и возвращать `{"error": str(exc)}`
7. **Таймауты:** не нужно явно задавать (fastbitrix24 сам управляет)
8. После создания файла запусти `.\make.cmd lint` и убедись, что flake8 проходит
