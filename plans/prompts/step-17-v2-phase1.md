# Промпт для Cursor — v2 Фаза 1: T1, T2 + 3 коллектора + AuditState без LLM

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `.env`, `.env.example`, `src/config.py`, `src/tools.py`, `src/graph.py`, `src/main.py`.

**Суть фазы:** сбор данных (лиды + сделки по воронкам с таймлайном), без LLM-аналитиков.

---

## Задача 1: Добавить CATEGORY_ID в `.env` и `config.py`

### 1a. `.env` — добавить в секцию Bitrix24:
```ini
# ID воронок сделок (Настройки → CRM → Воронки)
BUYERS_CATEGORY_ID=18
SELLERS_CATEGORY_ID=0
```

### 1b. `.env.example` — аналогично.

### 1c. `src/config.py` — добавить поля в Settings:
```python
buyers_category_id: int = Field(default=0, validation_alias="BUYERS_CATEGORY_ID")
sellers_category_id: int = Field(default=0, validation_alias="SELLERS_CATEGORY_ID")
```

---

## Задача 2: Добавить T1 и T2 в `src/tools.py`

Добавить ДВА новых инструмента в конец файла (перед последней строкой):

### T1: get_all_leads_with_timeline

```python
@tool
def get_all_leads_with_timeline() -> dict[str, Any]:
    """Получить ВСЕ лиды с полным таймлайном комментариев.

    Использует: crm.lead.list → на каждый лид crm.timeline.comment.list

    Returns:
        {"leads": [...], "total": int}, где каждый лид имеет поле "timeline".
    """
    try:
        bx = _get_bitrix()
        leads = bx.get_all(
            "crm.lead.list",
            {
                "select": [
                    "ID", "TITLE", "STATUS_ID", "ASSIGNED_BY_ID",
                    "DATE_CREATE", "COMMENTS", "SOURCE_ID",
                ],
            },
        )
        result: list[dict[str, Any]] = []
        for lead in leads:
            if not isinstance(lead, dict):
                continue
            lead_id = _coerce_int(lead.get("ID"))
            # Fetch timeline for this lead
            try:
                timeline_raw = bx.get_all(
                    "crm.timeline.comment.list",
                    {
                        "filter": {
                            "ENTITY_ID": lead_id,
                            "ENTITY_TYPE": "lead",
                        },
                        "select": ["ID", "AUTHOR_ID", "COMMENT", "CREATED"],
                    },
                )
                timeline = _extract_comments(timeline_raw)
            except Exception:
                timeline = []

            result.append(
                {
                    "lead_id": lead_id,
                    "title": str(lead.get("TITLE") or ""),
                    "status_id": str(lead.get("STATUS_ID") or ""),
                    "assigned_by_id": _coerce_int(lead.get("ASSIGNED_BY_ID")),
                    "date_create": str(lead.get("DATE_CREATE") or ""),
                    "comments_field": str(lead.get("COMMENTS") or ""),
                    "source_id": str(lead.get("SOURCE_ID") or ""),
                    "timeline": timeline,
                }
            )
        return {"leads": result, "total": len(result)}
    except Exception as exc:
        logger.exception("get_all_leads_with_timeline failed")
        return {"error": str(exc), "leads": [], "total": 0}
```

### T2: get_deals_by_funnel_with_timeline

```python
@tool
def get_deals_by_funnel_with_timeline(category_id: int) -> dict[str, Any]:
    """Получить ВСЕ сделки указанной воронки с полным таймлайном.

    Использует: crm.deal.list (filter: CATEGORY_ID) → на каждую сделку
    crm.timeline.comment.list

    Args:
        category_id: ID воронки (0 = Продавцы, 18 = Покупатели).

    Returns:
        {"deals": [...], "category_id": int, "total": int}.
    """
    try:
        bx = _get_bitrix()
        deals = bx.get_all(
            "crm.deal.list",
            {
                "filter": {
                    "CATEGORY_ID": category_id,
                    "CLOSED": "N",
                },
                "select": [
                    "ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID",
                    "DATE_CREATE", "OPPORTUNITY", "CATEGORY_ID",
                    "UF_*",  # пользовательские поля
                ],
            },
        )
        result: list[dict[str, Any]] = []
        for deal in deals:
            if not isinstance(deal, dict):
                continue
            deal_id = _coerce_int(deal.get("ID"))
            # Fetch timeline
            try:
                timeline_raw = bx.get_all(
                    "crm.timeline.comment.list",
                    {
                        "filter": {
                            "ENTITY_ID": deal_id,
                            "ENTITY_TYPE": "deal",
                        },
                        "select": ["ID", "AUTHOR_ID", "COMMENT", "CREATED"],
                    },
                )
                timeline = _extract_comments(timeline_raw)
            except Exception:
                timeline = []

            result.append(
                {
                    "deal_id": deal_id,
                    "title": str(deal.get("TITLE") or ""),
                    "stage_id": str(deal.get("STAGE_ID") or ""),
                    "assigned_by_id": _coerce_int(deal.get("ASSIGNED_BY_ID")),
                    "date_create": str(deal.get("DATE_CREATE") or ""),
                    "opportunity": _coerce_float(deal.get("OPPORTUNITY")),
                    "category_id": category_id,
                    "timeline": timeline,
                    # Пользовательские поля — все UF_*
                    "uf_fields": {
                        k: v for k, v in deal.items()
                        if k.startswith("UF_") and v is not None
                    },
                }
            )
        return {
            "deals": result,
            "category_id": category_id,
            "total": len(result),
        }
    except Exception as exc:
        logger.exception(
            "get_deals_by_funnel_with_timeline failed category_id=%s",
            category_id,
        )
        return {"error": str(exc), "deals": [], "category_id": category_id, "total": 0}
```

---

## Задача 3: Создать AuditState и 3 коллектора в `src/graph.py`

### 3a. Добавить AuditState (рядом с CRMState, НЕ заменять):

```python
class AuditState(TypedDict, total=False):
    """LangGraph state v2: multi-agent CRM audit."""

    raw_leads: list[dict[str, Any]]
    raw_buyers_deals: list[dict[str, Any]]
    raw_sellers_deals: list[dict[str, Any]]
    violations: list[dict[str, Any]]
    current_time: str
    dry_run: bool
    status: str
    messages: list[str]
```

### 3b. Добавить импорт новых инструментов — обновить from tools import:

Добавить в существующий импорт:
```python
from tools import (
    ...
    get_all_leads_with_timeline,
    get_deals_by_funnel_with_timeline,
)
```

### 3c. Три коллектора (pure Python, NO LLM):

```python
async def lead_collector(state: AuditState, settings: Settings) -> AuditState:
    """Agent 1: collect all leads + timeline."""
    logger.info("Agent 1 (Lead Collector): fetching all leads with timeline")

    data = get_all_leads_with_timeline.invoke({})
    leads = data.get("leads", []) if isinstance(data, dict) else []

    logger.info("Agent 1: collected %d leads", len(leads))

    messages = list(state.get("messages", []))
    messages.append(f"lead_collector: {len(leads)} leads")

    return {
        **state,
        "raw_leads": leads,
        "messages": messages,
    }


async def buyer_collector(state: AuditState, settings: Settings) -> AuditState:
    """Agent 2: collect buyer deals + timeline."""
    category_id = settings.buyers_category_id
    logger.info(
        "Agent 2 (Buyer Collector): fetching deals for category_id=%d",
        category_id,
    )

    data = get_deals_by_funnel_with_timeline.invoke({"category_id": category_id})
    deals = data.get("deals", []) if isinstance(data, dict) else []

    logger.info("Agent 2: collected %d buyer deals", len(deals))

    messages = list(state.get("messages", []))
    messages.append(f"buyer_collector: {len(deals)} deals (cat={category_id})")

    return {
        **state,
        "raw_buyers_deals": deals,
        "messages": messages,
    }


async def seller_collector(state: AuditState, settings: Settings) -> AuditState:
    """Agent 3: collect seller deals + timeline."""
    category_id = settings.sellers_category_id
    logger.info(
        "Agent 3 (Seller Collector): fetching deals for category_id=%d",
        category_id,
    )

    data = get_deals_by_funnel_with_timeline.invoke({"category_id": category_id})
    deals = data.get("deals", []) if isinstance(data, dict) else []

    logger.info("Agent 3: collected %d seller deals", len(deals))

    messages = list(state.get("messages", []))
    messages.append(f"seller_collector: {len(deals)} deals (cat={category_id})")

    return {
        **state,
        "raw_sellers_deals": deals,
        "messages": messages,
    }
```

---

## Задача 4: build_graph_v2() в `src/graph.py`

```python
def build_graph_v2(settings: Settings):
    """Build v2 audit graph: 3 parallel collectors.

    Args:
        settings: Application settings.

    Returns:
        Compiled LangGraph runnable.
    """
    graph = StateGraph(AuditState)

    async def lead_col(state: AuditState) -> AuditState:
        return await lead_collector(state, settings)

    async def buyer_col(state: AuditState) -> AuditState:
        return await buyer_collector(state, settings)

    async def seller_col(state: AuditState) -> AuditState:
        return await seller_collector(state, settings)

    graph.add_node("lead_collector", lead_col)
    graph.add_node("buyer_collector", buyer_col)
    graph.add_node("seller_collector", seller_col)

    # Parallel from START
    graph.add_edge(START, "lead_collector")
    graph.add_edge(START, "buyer_collector")
    graph.add_edge(START, "seller_collector")

    # All → END
    graph.add_edge("lead_collector", END)
    graph.add_edge("buyer_collector", END)
    graph.add_edge("seller_collector", END)

    return graph.compile()
```

---

## Задача 5: run_audit_v2() в `src/graph.py`

```python
async def run_audit_v2(settings: Settings) -> AuditState:
    """Run v2 audit: 3 collectors (no LLM yet).

    Args:
        settings: Application settings.

    Returns:
        Final AuditState with raw_leads, raw_buyers_deals, raw_sellers_deals.
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

---

## Задача 6: Обновить `src/main.py` — флаг `--v2`

Замени функцию `main()`:

```python
async def main(use_v2: bool = True) -> None:
    """Load config, run audit pipeline, log result.

    Args:
        use_v2: If True, run v2 graph (3 collectors).
                If False, run v1 graph (Auditor → Analyst → Dispatcher).
    """
    settings = get_settings()
    setup_logging(settings.log_level)

    if use_v2:
        logger.info(
            "Starting b24-ai-auditor V2 (DRY_RUN=%s, BUYERS_CAT=%d, SELLERS_CAT=%d)",
            settings.dry_run,
            settings.buyers_category_id,
            settings.sellers_category_id,
        )
        result = await run_audit_v2(settings)
        logger.info(
            "Audit V2 finished: leads=%d, buyer_deals=%d, seller_deals=%d",
            len(result.get("raw_leads", [])),
            len(result.get("raw_buyers_deals", [])),
            len(result.get("raw_sellers_deals", [])),
        )
    else:
        logger.info(
            "Starting b24-ai-auditor V1 (DRY_RUN=%s, B24_USER_ID=%s)",
            settings.dry_run,
            settings.b24_user_id,
        )
        result = await run_audit(settings, deal_ids=[])
        messages = result.get("messages", [])
        violations = result.get("violations", [])
        logger.info(
            "Audit V1 finished: status=%s, steps=%d, violations=%d",
            result.get("status"),
            len(messages),
            len(violations),
        )
```

И обнови `cli()` — замени `asyncio.run(main())` на `asyncio.run(main(use_v2=True))`.

---

## Проверка

После внесения изменений:
1. `.\make.cmd lint` — должен пройти
2. `.\make.cmd dry-run` — запустит v2, 3 коллектора параллельно, без LLM
3. Проверить `logs/audit.log` — должны быть строки: `lead_collector: N leads`, `buyer_collector: N deals`, `seller_collector: N deals`
