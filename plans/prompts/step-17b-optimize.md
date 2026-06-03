# Промпт для Cursor — v2 Оптимизация: параллельный таймлайн (ThreadPoolExecutor)

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`.

**Суть:** заменить последовательный цикл `for entity in entities: fetch_timeline()` на параллельный `ThreadPoolExecutor` с 10 воркерами. ~10x ускорение без потери данных.

---

## Задача 1: Добавить константу и хелпер в `src/tools.py`

### 1a. Добавить константу в начало файла (после `SUCCESSFUL_CALL_FAILED_CODE`):

```python
MAX_TIMELINE_WORKERS = 10
```

### 1b. Добавить функцию `_fetch_entity_timeline` (в секцию хелперов, перед инструментами):

```python
def _fetch_entity_timeline(
    entity_id: int,
    entity_type: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Fetch timeline for a single entity (lead or deal).

    Designed for use with ThreadPoolExecutor — creates its own
    Bitrix client per call for thread safety.

    Args:
        entity_id: CRM entity ID (lead or deal).
        entity_type: "lead" or "deal".

    Returns:
        Tuple of (entity_id, timeline_comments_list).
    """
    try:
        bx = _get_bitrix()
        raw = bx.get_all(
            "crm.timeline.comment.list",
            {
                "filter": {
                    "ENTITY_ID": entity_id,
                    "ENTITY_TYPE": entity_type,
                },
                "select": ["ID", "AUTHOR_ID", "COMMENT", "CREATED"],
            },
        )
        return (entity_id, _extract_comments(raw))
    except Exception:
        logger.debug(
            "Timeline fetch failed for %s id=%s",
            entity_type,
            entity_id,
        )
        return (entity_id, [])
```

---

## Задача 2: Обновить T1 (`get_all_leads_with_timeline`)

Заменить ВЕСЬ блок после `leads = bx.get_all("crm.lead.list", ...)` и до `return {"leads": result, ...}`.

**Было** (последовательный цикл):
```python
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

            result.append({...})
```

**Стало** (параллельный ThreadPoolExecutor):
```python
        # Build base records (without timeline)
        lead_records: dict[int, dict[str, Any]] = {}
        for lead in leads:
            if not isinstance(lead, dict):
                continue
            lead_id = _coerce_int(lead.get("ID"))
            lead_records[lead_id] = {
                "lead_id": lead_id,
                "title": str(lead.get("TITLE") or ""),
                "status_id": str(lead.get("STATUS_ID") or ""),
                "assigned_by_id": _coerce_int(lead.get("ASSIGNED_BY_ID")),
                "date_create": str(lead.get("DATE_CREATE") or ""),
                "comments_field": str(lead.get("COMMENTS") or ""),
                "source_id": str(lead.get("SOURCE_ID") or ""),
                "timeline": [],
            }

        # Parallel timeline fetch
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_entity_timeline, lid, "lead"): lid
                for lid in lead_records
            }
            for future in as_completed(futures):
                lid = futures[future]
                try:
                    _, timeline = future.result()
                    lead_records[lid]["timeline"] = timeline
                except Exception:
                    pass

        result = list(lead_records.values())
```

---

## Задача 3: Обновить T2 (`get_deals_by_funnel_with_timeline`)

Аналогично T1 — заменить последовательный цикл на параллельный.

**Было** (последовательный цикл):
```python
        result: list[dict[str, Any]] = []
        for deal in deals:
            if not isinstance(deal, dict):
                continue
            deal_id = _coerce_int(deal.get("ID"))
            # Fetch timeline
            try:
                timeline_raw = bx.get_all(...)
                timeline = _extract_comments(timeline_raw)
            except Exception:
                timeline = []

            result.append({...})
```

**Стало** (параллельный ThreadPoolExecutor):
```python
        # Build base records (without timeline)
        deal_records: dict[int, dict[str, Any]] = {}
        for deal in deals:
            if not isinstance(deal, dict):
                continue
            deal_id = _coerce_int(deal.get("ID"))
            deal_records[deal_id] = {
                "deal_id": deal_id,
                "title": str(deal.get("TITLE") or ""),
                "stage_id": str(deal.get("STAGE_ID") or ""),
                "assigned_by_id": _coerce_int(deal.get("ASSIGNED_BY_ID")),
                "date_create": str(deal.get("DATE_CREATE") or ""),
                "opportunity": _coerce_float(deal.get("OPPORTUNITY")),
                "category_id": category_id,
                "timeline": [],
                "uf_fields": {
                    k: v for k, v in deal.items()
                    if k.startswith("UF_") and v is not None
                },
            }

        # Parallel timeline fetch
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=MAX_TIMELINE_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_entity_timeline, did, "deal"): did
                for did in deal_records
            }
            for future in as_completed(futures):
                did = futures[future]
                try:
                    _, timeline = future.result()
                    deal_records[did]["timeline"] = timeline
                except Exception:
                    pass

        result = list(deal_records.values())
```

---

## Проверка

После внесения изменений:
1. `.\make.cmd lint` — должен пройти
2. `.\make.cmd dry-run` — должен отработать **значительно быстрее** (~1.5–2 мин вместо 14 мин)
3. Количество лидов/сделок — такое же (669, 553, 565)
