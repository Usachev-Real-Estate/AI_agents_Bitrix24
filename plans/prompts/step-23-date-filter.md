# Промпт для Cursor — Фильтр по дате: отчёт с 29.05.2026

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `.env`, `src/config.py`, `src/tools.py`.

---

## Задача 1: Добавить `REPORT_SINCE` в `.env` и `config.py`

### 1a. `.env` — добавить:
```ini
# Дата начала отчётного периода (YYYY-MM-DD)
REPORT_SINCE=2026-05-29
```

### 1b. `src/config.py` — добавить поле в Settings:
```python
report_since: str = Field(default="", validation_alias="REPORT_SINCE")
```

---

## Задача 2: Добавить фильтр по дате в `src/tools.py`

### 2a. `get_all_leads_with_timeline` — добавить фильтр `>=DATE_CREATE`:

В параметрах `bx.get_all("crm.lead.list", ...)` добавить в filter:
```python
from config import get_settings

# Внутри функции, после bx = _get_bitrix():
settings = get_settings()
filter_params = {
    "select": [...],
}
if settings.report_since:
    filter_params["filter"] = {
        ">=DATE_CREATE": settings.report_since,
    }
```

Т.е. заменить жёсткий `"select": [...]` на условный фильтр.

### 2b. `get_deals_by_funnel_with_timeline` — аналогично:

```python
settings = get_settings()
filter_params: dict[str, Any] = {
    "filter": {
        "CATEGORY_ID": category_id,
        "CLOSED": "N",
    },
    "select": [...],
}
if settings.report_since:
    filter_params["filter"][">=DATE_CREATE"] = settings.report_since
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — только лиды/сделки созданные с 29.05.2026
