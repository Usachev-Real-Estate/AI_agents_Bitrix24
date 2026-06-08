# Чеклист изменений MCP-интеграции для деплоя на сервер

## Дата: 2026-06-08

---

## Изменения в коде (обязательно применить на сервере)

### 1. `src/notify.py` — строка 82: `CHAT_ID` → `DIALOG_ID`

**Было:**
```python
result = _bx_call_sync(
    "im.message.add",
    {"CHAT_ID": chat_id, "MESSAGE": message},
)
```

**Стало:**
```python
result = _bx_call_sync(
    "im.message.add",
    {"DIALOG_ID": f"chat{chat_id}", "MESSAGE": message},
)
```

**Причина**: MCP-документация требует `DIALOG_ID` в формате `chat{id}`. Bitrix принимает оба, но для соответствия документации — исправлено.

---

### 2. `src/tools.py` — удалить функцию `send_management_report`

**Причина**: Использовала `im.notify.system.add` с `CHAT_ID` вместо `USER_ID` (не по документации). V2-диспетчер не вызывает этот tool — отчёты уходят через `send_chat_message` → `im.message.add`.

---

### 3. `mcp.json.example` — новый файл (опционально, только для разработки)

Подключение MCP-сервера Bitrix24 в Cursor для получения актуальной документации API при доработках.

---

### 4. `test_mcp_server.py` — новый файл (опционально, для тестов)

Скрипт для проверки доступности и состава MCP-сервера. На сервере не нужен.

---

### 5. `.gitignore` — дополнен

Добавлена строка: `mcp_tools_list.json`

---

## Проверка после деплоя

```bash
cd /path/to/b24-ai-auditor
git pull origin main

python -c "from src.notify import send_chat_message; print('notify OK')"
python -c "from src.tools import get_active_deals; print('tools OK')"
grep -r "send_management_report" src/    # должно быть пусто

DRY_RUN=true python -m src.main
```

## Что НЕ менялось (работает в проде)

- `voximplant.statistic.get` — lowercase ключи `filter/sort/order` (fast_bitrix24 нормализует)
- `user.get` — `{"ID": uid}` (работает, менять только после тестов на стенде)
- `user.search` — fallback, `FILTER.ID` не документирован, но срабатывает редко

## Итог

Критических изменений в рантайме нет. Оба изменения безопасны:
- `DIALOG_ID` — Bitrix принимает оба формата
- Удаление `send_management_report` — не используется в v2
