# Промпт для Cursor — Этап 9: get_all() вместо call() для .list-методов

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`

---

## Проблема

`fast_bitrix24` выдаёт предупреждение:
> It's better to use get_all() with methods that end with .list

Сейчас `bx.call()` возвращает только **первую страницу** (50 записей). При большом количестве сделок/лидов часть данных теряется. `bx.get_all()` автоматически подгружает **все страницы** и возвращает полный список.

---

## Задача 1: Исправить `get_active_deals()`

В файле `src/tools.py`, функция `get_active_deals` (начинается с `@tool` и `def get_active_deals`).

**Было** (строки 407–428):
```python
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
                    "ID",
                    "TITLE",
                    "STAGE_ID",
                    "ASSIGNED_BY_ID",
                    "DATE_CREATE",
                    "OPPORTUNITY",
                ],
                "order": {"DATE_CREATE": "DESC"},
                "start": 0,
            },
        )
        deals = _as_list(raw)
```

**Замени на**:
```python
    try:
        bx = _get_bitrix()
        deals = bx.get_all(
            "crm.deal.list",
            {
                "filter": {
                    "!STAGE_ID": ["C2:WON", "C2:LOSE"],
                    "CLOSED": "N",
                },
                "select": [
                    "ID",
                    "TITLE",
                    "STAGE_ID",
                    "ASSIGNED_BY_ID",
                    "DATE_CREATE",
                    "OPPORTUNITY",
                ],
                "order": {"DATE_CREATE": "DESC"},
            },
        )
```

Изменения:
- `bx.call(` → `bx.get_all(`
- `raw = bx.get_all(...)` → `deals = bx.get_all(...)` (get_all возвращает список сразу)
- Убран `"start": 0` (get_all сам управляет пагинацией)
- Убрана строка `deals = _as_list(raw)` (больше не нужна)

Остаток функции (цикл `for deal in deals[:limit]:`) остаётся без изменений.

---

## Задача 2: Исправить `check_lead_qualification()`

В файле `src/tools.py`, функция `check_lead_qualification` (начинается с `@tool` и `def check_lead_qualification`).

**Было** (строки 268–277):
```python
    try:
        bx = _get_bitrix()
        raw = bx.call(
            "crm.lead.list",
            {
                "filter": {"STATUS_ID": "NEW"},
                "select": ["ID", "TITLE", "DATE_CREATE", "STATUS_ID", "ASSIGNED_BY_ID"],
            },
        )
        leads = _as_list(raw)
```

**Замени на**:
```python
    try:
        bx = _get_bitrix()
        leads = bx.get_all(
            "crm.lead.list",
            {
                "filter": {"STATUS_ID": "NEW"},
                "select": ["ID", "TITLE", "DATE_CREATE", "STATUS_ID", "ASSIGNED_BY_ID"],
            },
        )
```

Изменения:
- `bx.call(` → `bx.get_all(`
- `raw = bx.get_all(...)` → `leads = bx.get_all(...)` (get_all возвращает список сразу)
- Убрана строка `leads = _as_list(raw)` (больше не нужна)

Остаток функции (цикл `for lead in leads:`) остаётся без изменений.

---

## Проверка

После внесения изменений:
1. `.\make.cmd lint` — должен пройти
2. `.\make.cmd dry-run` — UserWarning про `get_all()` должен исчезнуть, все сделки/лиды должны загрузиться полностью
