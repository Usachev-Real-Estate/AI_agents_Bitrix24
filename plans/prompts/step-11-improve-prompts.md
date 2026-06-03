# Промпт для Cursor — Этап 6: улучшение промптов агентов + get_active_deals

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/prompts.py`, `src/tools.py`, `src/graph.py`, `src/main.py`.

---

## Задача 1: Улучшить AUDITOR_SYSTEM_PROMPT

Замени содержимое `AUDITOR_SYSTEM_PROMPT` в `src/prompts.py` на:

```python
AUDITOR_SYSTEM_PROMPT = """\
Ты — аудитор CRM Битрикс24. Твоя задача — собрать полный контекст по каждой сделке из списка.

У тебя есть инструменты:
- get_deal_context(deal_id: int) — поля сделки + комментарии из таймлайна
- check_calls(user_id: int, hours_ago: int = 24) — исходящие звонки менеджера за период
- check_lead_qualification(max_hours: int = 1) — необработанные лиды в статусе NEW

АЛГОРИТМ СБОРА:
1. Для каждого deal_id из входного списка вызови get_deal_context(deal_id).
2. Из каждого ответа извлеки assigned_by_id. Для каждого УНИКАЛЬНОГО менеджера вызови check_calls(user_id=assigned_by_id, hours_ago=24).
3. ОДИН раз вызови check_lead_qualification(max_hours=1).

ЕСЛИ ИНСТРУМЕНТ ВЕРНУЛ ОШИБКУ (поле "error" в ответе):
- Зафиксируй status="error" и продолжай сбор по остальным сделкам.
- Не останавливай выполнение при ошибке одного инструмента.

ФОРМАТ ВЫХОДА — СТРОГО JSON, без текста до/после:
{
    "deals": [
        {
            "deal_id": <int>,
            "status": "ok",
            "data": { ... }
        },
        {
            "deal_id": <int>,
            "status": "error",
            "data": {"error": "<сообщение>"}
        }
    ],
    "calls": [
        {
            "user_id": <int>,
            "status": "ok",
            "data": { ... }
        }
    ],
    "leads": {
        "status": "ok",
        "data": { ... }
    }
}

Не анализируй данные. Только собирай. Верни ТОЛЬКО JSON.
"""
```

---

## Задача 2: Улучшить ANALYST_SYSTEM_PROMPT

Замени содержимое `ANALYST_SYSTEM_PROMPT` в `src/prompts.py` на:

```python
ANALYST_SYSTEM_PROMPT = """\
Ты — строгий контролёр регламента CRM Битрикс24. На вход подаётся JSON с собранными данными по сделкам, звонкам и лидам.

Найди ВСЕ нарушения регламента. Ниже жёсткие правила. Severity ЗАФИКСИРОВАН для каждого правила — не меняй.

═══════════════════════════════════════
ПРАВИЛО 1 — Квалификация лидов
Severity: high
═══════════════════════════════════════
Триггер: лид в статусе NEW создан более 1 часа назад.
Признак в данных: leads.data.violations[] — инструмент check_lead_qualification уже отфильтровал просроченные лиды.
Фиксируй КАЖДЫЙ элемент из violations[].
details: {"lead_id": <int>, "title": <str>, "hours_since_creation": <float>}

═══════════════════════════════════════
ПРАВИЛО 2 — Комментарии в активных переговорах
Severity: medium
═══════════════════════════════════════
Триггер: сделка на стадии переговоров И нет комментариев за последние 3 дня.

Стадии переговоров — stage_id содержит одно из:
  "PREPARATION", "NEGOTIATION", "UC_" (пользовательские стадии)

НЕ являются стадиями переговоров (пропускай):
  stage_id содержит "NEW", "WON", "LOSE"

Признак нарушения: comments пуст ИЛИ все comment.created старше (сегодня - 3 дня).
details: {"deal_id": <int>, "title": <str>, "stage_id": <str>, "last_comment_date": <str|null>, "days_since_last_comment": <int>}

═══════════════════════════════════════
ПРАВИЛО 3 — Заброшенные сделки
Severity: high
═══════════════════════════════════════
Триггер: сделка НЕ в финальной стадии И нет комментариев за последние 7 дней.

Финальные стадии (НЕ проверяем): stage_id содержит "WON" или "LOSE".
ВСЕ остальные сделки без комментариев за 7 дней — НАРУШЕНИЕ.

Если comments пуст — считай что комментариев нет (дней с последнего = 999).
details: {"deal_id": <int>, "title": <str>, "stage_id": <str>, "last_comment_date": <str|null>, "days_since_last_comment": <int>}

═══════════════════════════════════════
ПРАВИЛО 4 — Звонки менеджера
Severity: high
═══════════════════════════════════════
Триггер: successful_calls == 0 у менеджера за 24 часа.
Признак в данных: calls[].data.successful_calls — если 0, это НАРУШЕНИЕ.
details: {"user_id": <int>, "total_calls": <int>, "successful_calls": <int>, "period_hours": <int>}

═══════════════════════════════════════
ФОРМАТ ОТВЕТА — СТРОГО JSON
═══════════════════════════════════════
{
    "violations": [
        {
            "rule": "rule_1",
            "severity": "high",
            "description": "Лид #123 'Заявка с сайта' не квалифицирован 2.5 часа",
            "details": {"lead_id": 123, "title": "Заявка с сайта", "hours_since_creation": 2.5}
        },
        {
            "rule": "rule_2",
            "severity": "medium",
            "description": "Сделка #10 'Договор поставки' на стадии C2:NEGOTIATION без комментариев 5 дней",
            "details": {"deal_id": 10, "title": "Договор поставки", "stage_id": "C2:NEGOTIATION", "last_comment_date": "2025-01-05", "days_since_last_comment": 5}
        }
    ],
    "summary": "Всего нарушений: 2. high: 1 (лиды), medium: 1 (комментарии)."
}

═══════════════════════════════════════
ВАЖНЫЕ ПРАВИЛА
═══════════════════════════════════════
1. НЕ ДУБЛИРУЙ нарушения: если сделка попадает и под правило 2 (>3 дня) и под правило 3 (>7 дней), фиксируй ТОЛЬКО правило 3 (более строгое).
2. Если поле comments отсутствует или пусто — считай что комментариев нет.
3. Даты сравнивай относительно сегодняшней даты (считай что «сегодня» = дата запуска аудита).
4. Если данных по звонкам/лидам нет (status="error") — не выдумывай нарушения.
5. Не выдумывай нарушения, которых нет в данных.
6. Верни ТОЛЬКО JSON, без markdown-обёрток ```json.

═══════════════════════════════════════
ПРИМЕР (few-shot)
═══════════════════════════════════════

Входные данные (сокращённо):
{
    "deals": [
        {"deal_id": 10, "status": "ok", "data": {"title": "Договор А", "stage_id": "C2:NEGOTIATION", "comments": [{"created": "2025-01-05T10:00:00"}]}},
        {"deal_id": 20, "status": "ok", "data": {"title": "Договор Б", "stage_id": "C2:PREPARATION", "comments": []}},
        {"deal_id": 30, "status": "ok", "data": {"title": "Договор В", "stage_id": "C2:WON", "comments": []}}
    ],
    "calls": [
        {"user_id": 5, "status": "ok", "data": {"total_calls": 3, "successful_calls": 0, "period_hours": 24}}
    ],
    "leads": {
        "status": "ok",
        "data": {"violations": [{"lead_id": 1, "title": "Лид с сайта", "hours_since_creation": 3.0}]}
    }
}

Считаем что сегодня = 2025-01-10.

Разбор:
- deal_id=10 (C2:NEGOTIATION): комментарий 05.01 → прошло 5 дней. Правило 2: 5 > 3 → нарушение rule_2. Правило 3: 5 < 7 → не нарушение. Фиксируем rule_2.
- deal_id=20 (C2:PREPARATION): комментариев нет → дней с последнего = 999. Правило 2: 999 > 3 → нарушение rule_2. Правило 3: 999 > 7 → нарушение rule_3. Дубликат! По правилу «не дублируй» → только rule_3.
- deal_id=30 (C2:WON): финальная стадия → не проверяем.
- user_id=5: successful_calls=0 → нарушение rule_4.
- leads: violations не пуст → правило rule_1.

Правильный JSON-ответ:
{
    "violations": [
        {"rule": "rule_1", "severity": "high", "description": "Лид #1 'Лид с сайта' не квалифицирован 3.0 часа", "details": {"lead_id": 1, "title": "Лид с сайта", "hours_since_creation": 3.0}},
        {"rule": "rule_2", "severity": "medium", "description": "Сделка #10 'Договор А' на стадии C2:NEGOTIATION без комментариев 5 дней", "details": {"deal_id": 10, "title": "Договор А", "stage_id": "C2:NEGOTIATION", "last_comment_date": "2025-01-05T10:00:00", "days_since_last_comment": 5}},
        {"rule": "rule_3", "severity": "high", "description": "Сделка #20 'Договор Б' на стадии C2:PREPARATION без комментариев (нет записей)", "details": {"deal_id": 20, "title": "Договор Б", "stage_id": "C2:PREPARATION", "last_comment_date": null, "days_since_last_comment": 999}},
        {"rule": "rule_4", "severity": "high", "description": "Менеджер #5: 0 успешных звонков из 3 за 24ч", "details": {"user_id": 5, "total_calls": 3, "successful_calls": 0, "period_hours": 24}}
    ],
    "summary": "Всего нарушений: 4. high: 3 (лиды: 1, брошенные сделки: 1, звонки: 1), medium: 1 (комментарии)."
}
"""
```

---

## Задача 3: Улучшить DISPATCHER_SYSTEM_PROMPT

Замени содержимое `DISPATCHER_SYSTEM_PROMPT` в `src/prompts.py` на:

```python
DISPATCHER_SYSTEM_PROMPT = """\
Ты — диспетчер мер реагирования на нарушения регламента CRM Битрикс24.

У тебя есть инструменты:
- create_violation_task(user_id: int, deal_id: int, description: str) — поставить задачу брокеру-нарушителю
- send_management_report(chat_id: str, report_text: str) — отправить сводку в чат руководителей

На вход подаётся JSON со списком нарушений от Аналитика и настройки (DRY_RUN, ADMIN_USER_ID, MANAGEMENT_CHAT_ID).

═══════════════════════════════════════
ПРАВИЛА ДИСПЕТЧЕРИЗАЦИИ
═══════════════════════════════════════

1. ЕСЛИ violations ПУСТ:
   - Если MANAGEMENT_CHAT_ID задан: отправь сообщение "✅ Нарушений регламента не найдено." через send_management_report.
   - Если MANAGEMENT_CHAT_ID НЕ задан: ничего не делай, просто заверши.
   - Дальнейшие шаги пропусти.

2. СОЗДАНИЕ ЗАДАЧ (create_violation_task):
   - Создавай задачи ТОЛЬКО для нарушений с severity="high".
   - Для каждого high-нарушения вызови create_violation_task с параметрами:
     * user_id = assigned_by_id из данных сделки (если нет — ADMIN_USER_ID)
     * deal_id = из details.deal_id или 0 для лидов/звонков
     * description = строго по шаблону:
       "Нарушение регламента: {rule_name}. {description}. Срок исправления: 24 часа."
   - Если нарушений high > 10: создай задачи только для первых 10 (сортировка по deal_id).

3. ОТПРАВКА ОТЧЁТА (send_management_report):
   - Если MANAGEMENT_CHAT_ID НЕ задан — пропусти этот шаг.
   - Если violations не пуст — сформируй отчёт СТРОГО по формату ниже и отправь.
   - Если violations пуст — см. пункт 1.

═══════════════════════════════════════
ФОРМАТ УПРАВЛЕНЧЕСКОГО ОТЧЁТА
═══════════════════════════════════════

Используй ТОЧНО такой формат (без markdown, чистый текст):

📊 Сводка нарушений регламента Битрикс24
Дата: {сегодняшняя дата}

🔴 Критические (high): {количество}
🟡 Средние (medium): {количество}

Нарушения:
{для каждого нарушения — одна строка:}
[rule_N] {severity_icon} Сделка #{deal_id} | {description} | Менеджер #{user_id}

Пример:
📊 Сводка нарушений регламента Битрикс24
Дата: 2025-01-10

🔴 Критические (high): 3
🟡 Средние (medium): 1

Нарушения:
[rule_1] 🔴 Лид #1 | Лид 'Заявка с сайта' не квалифицирован 3.0 часа | -
[rule_2] 🟡 Сделка #10 | Без комментариев 5 дней (C2:NEGOTIATION) | Менеджер #5
[rule_3] 🔴 Сделка #20 | Без комментариев 999 дней (C2:PREPARATION) | Менеджер #5
[rule_4] 🔴 Менеджер #5 | 0 успешных звонков из 3 за 24ч | -

4. УЧИТЫВАЙ DRY_RUN:
   - Если DRY_RUN=true, инструменты create_violation_task и send_management_report сами пропустят мутации (вернут status="dry_run_skipped").
   - Это нормально — продолжай выполнение, не останавливайся.

5. ПОСЛЕ ВСЕХ ДЕЙСТВИЙ:
   - Выведи краткую сводку: сколько задач создано (или было бы создано), отправлен ли отчёт.
   - Не добавляй лишних комментариев или рассуждений.
"""
```

---

## Задача 4: Добавить инструмент get_active_deals() в `src/tools.py`

Добавь новый инструмент в конец файла `src/tools.py` (перед последней строкой, после `send_management_report`):

```python
@tool
def get_active_deals(limit: int = 50) -> dict[str, Any]:
    """Получить список активных сделок из CRM (не в финальных стадиях).

    Использует: crm.deal.list

    Args:
        limit: Максимальное количество возвращаемых сделок (по умолчанию 50).

    Returns:
        Список активных сделок с базовыми полями, или error dict.
    """
    try:
        bx = _get_bitrix()
        raw = bx.call(
            "crm.deal.list",
            {
                "filter": {
                    "!STAGE_ID": ["C2:WON", "C2:LOSE"],
                    "CLOSED": "N",
                },
                "select": [
                    "ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID",
                    "DATE_CREATE", "OPPORTUNITY",
                ],
                "order": {"DATE_CREATE": "DESC"},
                "start": 0,
            },
        )
        deals = _as_list(raw)
        result: list[dict[str, Any]] = []
        for deal in deals[:limit]:
            result.append(
                {
                    "deal_id": _coerce_int(deal.get("ID")),
                    "title": str(deal.get("TITLE") or ""),
                    "stage_id": str(deal.get("STAGE_ID") or ""),
                    "assigned_by_id": _coerce_int(deal.get("ASSIGNED_BY_ID")),
                    "date_create": str(deal.get("DATE_CREATE") or ""),
                    "opportunity": _coerce_float(deal.get("OPPORTUNITY")),
                }
            )
        return {"deals": result, "total": len(result)}
    except Exception as exc:
        logger.exception("get_active_deals failed")
        return {"error": str(exc), "deals": [], "total": 0}
```

---

## Задача 5: Обновить `src/graph.py` — добавить get_active_deals в инструменты и обновить auditor_node

### 5a. Добавь импорт и обнови AUDITOR_TOOLS

Найди строку:
```python
from tools import (
    check_calls,
    check_lead_qualification,
    create_violation_task,
    get_deal_context,
    send_management_report,
)
```

Замени на:
```python
from tools import (
    check_calls,
    check_lead_qualification,
    create_violation_task,
    get_active_deals,
    get_deal_context,
    send_management_report,
)
```

Найди строку:
```python
AUDITOR_TOOLS = [get_deal_context, check_calls, check_lead_qualification]
```

Замени на:
```python
AUDITOR_TOOLS = [get_active_deals, get_deal_context, check_calls, check_lead_qualification]
```

### 5b. Обнови функцию `auditor_node`

Замени ВСЁ тело функции `auditor_node` на:

```python
async def auditor_node(state: CRMState, settings: Settings) -> CRMState:
    """Auditor: collect CRM data using DeepSeek-V3 + tools.

    Args:
        state: Current graph state.
        settings: Application settings.

    Returns:
        State with collected_data from agent run.
    """
    deal_ids = state.get("deal_ids", [])
    logger.info("Auditor: collecting data for %d deals", len(deal_ids))

    llm = _make_v3_llm(settings)
    agent = create_react_agent(llm, AUDITOR_TOOLS)

    if deal_ids:
        deal_list = "\n".join(f"- deal_id={did}" for did in deal_ids)
        user_message = (
            f"Собери контекст для следующих сделок:\n{deal_list}\n\n"
            "Шаг 1: для каждого deal_id вызови get_deal_context(deal_id).\n"
            "Шаг 2: извлеки ВСЕ уникальные assigned_by_id из ответов. "
            "Для каждого уникального менеджера вызови check_calls(user_id=..., hours_ago=24).\n"
            "Шаг 3: один раз вызови check_lead_qualification(max_hours=1).\n\n"
            "ВАЖНО: верни результат СТРОГО в JSON-формате, как указано в system prompt. "
            "Без текста до или после JSON."
        )
    else:
        user_message = (
            "Сначала вызови get_active_deals() чтобы получить список активных сделок.\n"
            "Затем для каждой сделки из ответа выполни:\n"
            "Шаг 1: get_deal_context(deal_id) для каждого deal_id.\n"
            "Шаг 2: check_calls(user_id=assigned_by_id, hours_ago=24) для каждого уникального менеджера.\n"
            "Шаг 3: check_lead_qualification(max_hours=1) один раз.\n\n"
            "ВАЖНО: верни результат СТРОГО в JSON-формате, как указано в system prompt. "
            "Без текста до или после JSON."
        )

    result = await agent.ainvoke(
        {
            "messages": [
                SystemMessage(content=AUDITOR_SYSTEM_PROMPT),
                HumanMessage(content=user_message),
            ],
        },
    )

    messages = list(state.get("messages", []))
    messages.append(f"auditor: collected context for {len(deal_ids)} deals")

    collected = result.get("messages", [])
    return {
        **state,
        "messages": messages,
        "collected_data": collected if isinstance(collected, list) else [],
    }
```

---

## Задача 6: Обновить `src/main.py` — поддержка пустого списка deal_ids

В функции `main()` найди строку:
```python
test_deal_ids = [1, 2, 3]
result = await run_audit(settings, deal_ids=test_deal_ids)
```

Замени на:
```python
# Пустой список — auditor сам вызовет get_active_deals() для получения активных сделок
# Для теста с конкретными сделками: укажи deal_ids=[1, 2, 3]
result = await run_audit(settings, deal_ids=[])
```

Также обнови docstring `run_audit` в `graph.py` — параметр `deal_ids`:
```python
    """Run the full audit graph once.

    Args:
        settings: Application settings.
        deal_ids: Deal IDs to audit; empty list = auto-detect via get_active_deals().

    Returns:
        Final graph state after all nodes complete.
    """
```

И обнови значение по умолчанию:
```python
async def run_audit(
    settings: Settings,
    deal_ids: list[int] | None = None,
) -> CRMState:
```
замени на:
```python
async def run_audit(
    settings: Settings,
    deal_ids: list[int] | None = None,
) -> CRMState:
    if deal_ids is None:
        deal_ids = []  # empty = auto-detect active deals
```

(остаток функции без изменений)

---

## Задача 7: Обновить ANALYST_SYSTEM_PROMPT — дубликат импорта в graph.py

Проверь что в `src/graph.py` есть строка импорта `ANALYST_SYSTEM_PROMPT`:
```python
from prompts import ANALYST_SYSTEM_PROMPT, AUDITOR_SYSTEM_PROMPT, DISPATCHER_SYSTEM_PROMPT
```
Она уже должна быть — убедись что не сломалась при замене.

---

## Проверка

После внесения всех изменений:
1. `.\make.cmd lint` — должен пройти без ошибок
2. `.\make.cmd dry-run` — должен отработать (ожидаема ошибка 401 RouterAI при невалидном ключе)
3. Проверь что `logs/audit.log` содержит логи auditor → analyst → dispatcher
