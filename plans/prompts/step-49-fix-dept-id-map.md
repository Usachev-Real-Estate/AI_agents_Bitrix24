# step-49-fix-dept-id-map.md

## Контекст

При интеграции `src/db.py` в `src/graph.py` (недельный отчёт по брокерам) в двух вызовах пропущен аргумент `dept_id_map`. Это вызовет `TypeError` при запуске аудита.

Также в `src/db.py` в функции `upsert_brokers` есть лишнее дублирование значений в `records`.

## Задача

Исправить 2 критических бага в `src/graph.py` и 1 код-смелл в `src/db.py`.

---

## Шаг 1 — Исправить вызов `save_violations` в `src/graph.py`

### Где

Файл: `src/graph.py`, функция `report_dispatcher`, примерно строка 731.

### Текущий код

```python
save_violations(run_id, violations, user_map, now)
```

### Новый код

```python
save_violations(run_id, violations, user_map, dept_id_map, now)
```

**Причина:** сигнатура `save_violations` в [`db.py:83`](src/db.py:83) требует 5 аргументов:
```python
def save_violations(audit_run_id, violations, user_map, dept_id_map, now)
```

Переменная `dept_id_map` уже доступна в `report_dispatcher` — она получена на строке 597:
```python
user_map, dept_id_map = await _build_user_map(...)
```

---

## Шаг 2 — Исправить вызов `upsert_brokers` в `src/graph.py`

### Где

Файл: `src/graph.py`, функция `report_dispatcher`, примерно строки 732-738.

### Текущий код

```python
upsert_brokers(
    user_map, 
    raw_leads, 
    buyers_deals, 
    state.get("raw_sellers_deals", []), 
    now
)
```

### Новый код

```python
upsert_brokers(
    user_map,
    dept_id_map,
    raw_leads, 
    buyers_deals, 
    state.get("raw_sellers_deals", []), 
    now
)
```

**Причина:** сигнатура `upsert_brokers` в [`db.py:126`](src/db.py:126) требует 6 аргументов:
```python
def upsert_brokers(user_map, dept_id_map, leads, buyer_deals, seller_deals, now)
```

---

## Шаг 3 — Убрать дублирование значений в `upsert_brokers` (src/db.py)

### Где

Файл: `src/db.py`, функция `upsert_brokers`, примерно строки 158-176.

### Текущий код (строки 158-176)

```python
    records = []
    for rid, stats in broker_stats.items():
        records.append((
            rid, stats["name"], stats["dept"], stats["dept_id"], stats["leads"], stats["deals"], now,
            stats["name"], stats["dept"], stats["dept_id"], stats["leads"], stats["deals"], now
        ))
        
    with get_connection() as conn:
        conn.executemany("""
        INSERT INTO brokers (responsible_id, responsible_name, department, department_id, lead_count, deal_count, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(responsible_id) DO UPDATE SET
            responsible_name=excluded.responsible_name,
            department=excluded.department,
            department_id=excluded.department_id,
            lead_count=excluded.lead_count,
            deal_count=excluded.deal_count,
            last_seen=excluded.last_seen
        """, [r[:7] for r in records]) # Provide only first 7 values since ON CONFLICT uses excluded.*
```

### Новый код

```python
    records = []
    for rid, stats in broker_stats.items():
        records.append((
            rid, stats["name"], stats["dept"], stats["dept_id"], stats["leads"], stats["deals"], now,
        ))
        
    with get_connection() as conn:
        conn.executemany("""
        INSERT INTO brokers (responsible_id, responsible_name, department, department_id, lead_count, deal_count, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(responsible_id) DO UPDATE SET
            responsible_name=excluded.responsible_name,
            department=excluded.department,
            department_id=excluded.department_id,
            lead_count=excluded.lead_count,
            deal_count=excluded.deal_count,
            last_seen=excluded.last_seen
        """, records)
```

**Причина:** `ON CONFLICT DO UPDATE SET ... = excluded.column` использует те же значения, что были переданы в INSERT. Дублировать их не нужно — `excluded.*` автоматически ссылается на значения из `VALUES (...)`. Убираем дублирование и срез `[r[:7] for r in records]`.

---

## Проверка

После исправлений:

1. `python -m flake8 src/graph.py src/db.py` — без ошибок
2. `python src/main.py` — не должен падать с `TypeError` при сохранении в БД
3. `python src/weekly_report.py` — должен формировать отчёт (при наличии данных в БД)
