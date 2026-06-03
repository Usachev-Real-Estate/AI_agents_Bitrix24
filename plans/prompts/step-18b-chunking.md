# Промпт для Cursor — v2 Чанкинг: батчи по 100 для 4 аналитиков

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`.

**Проблема:** Agent 4 (670 лидов) → 102K символов JSON → битый синтаксис → `JSONDecodeError`.
**Решение:** бить данные на батчи по 100 сущностей, LLM вызывается N раз, violations агрегируются.

---

## Задача 1: Добавить константу `ANALYST_CHUNK_SIZE`

В начало `src/graph.py` (после импортов):

```python
ANALYST_CHUNK_SIZE = 100
```

## Задача 2: Переписать 4 аналитика с чанкингом

Заменить ВСЕ 4 функции аналитиков (`lead_analyst`, `buyer_deal_analyst`, `buyer_calls_controller`, `missed_calls_controller`) на новые.

### 2a. lead_analyst (Agent 4)

```python
async def lead_analyst(state: AuditState, settings: Settings) -> AuditState:
    """Agent 4: analyze leads for stage violations (chunked by 100)."""
    leads = state.get("raw_leads", [])
    logger.info("Agent 4 (Lead Analyst): analyzing %d leads in chunks of %d",
                len(leads), ANALYST_CHUNK_SIZE)

    if not leads:
        return {**state, "violations": []}

    llm = _make_r1_llm(settings)
    current_time = state.get("current_time", "")
    all_violations: list[dict[str, Any]] = []

    for i in range(0, len(leads), ANALYST_CHUNK_SIZE):
        chunk = leads[i:i + ANALYST_CHUNK_SIZE]
        payload = {"leads": chunk, "current_time": current_time}

        try:
            response = await llm.ainvoke([
                SystemMessage(content=LEAD_ANALYST_PROMPT),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
            ])
            content = _message_content_to_str(response.content)
            new_violations = _parse_violations_json(content)
            all_violations.extend(new_violations)
            logger.debug("Agent 4 chunk %d-%d: %d violations",
                         i, min(i + ANALYST_CHUNK_SIZE, len(leads)),
                         len(new_violations))
        except Exception as exc:
            logger.warning("Agent 4 chunk %d-%d failed: %s",
                          i, min(i + ANALYST_CHUNK_SIZE, len(leads)), exc)

    logger.info("Agent 4: found %d lead violations total", len(all_violations))
    return {**state, "violations": all_violations}
```

### 2b. buyer_deal_analyst (Agent 5)

```python
async def buyer_deal_analyst(state: AuditState, settings: Settings) -> AuditState:
    """Agent 5: analyze buyer deals for stage violations (chunked by 100)."""
    deals = state.get("raw_buyers_deals", [])
    logger.info("Agent 5 (Buyer Deal Analyst): analyzing %d deals in chunks of %d",
                len(deals), ANALYST_CHUNK_SIZE)

    if not deals:
        return {**state, "violations": []}

    llm = _make_r1_llm(settings)
    current_time = state.get("current_time", "")
    all_violations: list[dict[str, Any]] = []

    for i in range(0, len(deals), ANALYST_CHUNK_SIZE):
        chunk = deals[i:i + ANALYST_CHUNK_SIZE]
        payload = {"deals": chunk, "current_time": current_time}

        try:
            response = await llm.ainvoke([
                SystemMessage(content=BUYER_DEAL_ANALYST_PROMPT),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
            ])
            content = _message_content_to_str(response.content)
            new_violations = _parse_violations_json(content)
            all_violations.extend(new_violations)
            logger.debug("Agent 5 chunk %d-%d: %d violations",
                         i, min(i + ANALYST_CHUNK_SIZE, len(deals)),
                         len(new_violations))
        except Exception as exc:
            logger.warning("Agent 5 chunk %d-%d failed: %s",
                          i, min(i + ANALYST_CHUNK_SIZE, len(deals)), exc)

    logger.info("Agent 5: found %d buyer deal violations total", len(all_violations))
    return {**state, "violations": all_violations}
```

### 2c. buyer_calls_controller (Agent 6)

```python
async def buyer_calls_controller(state: AuditState, settings: Settings) -> AuditState:
    """Agent 6: check buyer deals for missed callbacks (chunked by 100)."""
    deals = state.get("raw_buyers_deals", [])
    logger.info("Agent 6 (Buyer Calls): analyzing %d deals for call patterns",
                len(deals))

    if not deals:
        return {**state, "violations": []}

    llm = _make_r1_llm(settings)
    current_time = state.get("current_time", "")
    all_violations: list[dict[str, Any]] = []

    for i in range(0, len(deals), ANALYST_CHUNK_SIZE):
        chunk = deals[i:i + ANALYST_CHUNK_SIZE]
        payload = {"deals": chunk, "current_time": current_time}

        try:
            response = await llm.ainvoke([
                SystemMessage(content=BUYER_CALLS_PROMPT),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
            ])
            content = _message_content_to_str(response.content)
            new_violations = _parse_violations_json(content)
            all_violations.extend(new_violations)
        except Exception as exc:
            logger.warning("Agent 6 chunk %d-%d failed: %s",
                          i, min(i + ANALYST_CHUNK_SIZE, len(deals)), exc)

    logger.info("Agent 6: found %d call violations total", len(all_violations))
    return {**state, "violations": all_violations}
```

### 2d. missed_calls_controller (Agent 7)

```python
async def missed_calls_controller(state: AuditState, settings: Settings) -> AuditState:
    """Agent 7: check leads for missed calls without callback (chunked by 100)."""
    leads = state.get("raw_leads", [])
    logger.info("Agent 7 (Missed Calls): analyzing %d leads", len(leads))

    if not leads:
        return {**state, "violations": []}

    llm = _make_r1_llm(settings)
    current_time = state.get("current_time", "")
    all_violations: list[dict[str, Any]] = []

    for i in range(0, len(leads), ANALYST_CHUNK_SIZE):
        chunk = leads[i:i + ANALYST_CHUNK_SIZE]
        payload = {"leads": chunk, "current_time": current_time}

        try:
            response = await llm.ainvoke([
                SystemMessage(content=MISSED_CALLS_PROMPT),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
            ])
            content = _message_content_to_str(response.content)
            new_violations = _parse_violations_json(content)
            all_violations.extend(new_violations)
        except Exception as exc:
            logger.warning("Agent 7 chunk %d-%d failed: %s",
                          i, min(i + ANALYST_CHUNK_SIZE, len(leads)), exc)

    logger.info("Agent 7: found %d missed call violations total", len(all_violations))
    return {**state, "violations": all_violations}
```

---

## Проверка

1. `.\make.cmd lint` — должен пройти
2. `.\make.cmd dry-run` — все 4 аналитика с чанкингом:
   - Agent 4: 7 батчей по 100 (670 лидов) + 1 батч × 70
   - Agent 5: 6 батчей по 100 (553 сделки) + 1 батч × 53
   - Agent 6: аналогично Agent 5
   - Agent 7: аналогично Agent 4
3. JSONDecodeError должен исчезнуть
