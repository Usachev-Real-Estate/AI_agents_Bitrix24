# step-53-fix-owner-all-brokers.md

## Контекст

Сейчас [`src/owner_tracker.py`](src/owner_tracker.py) показывает только брокеров, у которых есть хотя бы 1 контакт-собственник. Брокеры с 0 собственников не видны в отчёте.

Нужно показывать ВСЕХ брокеров, включая тех, у кого 0.

## Задача

Добавить в отчёт брокеров с 0 собственников (категория «Отстают»).

---

## Шаг 1 — Получить список ВСЕХ брокеров

### Где

Файл: `src/owner_tracker.py`, функция `async_main()`.

### Как

Взять список брокеров из существующей таблицы `brokers` в `data/violations.db` (она обновляется при каждом CRM-аудите через `upsert_brokers`). Либо запросить через Bitrix24 API.

**Вариант А (рекомендуемый) — через БД:**

Добавить импорт и вызов в `async_main()` после строки 234:

```python
from db import get_connection

# Получить ВСЕХ активных брокеров из БД
all_broker_ids = set()
with get_connection() as conn:
    rows = conn.execute(
        "SELECT responsible_id FROM brokers WHERE lead_count > 0 OR deal_count > 0"
    ).fetchall()
    all_broker_ids = {row[0] for row in rows}
```

Затем добавить брокеров с 0 собственников в `broker_groups` (перед вызовом `classify_brokers`):

```python
# Добавить брокеров с 0 собственников
for bid in all_broker_ids:
    if bid not in broker_groups:
        broker_groups[bid] = []
```

Также нужно дополнить `user_info` для этих брокеров (если их нет в `user_ids`):

```python
missing_ids = all_broker_ids - user_ids
if missing_ids:
    missing_info = await fetch_user_names(bx, missing_ids)
    user_info.update(missing_info)
```

---

## Шаг 2 — Обработать случай 0 собственников в `format_kpi_report`

### Где

Файл: `src/owner_tracker.py`, функция `format_kpi_report`, вложенная `add_broker_section`.

### Текущий код (строки 168, 172-183)

При 0 собственников:
- `plan_text` = `" (0% плана, осталось {target})"` — ок
- Цикл `for contact in contacts_sorted` не выполнится (контактов нет) — ок
- Но строка с именем брокера всё равно выведется — ок

Проблем нет, ноль обработается корректно. Но стоит добавить визуальный индикатор:

### Новый код (добавить после строки 170, перед циклом контактов)

```python
            if count == 0:
                lines.append(f"   ⚠️ Ни одного собственника не добавлено")
```

---

## Шаг 3 — Обновить `user_info` для брокеров из БД

Функция `fetch_user_names` уже умеет получать имена по списку ID. Нужно просто передать ей полный список (включая брокеров с 0 собственников).

В `async_main()`, после того как получен `all_broker_ids`:

```python
# Получить имена для ВСЕХ брокеров (а не только тех, у кого есть контакты)
all_user_ids = all_broker_ids | set(broker_groups.keys())
user_info = await fetch_user_names(bx, all_user_ids)
```

Это заменит текущий вызов `fetch_user_names(bx, user_ids)` на строке 238.

---

## Итоговый diff для `async_main()`

```python
# После строки 238 (group_by_broker)
# ДОБАВИТЬ:

from db import get_connection

# Получить ВСЕХ активных брокеров из БД
all_broker_ids = set()
try:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT responsible_id FROM brokers WHERE lead_count > 0 OR deal_count > 0"
        ).fetchall()
        all_broker_ids = {row[0] for row in rows}
except Exception:
    pass

# Если БД недоступна — используем только тех, у кого есть контакты
if not all_broker_ids:
    all_broker_ids = set(broker_groups.keys())

# Добавить брокеров с 0 собственников
for bid in all_broker_ids:
    if bid not in broker_groups:
        broker_groups[bid] = []

# Заменить строку 237-238:
# Было: user_ids = set(broker_groups.keys())
#       user_info = await fetch_user_names(bx, user_ids)
# Стало:
user_info = await fetch_user_names(bx, all_broker_ids)
```

---

## Проверка

1. `python src/owner_tracker.py` с `DRY_RUN=true`
2. В отчёте должны появиться брокеры с `0 собственников` в секции «Отстают»
3. У них должна быть строка `⚠️ Ни одного собственника не добавлено`
4. Общее количество брокеров должно увеличиться
