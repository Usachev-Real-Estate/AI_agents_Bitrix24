# Промпт для Cursor — Маршрутизация отчётов по чатам РОПов

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`.

---

## Контекст

Step-44 выполнен: seller_collector → merge восстановлен, звонковые блоки убраны, дата `REPORT_SINCE=2026-06-02`, crontab на 10:00/17:00 МСК.

Теперь нужно маршрутизировать отчёты по отделам в чаты РОПов (вместо общего чата 22358).

**Маппинг ID отдела → chat_id РОПа (получен в Шаге 0):**

| ID отдела | РОП | chat_id |
|-----------|-----|---------|
| 60 | Кретов | 17710 |
| 46 | Горяинов | 17712 |
| 42 | Каратевский | 17716 |
| 44 | Трофимова | 17708 |
| 50 | Волкова | 17714 |

**Правила маршрутизации:**
- Отчёт по отделу → в чат соответствующего РОПа
- Отдел без РОПа (HR, Бэк-офис, ТО, etc.) → **пропустить** (не отправлять)
- Сводный отчёт (summary) → в общий чат `REPORT_CHAT_ID = 22358`

---

## Задача: 4 изменения в `src/graph.py`

### Изменение 1 — Добавить константу `DEPT_CHAT_MAP`

Вставить после строки 30 (после `REPORT_CHAT_ID = 22358`):

```python
# БЫЛО:
REPORT_CHAT_ID = 22358

# СТАЛО:
REPORT_CHAT_ID = 22358

DEPT_CHAT_MAP: dict[int, int] = {
    60: 17710,  # Кретов
    46: 17712,  # Горяинов
    42: 17716,  # Каратевский
    44: 17708,  # Трофимова
    50: 17714,  # Волкова
}
```

---

### Изменение 2 — `_build_user_map` возвращает ещё `dept_id_map`

Функция `_build_user_map` сейчас возвращает `dict[int, str]`. Нужно, чтобы она возвращала ещё `dict[int, int]` — маппинг `user_id → department_id`.

**Сигнатура:**
```python
# БЫЛО:
async def _build_user_map(...) -> dict[int, str]:

# СТАЛО:
async def _build_user_map(...) -> tuple[dict[int, str], dict[int, int]]:
```

**Внутри функции** — после определения `dept_name`, сохранить `dept_id`:

В цикле `for uid in user_ids:` (строка 459), там где сейчас определяется `dept_name` из `dept_raw`, нужно сохранять `dept_id_int`. Найти место, где `dept_name` присваивается, и добавить сохранение `dept_id`.

Конкретно: переменная `dept_id` уже используется в цикле `for dept_id in dept_raw:` (строка 487). Нужно сохранить **последний успешный** `dept_id` в словарь `dept_id_map`.

Добавить в начало функции:
```python
dept_id_map: dict[int, int] = {}
```

В блоке, где `dept_name` присвоен (после строки 502 или 506), добавить:
```python
dept_id_map[uid] = int(dept_id) if isinstance(dept_id, (int, str)) else 0
```

В конце функции, вместо:
```python
return user_map
```
Вернуть:
```python
return user_map, dept_id_map
```

**Важно:** переменная `dept_id` в цикле `for dept_id in dept_raw` (строка 487) перезаписывается. Сохранять нужно после успешного получения `dept_name`. В блоке `if dname:` (строка 501) переменная `dept_id` ещё доступна.

---

### Изменение 3 — `report_dispatcher` использует `dept_id_map`

В строке 675 сейчас:
```python
user_map = await _build_user_map(
    violations,
    state.get("raw_leads", []),
    state.get("raw_buyers_deals", []),
    state.get("raw_sellers_deals", []),
)
```

Заменить на:
```python
user_map, dept_id_map = await _build_user_map(
    violations,
    state.get("raw_leads", []),
    state.get("raw_buyers_deals", []),
    state.get("raw_sellers_deals", []),
)
```

---

### Изменение 4 — Отправка отчёта по отделу в чат РОПа

В цикле `for dept_name in sorted(dept_groups):` (строка 698) нужно:

1. Определить `dept_id` для отдела (по первому сотруднику с нарушениями):
```python
# Определить department_id для этого отдела
dept_id = None
for v in dept_violations:
    uid = _coerce_int(v.get("responsible_id", 0))
    did = dept_id_map.get(uid)
    if did and did in DEPT_CHAT_MAP:
        dept_id = did
        break

# Если отдел не привязан к РОПу — пропускаем
if dept_id is None:
    logger.info(
        "Dispatcher: skipping dept '%s' (no ROP chat mapping)",
        dept_name,
    )
    continue

rop_chat_id = DEPT_CHAT_MAP[dept_id]
```

Вставить этот блок **перед** `lines = [...]` (строка 740).

2. Заменить `REPORT_CHAT_ID` на `rop_chat_id` в вызовах `send_chat_message_chunked` для отчёта по отделу:

В строке 788:
```python
# БЫЛО:
chunks = send_chat_message_chunked(REPORT_CHAT_ID, report)

# СТАЛО:
chunks = send_chat_message_chunked(rop_chat_id, report)
```

И в лог-строке 790-795 заменить `REPORT_CHAT_ID` на `rop_chat_id`:
```python
# БЫЛО:
logger.info(
    "Dispatcher: dept '%s' report sent to chat %d (%d violations, %d chunks)",
    dept_name,
    REPORT_CHAT_ID,
    len(dept_violations),
    chunks,
)

# СТАЛО:
logger.info(
    "Dispatcher: dept '%s' report sent to ROP chat %d (%d violations, %d chunks)",
    dept_name,
    rop_chat_id,
    len(dept_violations),
    chunks,
)
```

---

### Важно: сводка остаётся в общем чате

Сводный отчёт (строки 687-696) продолжает отправляться в `REPORT_CHAT_ID = 22358` — **не менять**.

---

## Проверка

1. `.\make.cmd lint` — не должно быть ошибок.
2. `.\make.cmd dry-run` — проверить в логах:
   - `dept_id_map` собирается корректно (user_id → department_id)
   - Для каждого РОПа: `report sent to ROP chat 17710/17712/17716/17708/17714`
   - Отделы без РОПа: `skipping dept '...' (no ROP chat mapping)`
   - Сводка уходит в `chat 22358`
   - Нет попыток отправки в несуществующие чаты
