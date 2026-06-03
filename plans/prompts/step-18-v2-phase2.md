# Промпт для Cursor — v2 Фаза 2: 4 LLM-аналитика + fan-out граф

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/prompts.py`, `src/graph.py`.

**Суть фазы:** добавить 4 LLM-аналитика (Agent 4–7), каждый со своим промптом. После сбора данных коллекторами аналитики ищут нарушения в своих доменах.

---

## Задача 1: Добавить 4 промпта в `src/prompts.py`

Добавить в КОНЕЦ файла (НЕ заменять существующие v1 промпты):

### 1a. LEAD_ANALYST_PROMPT (Agent 4)

```python
LEAD_ANALYST_PROMPT = """\
Ты — контролёр качества обработки лидов в CRM (воронка лидов).

На вход подаётся JSON: {"leads": [...], "current_time": "<ISO>"}
Каждый лид: lead_id, title, status_id, assigned_by_id, date_create, comments_field, timeline.

Найди нарушения. Severity ЗАФИКСИРОВАН.

═══════════════════════════════════════
ПРАВИЛО 1 — Лид в статусе "Новый" > 2 часов
Severity: high
═══════════════════════════════════════
Триггер: status_id содержит "NEW" И с date_create прошло > 2 часов
И в timeline нет ни одного комментария от ответственного (author_id == assigned_by_id).
Если timeline пуст ИЛИ все комментарии от других пользователей — НАРУШЕНИЕ.
details: lead_id, title, status_id, hours_since_creation, has_broker_comment (bool)

═══════════════════════════════════════
ПРАВИЛО 2 — Лид в статусе "Спам" без обоснования
Severity: high
═══════════════════════════════════════
Триггер: status_id содержит "SPAM"
И (comments_field пусто ИЛИ timeline не содержит комментария с обоснованием причины спама).
Обоснование = комментарий длиной > 20 символов от любого пользователя.
Если comments_field пуст И timeline без обоснования — НАРУШЕНИЕ.
details: lead_id, title, comments_field_empty (bool), has_justification (bool)

═══════════════════════════════════════
ПРАВИЛО 3 — Лид в статусе "Нецелевой" без обоснования
Severity: high
═══════════════════════════════════════
Триггер: status_id НЕ "NEW" И НЕ "SPAM" И НЕ "WON" И НЕ "LOSE"
(т.е. любой промежуточный/конечный статус, включая "Нецелевой").
И (comments_field пусто ИЛИ timeline не содержит комментария с обоснованием).
Аналогично правилу 2.
details: lead_id, title, status_id, comments_field_empty (bool), has_justification (bool)

═══════════════════════════════════════
ФОРМАТ ОТВЕТА — СТРОГО JSON
═══════════════════════════════════════
{
    "violations": [
        {
            "entity_type": "lead",
            "entity_id": <lead_id>,
            "responsible_id": <assigned_by_id>,
            "severity": "high",
            "rule": "lead_rule_1 | lead_rule_2 | lead_rule_3",
            "reason": "<описание на русском>",
            "details": {...}
        }
    ]
}

Верни ТОЛЬКО JSON, без markdown.
"""
```

### 1b. BUYER_DEAL_ANALYST_PROMPT (Agent 5)

```python
BUYER_DEAL_ANALYST_PROMPT = """\
Ты — контролёр сделок воронки "Покупатели" (недвижимость).

На вход подаётся JSON: {"deals": [...], "current_time": "<ISO>"}
Каждая сделка: deal_id, title, stage_id, assigned_by_id, date_create, timeline, uf_fields.

Найди нарушения. Severity ЗАФИКСИРОВАН.

ВАЖНО: stage_id в Битрикс24 — строки вида "C2:PREPARATION", "C2:UC_XXX", "C2:NEW" и т.д.
Ориентируйся на СМЫСЛ этапа, а не точное совпадение строки.

═══════════════════════════════════════
ПРАВИЛО 1 — Этап "Первый контакт" > 1 дня
Severity: medium
═══════════════════════════════════════
Триггер: stage_id похож на "первый контакт" / "NEW" / "FIRST_CONTACT"
И с date_create прошло > 1 дня (24 часов).
details: deal_id, title, stage_id, days_on_stage (float)

═══════════════════════════════════════
ПРАВИЛО 2 — Этап "Подбор" > 2 дней
Severity: medium
═══════════════════════════════════════
Триггер: stage_id похож на "подбор" / "PREPARATION"
И с date_create прошло > 2 дней.
details: deal_id, title, stage_id, days_on_stage (float)

═══════════════════════════════════════
ПРАВИЛО 3 — Этап "Показ"
Severity: medium
═══════════════════════════════════════
Триггер: stage_id похож на "показ" / "SHOW" / "DEMONSTRATION".

Подправила:
3a. Если в uf_fields нет поля, похожего на "Дата показа" (ключ содержит "DATE" или "SHOW") — НАРУШЕНИЕ.
3b. Если такое поле есть, но дата в нём меньше current_time (просрочено) — НАРУШЕНИЕ.

details: deal_id, title, stage_id, has_show_date (bool), show_date (str|null), is_overdue (bool)

═══════════════════════════════════════
ПРАВИЛО 4 — Этап "Показ проведен" > 1 дня
Severity: medium
═══════════════════════════════════════
Триггер: stage_id похож на "показ проведен" / "SHOW_DONE" / "DEMONSTRATION_DONE"
И с date_create (или даты перехода на этап) прошло > 1 дня.

Дополнительно: проанализируй uf_fields на наличие поля "Результат показа".
Если там короткая отписка (< 30 символов) типа "Думают", "Понравилось", "Будет думать" —
это НЕ считается развёрнутым комментарием. Тогда ищи в timeline развёрнутый комментарий
ответственного (длиной > 50 символов). Если ни того, ни другого — НАРУШЕНИЕ.

details: deal_id, title, stage_id, days_on_stage, show_result (str|null), has_detailed_comment (bool)

═══════════════════════════════════════
ПРАВИЛО 5 — Этап "Отложенный спрос": нет комментария за 7 дней
Severity: medium
═══════════════════════════════════════
Триггер: stage_id похож на "отложенный спрос" / "DEFERRED" / "LATER"
И в timeline нет комментария от assigned_by_id за последние 7 дней.
details: deal_id, title, stage_id, days_since_last_comment (int)

═══════════════════════════════════════
ФОРМАТ ОТВЕТА — СТРОГО JSON
═══════════════════════════════════════
{
    "violations": [
        {
            "entity_type": "deal",
            "entity_id": <deal_id>,
            "responsible_id": <assigned_by_id>,
            "severity": "medium",
            "rule": "buyer_stage_1 | buyer_stage_2 | ... | buyer_stage_5",
            "reason": "<описание на русском>",
            "details": {...}
        }
    ]
}

Верни ТОЛЬКО JSON.
"""
```

### 1c. BUYER_CALLS_PROMPT (Agent 6)

```python
BUYER_CALLS_PROMPT = """\
Ты — контролёр качества коммуникаций в сделках воронки "Покупатели".

На вход подаётся JSON: {"deals": [...], "current_time": "<ISO>"}
Каждая сделка: deal_id, title, stage_id, timeline.

Найди нарушения. Severity: very high.

═══════════════════════════════════════
ПРАВИЛО — Входящий вызов без обратной связи
Severity: very high
═══════════════════════════════════════
Триггер: в timeline есть запись о "Входящем вызове" (comment содержит "входящий вызов",
"входящий звонок", "INCOMING", "CALL_IN" — регистронезависимо).

И сделка после этого двигалась дальше по воронке (stage_id изменился с момента вызова).

Если после такого входящего вызова НЕТ:
- "Исходящего вызова" (comment содержит "исходящий", "OUTGOING", "CALL_OUT") от assigned_by_id
- ИЛИ комментария от assigned_by_id длиной > 30 символов (подтверждение текстовой коммуникации)

→ НАРУШЕНИЕ.

details: deal_id, title, incoming_call_date (str), has_outgoing_call (bool), has_text_confirmation (bool)

═══════════════════════════════════════
ФОРМАТ ОТВЕТА — СТРОГО JSON
═══════════════════════════════════════
{
    "violations": [
        {
            "entity_type": "deal",
            "entity_id": <deal_id>,
            "responsible_id": <assigned_by_id>,
            "severity": "very high",
            "rule": "buyer_no_callback",
            "reason": "<описание на русском>",
            "details": {...}
        }
    ]
}

Верни ТОЛЬКО JSON.
"""
```

### 1d. MISSED_CALLS_PROMPT (Agent 7)

```python
MISSED_CALLS_PROMPT = """\
Ты — контролёр пропущенных звонков по лидам.

На вход подаётся JSON: {"leads": [...], "current_time": "<ISO>"}
Каждый лид: lead_id, title, assigned_by_id, timeline.

Найди нарушения. Severity: very high.

═══════════════════════════════════════
ПРАВИЛО — Пропущенный входящий без обратного звонка
Severity: very high
═══════════════════════════════════════
Триггер: в timeline есть запись о "Пропущенном входящем вызове"
(comment содержит "пропущен", "missed", "MISSED_CALL" — регистронезависимо).

После пропущенного вызова ОБЯЗАТЕЛЬНО должен быть "Успешный исходящий вызов"
(comment содержит "успешный исходящий", "исходящий вызов", "OUTGOING", "SUCCESS",
или "CALL_OUT") от assigned_by_id, с датой ПОЗЖЕ даты пропущенного.

Если такого вызова нет — НАРУШЕНИЕ.

details: lead_id, title, missed_call_date (str), has_callback (bool)

═══════════════════════════════════════
ФОРМАТ ОТВЕТА — СТРОГО JSON
═══════════════════════════════════════
{
    "violations": [
        {
            "entity_type": "lead",
            "entity_id": <lead_id>,
            "responsible_id": <assigned_by_id>,
            "severity": "very high",
            "rule": "lead_missed_callback",
            "reason": "<описание на русском>",
            "details": {...}
        }
    ]
}

Верни ТОЛЬКО JSON.
"""
```

---

## Задача 2: Обновить импорты и AuditState в `src/graph.py`

### 2a. Добавить импорт промптов:
```python
from prompts import (
    ...,
    LEAD_ANALYST_PROMPT,
    BUYER_DEAL_ANALYST_PROMPT,
    BUYER_CALLS_PROMPT,
    MISSED_CALLS_PROMPT,
)
```

### 2b. Обновить AuditState — violations с Annotated:

Заменить:
```python
violations: list[dict[str, Any]]
```
На:
```python
violations: Annotated[list[dict[str, Any]], operator.add]
```

И добавить импорт operator (если ещё нет):
```python
import operator
```

---

## Задача 3: 4 узла-аналитика в `src/graph.py`

Добавить ПОСЛЕ seller_collector, ПЕРЕД build_graph_v2:

```python
async def lead_analyst(state: AuditState, settings: Settings) -> AuditState:
    """Agent 4: analyze leads for stage violations."""
    logger.info("Agent 4 (Lead Analyst): analyzing %d leads",
                len(state.get("raw_leads", [])))

    llm = _make_r1_llm(settings)

    payload = {
        "leads": state.get("raw_leads", []),
        "current_time": state.get("current_time", ""),
    }

    response = await llm.ainvoke([
        SystemMessage(content=LEAD_ANALYST_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
    ])

    new_violations = _parse_violations_json(
        _message_content_to_str(response.content)
    )

    logger.info("Agent 4: found %d lead violations", len(new_violations))
    return {
        **state,
        "violations": new_violations,
    }


async def buyer_deal_analyst(state: AuditState, settings: Settings) -> AuditState:
    """Agent 5: analyze buyer deals for stage/timeline violations."""
    logger.info("Agent 5 (Buyer Deal Analyst): analyzing %d deals",
                len(state.get("raw_buyers_deals", [])))

    llm = _make_r1_llm(settings)

    payload = {
        "deals": state.get("raw_buyers_deals", []),
        "current_time": state.get("current_time", ""),
    }

    response = await llm.ainvoke([
        SystemMessage(content=BUYER_DEAL_ANALYST_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
    ])

    new_violations = _parse_violations_json(
        _message_content_to_str(response.content)
    )

    logger.info("Agent 5: found %d buyer deal violations", len(new_violations))
    return {
        **state,
        "violations": new_violations,
    }


async def buyer_calls_controller(state: AuditState, settings: Settings) -> AuditState:
    """Agent 6: check buyer deals for missed callbacks."""
    logger.info("Agent 6 (Buyer Calls): analyzing %d deals for call patterns",
                len(state.get("raw_buyers_deals", [])))

    llm = _make_r1_llm(settings)

    payload = {
        "deals": state.get("raw_buyers_deals", []),
        "current_time": state.get("current_time", ""),
    }

    response = await llm.ainvoke([
        SystemMessage(content=BUYER_CALLS_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
    ])

    new_violations = _parse_violations_json(
        _message_content_to_str(response.content)
    )

    logger.info("Agent 6: found %d call violations", len(new_violations))
    return {
        **state,
        "violations": new_violations,
    }


async def missed_calls_controller(state: AuditState, settings: Settings) -> AuditState:
    """Agent 7: check leads for missed incoming calls without callback."""
    logger.info("Agent 7 (Missed Calls): analyzing %d leads",
                len(state.get("raw_leads", [])))

    llm = _make_r1_llm(settings)

    payload = {
        "leads": state.get("raw_leads", []),
        "current_time": state.get("current_time", ""),
    }

    response = await llm.ainvoke([
        SystemMessage(content=MISSED_CALLS_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
    ])

    new_violations = _parse_violations_json(
        _message_content_to_str(response.content)
    )

    logger.info("Agent 7: found %d missed call violations", len(new_violations))
    return {
        **state,
        "violations": new_violations,
    }
```

---

## Задача 4: Обновить `build_graph_v2()` — добавить аналитиков

Заменить ВСЮ функцию `build_graph_v2`:

```python
def build_graph_v2(settings: Settings):
    """Build v2 audit graph: 3 collectors → 4 analysts.

    Args:
        settings: Application settings.

    Returns:
        Compiled LangGraph runnable.
    """
    graph = StateGraph(AuditState)

    # Wrappers
    async def lead_col(state: AuditState) -> AuditState:
        return await lead_collector(state, settings)

    async def buyer_col(state: AuditState) -> AuditState:
        return await buyer_collector(state, settings)

    async def seller_col(state: AuditState) -> AuditState:
        return await seller_collector(state, settings)

    async def lead_an(state: AuditState) -> AuditState:
        return await lead_analyst(state, settings)

    async def buyer_deal_an(state: AuditState) -> AuditState:
        return await buyer_deal_analyst(state, settings)

    async def buyer_calls(state: AuditState) -> AuditState:
        return await buyer_calls_controller(state, settings)

    async def missed_calls(state: AuditState) -> AuditState:
        return await missed_calls_controller(state, settings)

    # Nodes
    graph.add_node("lead_collector", lead_col)
    graph.add_node("buyer_collector", buyer_col)
    graph.add_node("seller_collector", seller_col)
    graph.add_node("lead_analyst", lead_an)
    graph.add_node("buyer_deal_analyst", buyer_deal_an)
    graph.add_node("buyer_calls_controller", buyer_calls)
    graph.add_node("missed_calls_controller", missed_calls)

    # Parallel collectors from START
    graph.add_edge(START, "lead_collector")
    graph.add_edge(START, "buyer_collector")
    graph.add_edge(START, "seller_collector")

    # Collector → Analyst chains (sequential per branch)
    graph.add_edge("lead_collector", "lead_analyst")
    graph.add_edge("lead_analyst", "missed_calls_controller")
    graph.add_edge("missed_calls_controller", END)

    graph.add_edge("buyer_collector", "buyer_deal_analyst")
    graph.add_edge("buyer_deal_analyst", "buyer_calls_controller")
    graph.add_edge("buyer_calls_controller", END)

    graph.add_edge("seller_collector", END)

    return graph.compile()
```

---

## Задача 5: Обновить `run_audit_v2()` — добавить violations в лог

Заменить тело функции:

```python
async def run_audit_v2(settings: Settings) -> AuditState:
    """Run v2 audit: 3 collectors → 4 analysts.

    Args:
        settings: Application settings.

    Returns:
        Final AuditState with violations.
    """
    from datetime import datetime, timezone

    app = build_graph_v2(settings)
    initial: AuditState = {
        "raw_leads": [],
        "raw_buyers_deals": [],
        "raw_sellers_deals": [],
        "violations": [],
        "current_time": datetime.now(timezone.utc).isoformat(),
        "dry_run": settings.dry_run,
        "messages": [],
    }
    return await app.ainvoke(initial)
```

И обнови `main()` в `main.py`:

```python
result = await run_audit_v2(settings)
logger.info(
    "Audit V2 finished: leads=%d, buyer_deals=%d, seller_deals=%d, violations=%d",
    len(result.get("raw_leads", [])),
    len(result.get("raw_buyers_deals", [])),
    len(result.get("raw_sellers_deals", [])),
    len(result.get("violations", [])),
)
```

---

## Проверка

1. `.\make.cmd lint` — должен пройти
2. `.\make.cmd dry-run` — полный пайплайн: 3 коллектора → 4 LLM-аналитика
3. В логах: `Agent 4: found N lead violations`, `Agent 5: ...`, etc.
4. `violations` в логах — непустой массив (если есть реальные нарушения)
