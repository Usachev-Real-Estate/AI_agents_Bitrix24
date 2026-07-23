# Промпт для Cursor — Упрощение правила этапа «Показ» (buyer_stage_3)

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`, `src/weekly_report.py`.

---

## Задача: Заменить сложную логику buyer_stage_3 на простое правило

**Суть:** На этапе «Показ» (C18:UC_UFPFKK) нужно контролировать **только комментарии**: каждые 7 дней в таймлайне должен быть комментарий длиной > 10 символов от брокера или его РОПа. Все старые проверки (UF-поля, activities, дела, даты показа) — убрать.

---

## Шаг 1 — Добавить `min_length` в `_days_since_last_comment_by_authors`

**Файл:** `src/tools.py`, строки 311–328

**Было:**
```python
def _days_since_last_comment_by_authors(
    timeline: list[dict[str, Any]],
    current: datetime,
    allowed_author_ids: set[int],
) -> int:
    """Days since last comment from allowed authors, or 999 if none."""
    authored = [
        item for item in timeline
        if _coerce_int(item.get("author_id")) in allowed_author_ids
        and str(item.get("comment") or "").strip()
    ]
```

**Стало:**
```python
def _days_since_last_comment_by_authors(
    timeline: list[dict[str, Any]],
    current: datetime,
    allowed_author_ids: set[int],
    min_length: int = 0,
) -> int:
    """Days since last comment from allowed authors, or 999 if none.

    Args:
        timeline: Timeline comment records.
        current: Reference datetime.
        allowed_author_ids: Authors whose comments count.
        min_length: Minimum comment length (default 0 = any non-empty).
    """
    authored = [
        item for item in timeline
        if _coerce_int(item.get("author_id")) in allowed_author_ids
        and len(str(item.get("comment") or "").strip()) > min_length
    ]
```

---

## Шаг 2 — Заменить блок `elif rule_num == 3:` полностью

**Файл:** `src/tools.py`, строки 774–841

Удалить ВЕСЬ блок `elif rule_num == 3:` (строки 774–841, включая все проверки uf_fields, show_date, activities, overdue_activities и т.д.) и заменить на:

```python
        elif rule_num == 3:
            days_since_comment = _days_since_last_comment_by_authors(
                timeline, now, allowed_comment_authors, min_length=10,
            )
            if days_since_comment > 7:
                if days_since_comment >= 999:
                    reason = (
                        f"На этапе «{stage_name}» нет комментария "
                        f"от брокера или РОПа."
                    )
                else:
                    reason = (
                        f"На этапе «{stage_name}» последний комментарий "
                        f"от брокера или РОПа более {days_since_comment} "
                        f"дней назад."
                    )
                violations.append(_violation(
                    deal,
                    "buyer_stage_3",
                    reason,
                    {
                        **base_details,
                        "days_since_last_comment": days_since_comment,
                    },
                    severity="high",
                ))
```

---

## Шаг 3 — Удалить мёртвые хелперы

**Файл:** `src/tools.py`

Эти функции использовались **только** rule_3 и больше нигде не вызываются. Удалить полностью:

### 3a. `_show_date_from_uf` (строки 389–391)
```python
# УДАЛИТЬ:
def _show_date_from_uf(uf_fields: dict[str, Any]) -> datetime | None:
    """Parse show/meeting date from labeled uf_fields."""
    return _parse_datetime(uf_fields.get("Дата встречи"))
```

### 3b. `_show_date_from_timeline_comments` (строки 394–456)
Удалить всю функцию вместе с `month_map` (~60 строк).

### 3c. `_has_show_plan_comment` (строки 459–474)
```python
# УДАЛИТЬ:
def _has_show_plan_comment(
    timeline: list[dict[str, Any]],
    allowed_author_ids: set[int] | None = None,
) -> bool:
    """True when timeline contains a meaningful comment about a planned showing."""
    plan_markers = ("показ", "договарива", "назнач", "встреч")
    for item in timeline:
        if allowed_author_ids is not None:
            if _coerce_int(item.get("author_id")) not in allowed_author_ids:
                continue
        text = str(item.get("comment") or "").strip().lower()
        if len(text) < 12:
            continue
        if "показ" in text and any(marker in text for marker in plan_markers):
            return True
    return False
```

### 3d. НЕ удалять (используются rule_2):
- `_open_activity_due_datetime` — используется rule_2 (строка 728)
- `_is_contact_plan_activity` — используется rule_2 (строка 722)
- `_activity_text` — используется внутри `_is_contact_plan_activity`

---

## Шаг 4 — Обновить комментарий в stage mapping

**Файл:** `src/tools.py`, строка 33

**Было:**
```python
    "C18:UC_UFPFKK": 3,     # Показ — >3 дней без комментария
```

**Стало:**
```python
    "C18:UC_UFPFKK": 3,     # Показ — >7 дней без комментария от брокера/РОПа
```

---

## Шаг 5 — Обновить RULE_ADVICE в weekly_report.py

**Файл:** `src/weekly_report.py`, строка 77

**Было:**
```python
            "buyer_stage_3": "на этапе «Показ» обязательно держать актуальное запланированное дело и будущую дату показа, иначе переносить сделку дальше",
```

**Стало:**
```python
            "buyer_stage_3": "оставлять комментарий в таймлайне сделки каждые 7 дней на этапе «Показ»",
```

---

## Шаг 6 — Обновить RULES_ADVICE_JSON в .env.example

**Файл:** `.env.example`

В строке `RULES_ADVICE_JSON` найти `buyer_stage_3` и заменить значение на:
```
"buyer_stage_3":"оставлять комментарий в таймлайне сделки каждые 7 дней на этапе «Показ»"
```

---

## Проверка

1. `flake8 src/ --max-line-length=120`
2. `.venv/bin/python -m pytest tests/ -v`
3. Убедиться, что удалённые функции (`_show_date_from_uf`, `_show_date_from_timeline_comments`, `_has_show_plan_comment`) нигде больше не импортируются:
   ```bash
   grep -r "_show_date_from_uf\|_show_date_from_timeline_comments\|_has_show_plan_comment" src/
   ```
