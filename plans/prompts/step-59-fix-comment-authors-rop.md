# step-59-fix-comment-authors-rop.md

## Проблема

Сейчас:
- **Правила 2, 3, 4**: `_days_since_last_any_comment` — учитывает комментарий от **ЛЮБОГО** автора (даже чужого)
- **Правило 5**: `_days_since_last_comment(timeline, assigned_by_id)` — только от ответственного брокера

## Требование

Комментарий должен считаться только если он от:
- Ответственного брокера (`assigned_by_id`)
- **ИЛИ** РОПа этого брокера (Руководитель отдела продаж)

## Решение

1. Заменить `_days_since_last_any_comment` на `_days_since_last_comment_by_authors(timeline, now, allowed_ids)`
2. В `check_buyer_deal_violations` для каждой сделки определять `allowed_ids = {broker_id, rop_id}`
3. РОП определяется по `WORK_POSITION = "Руководитель отдела продаж (РОП)"`

---

## Шаг 1 — Новая функция `_days_since_last_comment_by_authors`

**Файл:** `src/tools.py`, после `_days_since_last_any_comment` (~строка 255).

```python
def _days_since_last_comment_by_authors(
    timeline: list[dict[str, Any]],
    current: datetime,
    allowed_author_ids: set[int],
) -> int:
    """Days since last comment from any of the allowed authors, or 999 if none."""
    authored = [
        item for item in timeline
        if _coerce_int(item.get("author_id")) in allowed_author_ids
        and str(item.get("comment") or "").strip()
    ]
    if not authored:
        return 999
    latest = max(authored, key=lambda item: str(item.get("created") or ""))
    created = _parse_datetime(latest.get("created"))
    if created is None:
        return 999
    return int(_days_between(created, current))
```

---

## Шаг 2 — Функция для получения РОП-маппинга

**Файл:** `src/tools.py` (в любое место, перед `check_buyer_deal_violations`).

```python
def _build_rop_map() -> dict[int, int]:
    """Build department_id → ROP user_id mapping.
    
    Fetches all users with WORK_POSITION = 'Руководитель отдела продаж (РОП)'.
    Returns dict: {dept_id: rop_user_id}
    """
    settings = get_settings()
    bx = _get_bitrix()
    try:
        rop_users = _bx_get_all_sync("user.get", {
            "FILTER": {
                "WORK_POSITION": "Руководитель отдела продаж (РОП)",
                "ACTIVE": True,
            }
        })
        rop_map: dict[int, int] = {}
        for user in rop_users:
            if not isinstance(user, dict):
                continue
            uid = _coerce_int(user.get("ID"))
            depts = user.get("UF_DEPARTMENT", [])
            if isinstance(depts, list) and depts:
                dept_id = _coerce_int(depts[0])
                if dept_id:
                    rop_map[dept_id] = uid
        return rop_map
    except Exception:
        logger.warning("Failed to build ROP map, ROP comments won't be counted")
        return {}
```

---

## Шаг 3 — Изменить сигнатуру `check_buyer_deal_violations`

**Файл:** `src/tools.py`, функция `check_buyer_deal_violations`.

### Текущая сигнатура

```python
def check_buyer_deal_violations(
    deals: list[dict[str, Any]],
    current_time: str,
) -> list[dict[str, Any]]:
```

### Новая сигнатура

```python
def check_buyer_deal_violations(
    deals: list[dict[str, Any]],
    current_time: str,
    rop_map: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
```

### В начале функции добавить

```python
    if rop_map is None:
        rop_map = _build_rop_map()
```

---

## Шаг 4 — Определять `allowed_ids` для каждой сделки

В цикле `for deal in deals:` после получения `assigned_by_id`:

```python
        # Определить, чьи комментарии считаются валидными
        broker_id = assigned_by_id
        allowed_comment_authors = {broker_id}
        # Найти РОПа этого брокера (по отделу брокера)
        if rop_map:
            # Ищем отдел брокера через user.get (может быть кеширован)
            # Пока упрощённо: перебираем dept→rop, но нужен broker→dept
            pass
```

**Проблема:** сделка не содержит ID отдела брокера. Нужно получить его.

**Решение:** построить `broker_dept_map` перед циклом.

---

## Шаг 5 — Построить `broker_dept_map` перед циклом

В `check_buyer_deal_violations`, перед циклом `for deal in deals:`:

```python
    # Собрать уникальных брокеров
    broker_ids = set()
    for deal in deals:
        bid = _coerce_int(deal.get("assigned_by_id"))
        if bid:
            broker_ids.add(bid)
    
    # Получить отделы брокеров
    broker_dept_map: dict[int, int] = {}
    if broker_ids and rop_map:
        try:
            bx = _get_bitrix()
            for bid in broker_ids:
                user_raw = _bx_get_all_sync("user.get", {"ID": bid})
                user = user_raw[0] if isinstance(user_raw, list) and user_raw else user_raw
                if isinstance(user, dict):
                    depts = user.get("UF_DEPARTMENT", [])
                    if isinstance(depts, list) and depts:
                        broker_dept_map[bid] = _coerce_int(depts[0])
        except Exception:
            logger.warning("Failed to load broker departments for ROP check")
```

Затем в цикле по сделкам:

```python
        # Определить валидных авторов комментариев
        allowed_comment_authors = {broker_id}
        broker_dept = broker_dept_map.get(broker_id)
        if broker_dept and rop_map:
            rop_id = rop_map.get(broker_dept)
            if rop_id:
                allowed_comment_authors.add(rop_id)
```

---

## Шаг 6 — Заменить вызовы в правилах 2, 3, 4, 5

### Правило 2 (строка 437)

**Было:**
```python
days_since_comment = _days_since_last_any_comment(timeline, now)
```

**Стало:**
```python
days_since_comment = _days_since_last_comment_by_authors(
    timeline, now, allowed_comment_authors
)
```

### Правило 3 (строка 449)

**Было:**
```python
days_since_comment = _days_since_last_any_comment(timeline, now)
```

**Стало:**
```python
days_since_comment = _days_since_last_comment_by_authors(
    timeline, now, allowed_comment_authors
)
```

### Правило 4 (строка 461)

**Было:**
```python
days_since_comment = _days_since_last_any_comment(timeline, now)
```

**Стало:**
```python
days_since_comment = _days_since_last_comment_by_authors(
    timeline, now, allowed_comment_authors
)
```

### Правило 5 (строка 473)

**Было:**
```python
days_since = _days_since_last_comment(timeline, assigned_by_id, now)
```

**Стало:**
```python
days_since = _days_since_last_comment_by_authors(
    timeline, now, allowed_comment_authors
)
```

---

## Шаг 7 — Обновить вызов в `buyer_deal_analyst` (graph.py)

**Файл:** `src/graph.py`, функция `buyer_deal_analyst` (~строка 248).

Сейчас:
```python
all_violations = check_buyer_deal_violations(deals, current_time)
```

Изменить на:
```python
from tools import _build_rop_map
rop_map = _build_rop_map()
all_violations = check_buyer_deal_violations(deals, current_time, rop_map)
```

---

## Шаг 8 — Обновить тесты

**Файл:** `tests/test_buyer_audit.py`.

В вызовы `check_buyer_deal_violations` можно не передавать `rop_map` — параметр опциональный. Но добавить тест с мок-РОПом:

```python
def test_rule2_rop_comment_counts():
    """Комментарий от РОПа засчитывается."""
    # РОП = user 200 для отдела 0 (дефолтный отдел тестовой сделки)
    deal = _deal(
        audit_rule=2,
        assigned_by_id=100,
        timeline=[
            {"author_id": 200, "comment": "Проверил", "created": "2026-06-07T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations(
        [deal], CURRENT,
        rop_map={0: 200},  # отдел 0 → РОП 200
    )
    assert violations == []  # комментарий РОПа засчитан
```

---

## Проверка

```bash
python -m pytest tests/test_buyer_audit.py -v
python -m flake8 src/tools.py src/graph.py
```
