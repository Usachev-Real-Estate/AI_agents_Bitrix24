# Промпт для Cursor — Улучшенный формат отчёта (ссылки + ответственный + подразделение)

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py` (только `report_dispatcher`), `src/tools.py` (добавить хелпер).

---

## Задача 1: Добавить хелпер `_build_crm_link` в `src/tools.py`

Добавить функцию ПЕРЕД инструментами (в секцию хелперов):

```python
def _build_crm_link(entity_type: str, entity_id: int) -> str:
    """Build Bitrix24 CRM link for a lead or deal.

    Args:
        entity_type: "lead" or "deal".
        entity_id: CRM entity ID.

    Returns:
        Full URL to CRM entity card.
    """
    settings = get_settings()
    # Extract domain from webhook: https://domain.bitrix24.ru/rest/...
    url = settings.b24_webhook_url
    domain = url.split("/rest/")[0] if "/rest/" in url else url.rstrip("/")

    if entity_type == "lead":
        return f"{domain}/crm/lead/details/{entity_id}/"
    return f"{domain}/crm/deal/details/{entity_id}/"
```

---

## Задача 2: Обновить импорт в `src/graph.py`

Добавить импорт `_build_crm_link`:

```python
from tools import (
    ...,
    _build_crm_link,
)
```

Если `_build_crm_link` не импортируется (она без @tool), добавить в `tools.py` публичный импорт:

Убедись что функция `_build_crm_link` доступна для импорта из `tools`. Если нужно — убери подчёркивание: `build_crm_link`.

---

## Задача 3: Переписать `report_dispatcher` с новым форматом

Заменить ВЕСЬ `report_dispatcher` на:

```python
async def report_dispatcher(state: AuditState, settings: Settings) -> AuditState:
    """Format violations with links, responsible, department and send to user #154."""
    violations = state.get("violations", [])
    logger.info("Dispatcher: formatting report for %d violations", len(violations))

    now = state.get("current_time", "")[:19]  # YYYY-MM-DDTHH:MM:SS

    # Build user cache from collected data
    user_map: dict[int, str] = {}
    for lead in state.get("raw_leads", []):
        uid = lead.get("assigned_by_id", 0)
        if uid and uid not in user_map:
            user_map[uid] = f"ID:{uid}"
    for deal in state.get("raw_buyers_deals", []):
        uid = deal.get("assigned_by_id", 0)
        if uid and uid not in user_map:
            user_map[uid] = f"ID:{uid}"
    for deal in state.get("raw_sellers_deals", []):
        uid = deal.get("assigned_by_id", 0)
        if uid and uid not in user_map:
            user_map[uid] = f"ID:{uid}"

    report_lines = [
        "b24-ai-auditor v2 — сводка нарушений",
        f"Дата: {now}",
        "",
    ]

    if not violations:
        report_lines.append("Нарушений не найдено.")
    else:
        very_high = [v for v in violations if v.get("severity") == "very high"]
        high = [v for v in violations if v.get("severity") == "high"]
        medium = [v for v in violations if v.get("severity") == "medium"]

        report_lines.append(f"Всего нарушений: {len(violations)}")
        report_lines.append(f"  🔴🔴 Критические: {len(very_high)}")
        report_lines.append(f"  🔴 Высокие: {len(high)}")
        report_lines.append(f"  🟡 Средние: {len(medium)}")
        report_lines.append("")

        # Sort: very high first, then high, then medium
        severity_order = {"very high": 0, "high": 1, "medium": 2}
        sorted_violations = sorted(
            violations,
            key=lambda v: severity_order.get(v.get("severity", "medium"), 99),
        )

        for i, v in enumerate(sorted_violations, 1):
            sev = v.get("severity", "?")
            icon = {"very high": "🔴🔴", "high": "🔴", "medium": "🟡"}.get(sev, "⚪")
            entity_type = v.get("entity_type", "?")
            entity_id = v.get("entity_id", 0)
            responsible = v.get("responsible_id", "?")
            rule = v.get("rule", "?")
            reason = v.get("reason", "—")

            link = _build_crm_link(entity_type, entity_id)

            report_lines.append(
                f"{i}. {icon} [{rule}] {entity_type.capitalize()} #{entity_id}"
            )
            report_lines.append(f"   Ответственный: #{responsible}")
            report_lines.append(f"   Нарушение: {reason}")
            report_lines.append(f"   Ссылка: {link}")
            report_lines.append("")

    # Summary line
    if violations:
        report_lines.append(
            f"Итого: {len(violations)} нарушений по {len(user_map)} сотрудникам."
        )

    report = "\n".join(report_lines)

    # Truncate to 2000 chars
    if len(report) > 2000:
        report = report[:1990] + "\n...[обрезано]"

    # Send to user #154
    try:
        from fast_bitrix24 import Bitrix
        bx = Bitrix(settings.b24_webhook_url)
        result = bx.call(
            "im.notify.personal.add",
            {
                "USER_ID": 154,
                "MESSAGE": report,
            },
        )
        logger.info("Dispatcher: report sent to user #154: %s", result)
    except Exception as exc:
        logger.exception("Dispatcher: failed to send report to user #154")

    messages = list(state.get("messages", []))
    messages.append(f"dispatcher: report sent ({len(violations)} violations)")

    return {
        **state,
        "messages": messages,
        "status": "completed",
    }
```

---

## Примечание по подразделениям

Поле «подразделение» (UF_DEPARTMENT) не загружается коллекторами. Для его получения нужно:
1. Добавить `user.get` с фильтром `UF_DEPARTMENT` в коллекторы
2. Или сделать отдельный инструмент `get_user_department(user_id)`

Пока в отчёте выводится `Ответственный: #ID`. Когда добавим подразделения — заменим на `Ответственный: Имя Фамилия (Отдел)`.

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — в уведомлении пользователя #154 должен быть формат:
```
1. 🔴 [buyer_stage_1] Deal #12345
   Ответственный: #154
   Нарушение: Первый контакт > 1 дня
   Ссылка: https://b24-xxx.bitrix24.ru/crm/deal/details/12345/
```
