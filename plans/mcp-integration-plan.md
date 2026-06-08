# План интеграции MCP-сервера Bitrix24 в b24-ai-auditor

## Что выяснили

**MCP-сервер** `https://mcp-dev.bitrix24.tech/mcp` — это **документационный сервер** (не прокси для вызовов API):
- Предоставляет AI-ассистентам актуальные описания методов REST API Bitrix24
- Доступен **без авторизации**
- Протокол: **MCP (JSON-RPC 2.0)** через HTTP
- Не выполняет сами вызовы REST API — только отдаёт документацию

## Текущие вызовы API в проекте

| Файл | Метод API | Назначение |
|---|---|---|
| `tools.py:441` | `crm.deal.get` | Получить карточку сделки |
| `tools.py:539` | `crm.lead.list` | Получить все лиды |
| `tools.py:826` | `crm.deal.list` | Получить сделки воронки |
| `tools.py:211` | `voximplant.statistic.get` | Статистика звонков |
| `tools.py:272` | `crm.activity.list` | Активности (звонки) |
| `tools.py:421` | `crm.timeline.comment.list` | Комментарии таймлайна |
| `tools.py:367` | `crm.status.list` | Статусы лидов |
| `tools.py:598` | `tasks.task.add` | Создать задачу |
| `tools.py:650` | `im.notify.system.add` | Системное уведомление |
| `notify.py:63` | `im.notify.personal.add` | Персональное уведомление |
| `notify.py:82` | `im.message.add` | Сообщение в чат |
| `graph.py:453` | `user.get` | Информация о пользователе |
| `graph.py:460` | `user.search` | Поиск пользователя |
| `graph.py:481` | `department.get` | Информация об отделе |

## Рекомендуемая стратегия: ГИБРИД

### 1. Для разработки (Cursor)
Подключить MCP-сервер к Cursor через `mcp.json`:
```json
{
  "mcpServers": {
    "b24-dev-mcp": {
      "url": "https://mcp-dev.bitrix24.tech/mcp",
      "timeout": 30000
    }
  }
}
```
**Польза**: При генерации/рефакторинге кода в `tools.py`, `notify.py` Cursor будет использовать актуальные сигнатуры методов.

### 2. В рантайме: MCP-клиент как вспомогательный инструмент
Добавить новый `@tool` в `tools.py`, который агенты LangGraph могут вызывать:
```python
@tool
def get_api_method_info(method_name: str) -> dict:
    """Получить документацию по методу REST API Bitrix24 через MCP."""
    # Запрос к MCP-серверу: tools/call с методом resource/read
    # Возвращает описание параметров метода
```

### 3. Авто-генерация инструментов (опционально)
При старте приложения получать `tools/list` из MCP → генерировать `@tool` функции для часто используемых методов. Но для текущего стабильного набора из 14 методов это избыточно.

## Шаги для реализации

1. **Подключить MCP к Cursor** — добавить `mcp.json` в проект
2. **Создать тестовый скрипт** `test_mcp_server.py` — опросить сервер и сохранить ответы
3. **Проанализировать ответ** — понять структуру данных MCP
4. **Добавить MCP-клиент** в `tools.py` как дополнительный инструмент для агентов
5. **Обновить промпты аналитиков** — научить агентов использовать MCP-инструмент

## Промпт для Cursor (тестирование MCP-сервера)

Скопируй текст ниже в чат Cursor:

---

**Задача**: Создай скрипт `test_mcp_server.py` в корне проекта для тестирования Bitrix24 MCP-сервера `https://mcp-dev.bitrix24.tech/mcp`.

**Детали реализации**:
1. Используй только стандартную библиотеку Python (`urllib.request`, `json`)
2. Отправляй JSON-RPC 2.0 POST-запросы с заголовками `Content-Type: application/json` и `Accept: application/json, text/event-stream`
3. Выполни последовательно:
   - `initialize` с параметрами: `protocolVersion: "2024-11-05"`, `capabilities: {}`, `clientInfo: {name: "b24-ai-auditor-test", version: "1.0.0"}`
   - `tools/list` — сохрани результат в `mcp_tools_list.json`
   - `resources/list` — выведи первые 5 ресурсов
   - `prompts/list` — выведи количество промптов
4. Для каждого ответа выведи: HTTP статус, Content-Type, первые 500 символов тела
5. Если сервер отвечает SSE (`text/event-stream`), выведи полное тело
6. Обработай ошибки (HTTPError, таймаут 30с)

**После создания скрипта**:
7. Запусти его: `python test_mcp_server.py`
8. Покажи полный вывод
9. Покажи содержимое `mcp_tools_list.json` (первые 200 строк)

---

## Ожидаемый результат

После выполнения промпта Cursor должен:
1. Создать и запустить `test_mcp_server.py`
2. Показать, какие инструменты/tools/resources/prompts предоставляет MCP-сервер
3. Сохранить `mcp_tools_list.json` с полным списком методов

На основе этих данных мы сможем принять решение:
- Использовать ли MCP для авто-генерации `@tool` функций
- Или ограничиться подключением MCP к Cursor для помощи в разработке
- Или добавить MCP как дополнительный инструмент для агентов LangGraph

## Итоговая архитектура (предварительно)

```mermaid
flowchart TD
    subgraph "Разработка"
        C[Cursor IDE] -->|mcp.json| M[MCP Server]
        M -->|tools/list| D[Документация API]
    end
    
    subgraph "Рантайм"
        A[b24-ai-auditor] --> B[fast_bitrix24]
        B --> R[Bitrix24 REST API]
        A -->|опционально| M2[MCP Client Tool]
        M2 --> M
    end
    
    subgraph "Агенты LangGraph"
        L[Collectors] --> T[@tool функции]
        T --> B
        AN[Analysts/LLM] -->|могут вызвать| M2
    end
```

## Результаты теста (выполнено)

`python test_mcp_server.py` — сервер доступен, ответы в **SSE** (`data: {json}`).

| Что | Результат |
|-----|-----------|
| Сервер | `b24-dev-mcp` v0.2.0 |
| Tools | 5: `bitrix-search`, `bitrix-method-details`, `bitrix-event-details`, `bitrix-article-details`, `bitrix-app-development-doc-details` |
| Resources | 0 |
| Prompts | 0 |

Файлы в репо: `mcp.json.example`, `test_mcp_server.py` (с парсером SSE).

**Шаги 1–3:** готово. **Шаги 4–5 (рантайм в LangGraph):** не требуются для стабильного v2.

## Ключевой вывод

**MCP-сервер Bitrix24 — документационный, а не прокси**. Он помогает AI понимать API, но не выполняет вызовы. Поэтому:

1. **Прямые REST-вызовы через `fast_bitrix24` остаются основным транспортом**
2. **MCP используется как дополнительный источник знаний** для AI-агентов
3. **Наибольшая польза MCP — на этапе разработки** (Cursor/Claude генерируют корректный код)

## Аудит API: результаты (выполнено)

**Отчёт Cursor**: 10✅, 4⚠️, 1❌

| Статус | Метод | Детали |
|---|---|---|
| ✅ | `crm.deal.get`, `crm.deal.list`, `crm.lead.list`, `crm.activity.list`, `crm.timeline.comment.list`, `crm.status.list`, `im.notify.personal.add`, `department.get`, `tasks.task.add` | Параметры корректны |
| ❌ | `im.notify.system.add` | `CHAT_ID` вместо `USER_ID` — мертвый код, v2 dispatcher не использует |
| ⚠️ | `im.message.add` | `CHAT_ID` → должно быть `DIALOG_ID: "chat{id}"` |
| ⚠️ | `voximplant.statistic.get` | lowercase `filter/sort/order` → uppercase `FILTER/SORT/ORDER` |
| ⚠️ | `user.get` | `{"ID": uid}` → `{"filter": {"ID": uid}}` |
| ⚠️ | `user.search` | `FILTER.ID` не документирован |

**Решение**: исправить только безопасное (❌ + ⚠️ `im.message.add`). Остальное не трогать — работает в проде.

---

## Промпт для Cursor: исправление ❌ и ⚠️ (безопасные правки)

Скопируй текст ниже в чат Cursor:

---

**Задача**: Исправить два расхождения с документацией Bitrix24 REST API, найденные через MCP-аудит.

### Правка 1 (❌): Удалить `send_management_report` из `src/tools.py`

Функция `send_management_report` (строки 623–659) использует `im.notify.system.add` с `CHAT_ID` вместо документированного `USER_ID`. Этот `@tool` не вызывается в v2-диспетчере — отчёты уходят через `send_chat_message` → `im.message.add` из `src/notify.py`.

**Действия:**
1. Удали ВЕСЬ блок `@tool send_management_report` (от строки 623 `@tool` до строки 659 `return {"error": ...}` включительно)
2. Убедись, что после удаления нет «висящих» пустых строк (оставь одну пустую строку между соседними функциями)
3. Проверь: `send_management_report` нигде не импортируется (в `graph.py`, `main.py` нет импорта)

### Правка 2 (⚠️): `CHAT_ID` → `DIALOG_ID` в `src/notify.py`

В функции `send_chat_message` (строка 82–84) параметр `CHAT_ID` не соответствует документации — MCP требует `DIALOG_ID` в формате `chat{id}`.

**Текущий код (строка 82–84):**
```python
result = _bx_call_sync(
    "im.message.add",
    {"CHAT_ID": chat_id, "MESSAGE": message},
)
```

**Замени на:**
```python
result = _bx_call_sync(
    "im.message.add",
    {"DIALOG_ID": f"chat{chat_id}", "MESSAGE": message},
)
```

Функция `send_chat_message_chunked` (строка 91) вызывает `send_chat_message` — её менять не нужно, изменение подхватится автоматически.

**После правок:**
3. Проверь, что `python -c "from src.notify import send_chat_message; print('OK')"` работает
4. Убедись, что нигде нет обращений к удалённому `send_management_report`

---

## Промпт для Cursor: аудит вызовов API через MCP

Скопируй текст ниже в чат Cursor (убедись, что MCP-сервер подключён через `mcp.json.example`):

---

**Задача**: Проверь корректность всех вызовов Bitrix24 REST API в проекте, сверяя их с официальной документацией через MCP `bitrix-method-details`.

**Шаг 1 — Собери все вызовы API из кода**

Прочитай файлы `src/tools.py`, `src/notify.py`, `src/graph.py`. Найди ВСЕ вызовы методов Bitrix24 REST API. Для каждого вызова зафиксируй:
- Название метода (строка, первый аргумент `bx.call()` / `bx.get_all()` / `_bx_call_sync()` / `_bx_get_all_sync()`)
- Передаваемые параметры (`filter`, `select`, `order`, `sort`, `fields`, `id`, `USER_ID`, `MESSAGE`, `CHAT_ID`, `ENTITY_ID`, `ENTITY_TYPE` и т.д.)
- Файл и номер строки

**Вот список методов, которые точно используются — проверь каждый:**

| Метод | Где используется |
|---|---|
| `crm.deal.get` | `tools.py:457` — `bx.call("crm.deal.get", {"id": deal_id})` |
| `crm.deal.list` | `tools.py:677` — `bx.get_all("crm.deal.list", {filter, select})` |
| `crm.deal.list` | `tools.py:825` — `_bx_get_all_sync("crm.deal.list", {filter, select})` |
| `crm.lead.list` | `tools.py:539` — `bx.get_all("crm.lead.list", {filter, select})` |
| `crm.lead.list` | `tools.py:746` — `_bx_get_all_sync("crm.lead.list", {select})` |
| `crm.activity.list` | `tools.py:272` — `_bx_get_all_sync("crm.activity.list", {filter, select})` |
| `crm.timeline.comment.list` | `tools.py:421` — `bx.get_all("crm.timeline.comment.list", {filter, select})` |
| `crm.timeline.comment.list` | `tools.py:462` — `bx.call("crm.timeline.comment.list", {filter, select})` |
| `crm.status.list` | `tools.py:367` — `_bx_get_all_sync("crm.status.list", {filter})` |
| `voximplant.statistic.get` | `tools.py:221` — `bx.call("voximplant.statistic.get", {filter, sort, order})` |
| `tasks.task.add` | `tools.py:598` — `bx.call("tasks.task.add", {fields})` |
| `im.notify.system.add` | `tools.py:651` — `bx.call("im.notify.system.add", {CHAT_ID, MESSAGE})` |
| `im.notify.personal.add` | `notify.py:63` — `_bx_call_sync("im.notify.personal.add", {USER_ID, MESSAGE})` |
| `im.message.add` | `notify.py:82` — `_bx_call_sync("im.message.add", {CHAT_ID, MESSAGE})` |
| `user.get` | `graph.py:453` — `_bx_call_sync("user.get", {ID: uid})` |
| `user.search` | `graph.py:460` — `_bx_call_sync("user.search", {FILTER: {ID: uid}})` |
| `department.get` | `graph.py:481` — `_bx_call_sync("department.get", {ID: dept_id})` |

**Шаг 2 — Запроси документацию через MCP**

Для КАЖДОГО уникального метода (14 методов) используй MCP-инструмент `bitrix-method-details`, чтобы получить официальную документацию:

```
Используй MCP bitrix-method-details для метода crm.deal.get
Используй MCP bitrix-method-details для метода crm.deal.list
... и так далее для всех 14 методов
```

**Шаг 3 — Сравни и составь отчёт**

Для каждого вызова сравни фактические параметры в коде с документацией MCP. Отчёт должен быть в формате:

```
✅ crm.deal.get — ПАРАМЕТРЫ КОРРЕКТНЫ
⚠️ crm.deal.list (tools.py:677) — РАСХОЖДЕНИЕ: параметр X не документирован / отсутствует параметр Y
❌ voximplant.statistic.get — ОШИБКА: неверный параметр Z
```

Отметь:
- ✅ — полное совпадение с документацией
- ⚠️ — мелкие расхождения (лишние/нестандартные параметры, которые могут работать)
- ❌ — критические ошибки (неправильные имена параметров, отсутствующие обязательные поля)

**Шаг 4 — Дай рекомендации**

Для каждого ⚠️ и ❌ предложи исправление со ссылкой на документацию MCP.

---
