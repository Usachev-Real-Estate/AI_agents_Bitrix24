# Промпт для Cursor — Имена и подразделения ответственных в отчёте

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py` (добавить `_build_user_map`, обновить `_format_and_send`).

**Проблема:** в отчёте `Ответственный: #154` — нужны Фамилия Имя + Подразделение.

**Решение:** собрать `user_map` из `user.get` для всех уникальных `responsible_id` из violations. Встроить имена в формат отчёта.

---

## Задача 1: Добавить `_build_user_map` в `src/graph.py`

Добавить ПЕРЕД `report_dispatcher`:

```python
async def _build_user_map(
    violations: list[dict],
    leads: list[dict],
    buyer_deals: list[dict],
    seller_deals: list[dict],
) -> dict[int, str]:
    """Fetch user names and departments for all responsible_id in violations.

    Args:
        violations: All violations from analysts.
        leads: Raw leads data.
        buyer_deals: Raw buyer deals data.
        seller_deals: Raw seller deals data.

    Returns:
        Dict mapping user_id → "LastName FirstName (Department)".
    """
    # Collect all unique user IDs
    user_ids: set[int] = set()

    for v in violations:
        uid = v.get("responsible_id", 0)
        if uid:
            user_ids.add(uid)

    # Also add from raw data (for entities without violations)
    for lead in leads:
        uid = lead.get("assigned_by_id", 0)
        if uid:
            user_ids.add(uid)
    for deal in buyer_deals:
        uid = deal.get("assigned_by_id", 0)
        if uid:
            user_ids.add(uid)
    for deal in seller_deals:
        uid = deal.get("assigned_by_id", 0)
        if uid:
            user_ids.add(uid)

    if not user_ids:
        return {}

    # Fetch user info
    from fast_bitrix24 import Bitrix
    from config import get_settings

    settings = get_settings()
    bx = Bitrix(settings.b24_webhook_url)
    user_map: dict[int, str] = {}

    for uid in user_ids:
        try:
            user = bx.call("user.get", {"ID": uid})
            if isinstance(user, dict) and user:
                first = str(user.get("NAME") or "")
                last = str(user.get("LAST_NAME") or "")
                name = f"{last} {first}".strip()
                if not name:
                    name = f"ID:{uid}"

                # Department
                dept_raw = user.get("UF_DEPARTMENT")
                if isinstance(dept_raw, list) and dept_raw:
                    dept = f"Отд.{dept_raw[0]}"
                elif dept_raw:
                    dept = f"Отд.{dept_raw}"
                else:
                    dept = ""

                display = f"{name} ({dept})" if dept else name
                user_map[uid] = display
            else:
                user_map[uid] = f"ID:{uid}"
        except Exception:
            logger.debug("Failed to fetch user %s", uid)
            user_map[uid] = f"ID:{uid}"

    logger.info("User map: fetched %d users", len(user_map))
    return user_map
```

---

## Задача 2: Обновить `report_dispatcher` — использовать `_build_user_map`

В `report_dispatcher`, ПЕРЕД форматированием отчётов, добавить:

```python
async def report_dispatcher(state: AuditState, settings: Settings) -> AuditState:
    """..."""
    if state.get("report_sent"):
        logger.info("Dispatcher: report already sent, skipping")
        return state

    violations = state.get("violations", [])
    logger.info("Dispatcher: formatting %d violations into 3 reports",
                len(violations))
    now = state.get("current_time", "")[:19]

    # Build user map (names + departments)
    user_map = await _build_user_map(
        violations,
        state.get("raw_leads", []),
        state.get("raw_buyers_deals", []),
        state.get("raw_sellers_deals", []),
    )

    # Split violations by type
    lead_violations = [v for v in violations if v.get("entity_type") == "lead"]
    buyer_violations = [v for v in violations if v.get("entity_type") == "deal"]
    seller_deals_count = len(state.get("raw_sellers_deals", []))

    # ... rest of the function, passing user_map to _format_and_send
```

---

## Задача 3: Обновить `_format_and_send` — принимать `user_map` и выводить имена

Обновить сигнатуру и формат:

```python
def _format_and_send(
    bx: Any,
    title: str,
    violations: list[dict],
    now: str,
    extra: str,
    entity_count: int,
    user_map: dict[int, str],
) -> int:
```

И в строке форматирования заменить:

**Было:**
```python
        line = (
            f"{icon} {entity_type[0].upper()}#{entity_id} "
            f"Отв:#{responsible} [{rule}] {reason}\n"
            f"   {link}"
        )
```

**Стало:**
```python
        user_display = user_map.get(responsible, f"ID:{responsible}")
        line = (
            f"{icon} {entity_type[0].upper()}#{entity_id} "
            f"{user_display} [{rule}] {reason}\n"
            f"   {link}"
        )
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — в отчёте вместо `Отв:#154` будет `Иванов Иван (Отд.5)`
