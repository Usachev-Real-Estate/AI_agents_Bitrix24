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

## Вопрос для обсуждения

Нужно ли вообще что-то менять в коде `b24-ai-auditor`? Или достаточно просто подключить MCP к Cursor для улучшения качества генерации кода при будущих доработках?
