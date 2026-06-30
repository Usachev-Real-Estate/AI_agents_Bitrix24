# step-56-fix-deal-audit-rules.md

## Контекст

Меняются правила аудита сделок воронки «Покупатели». Вместо проверок UF-полей — теперь везде проверяется **давность последнего комментария в таймлайне** (от любого автора).

## Сводка изменений

| Правило | Этап | Было | Стало |
|---------|------|------|-------|
| 1 | Первый контакт | `days_on_stage > 1` | Без изменений |
| 2 | Подбор | `days_on_stage > 2 AND нет комментариев` | `дней с последнего комментария > 5` |
| 3 | Показ | Проверка UF-поля «Дата встречи» | `дней с последнего комментария > 3` |
| 4 | Показ проведен | `days_on_stage > 1 AND нет результата показа` | `дней с последнего комментария > 5` |
| 5 | Отложенный спрос | `дней с последнего комментария ответственного > 7` | Без изменений |

---

## Шаг 1 — Новая функция `_days_since_last_any_comment`

**Файл:** `src/tools.py`, после `_days_since_last_comment` (~строка 238).

```python
def _days_since_last_any_comment(
    timeline: list[dict[str, Any]],
    current: datetime,
) -> int:
    """Days since the latest timeline comment from ANY author, or 999 if none."""
    all_comments = [
        item for item in timeline
        if str(item.get("comment") or "").strip()
    ]
    if not all_comments:
        return 999
    latest = max(all_comments, key=lambda item: str(item.get("created") or ""))
    created = _parse_datetime(latest.get("created"))
    if created is None:
        return 999
    return int(_days_between(created, current))
```

---

## Шаг 2 — Правило 2 (Подбор): заменить проверку

**Файл:** `src/tools.py`, строки 419-426.

**Было:**
```python
        elif rule_num == 2:
            if days_on_stage > 2 and not _timeline_has_comment(timeline):
                violations.append(_violation(
                    deal,
                    "buyer_stage_2",
                    f"Сделка находится на этапе «{stage_name}» более 2 дней.",
                    {**base_details, "days_on_stage": days_on_stage},
                ))
```

**Стало:**
```python
        elif rule_num == 2:
            days_since_comment = _days_since_last_any_comment(timeline, now)
            if days_since_comment > 5:
                violations.append(_violation(
                    deal,
                    "buyer_stage_2",
                    f"На этапе «{stage_name}» последний комментарий более "
                    "5 дней назад.",
                    {**base_details,
                     "days_since_last_comment": days_since_comment},
                ))
```

---

## Шаг 3 — Правило 3 (Показ): полностью заменить

**Файл:** `src/tools.py`, строки 428-458.

**Было:** (весь блок `elif rule_num == 3:` с проверкой `show_date`)

**Стало:**
```python
        elif rule_num == 3:
            days_since_comment = _days_since_last_any_comment(timeline, now)
            if days_since_comment > 3:
                violations.append(_violation(
                    deal,
                    "buyer_stage_3",
                    f"На этапе «{stage_name}» последний комментарий более "
                    "3 дней назад.",
                    {**base_details,
                     "days_since_last_comment": days_since_comment},
                ))
```

---

## Шаг 4 — Правило 4 (Показ проведен): заменить проверку

**Файл:** `src/tools.py`, строки 460-479.

**Было:**
```python
        elif rule_num == 4:
            if days_on_stage <= 1:
                continue
            show_result = str(uf_fields.get("Результат показа") or "").strip()
            if len(show_result) > 10:
                continue
            latest = _latest_comment_from(timeline, assigned_by_id)
            if latest and len(str(latest.get("comment") or "")) > 10:
                continue
            violations.append(_violation(
                deal,
                "buyer_stage_4",
                f"Сделка на этапе «{stage_name}» более 1 дня без результата "
                "показа или комментария.",
                {
                    **base_details,
                    "days_on_stage": days_on_stage,
                    "has_detailed_comment": False,
                },
            ))
```

**Стало:**
```python
        elif rule_num == 4:
            days_since_comment = _days_since_last_any_comment(timeline, now)
            if days_since_comment > 5:
                violations.append(_violation(
                    deal,
                    "buyer_stage_4",
                    f"На этапе «{stage_name}» последний комментарий более "
                    "5 дней назад.",
                    {**base_details,
                     "days_since_last_comment": days_since_comment},
                ))
```

---

## Шаг 5 — Обновить тесты

**Файл:** `tests/test_buyer_audit.py`.

Удалить ВСЕ старые тесты и заменить на:

```python
"""Tests for deterministic buyer deal audit rules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools import check_buyer_deal_violations  # noqa: E402


def _deal(**overrides):
    base = {
        "deal_id": 12454,
        "title": "Test deal",
        "stage_id": "C18:UC_V0DMMX",
        "stage_name": "Подбор",
        "audit_rule": 2,
        "assigned_by_id": 100,
        "date_create": "2026-06-01T10:00:00+00:00",
        "timeline": [],
        "uf_fields": {},
    }
    base.update(overrides)
    return base


CURRENT = "2026-06-08T12:00:00+00:00"


# ── Правило 1 (без изменений: >1 дня на этапе) ──

def test_rule1_violation_over_1_day():
    deal = _deal(
        stage_id="C18:NEW", stage_name="Первый контакт", audit_rule=1,
        date_create="2026-06-01T10:00:00+00:00",
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_1"


def test_rule1_no_violation_under_1_day():
    deal = _deal(
        stage_id="C18:NEW", stage_name="Первый контакт", audit_rule=1,
        date_create="2026-06-08T00:00:00+00:00",
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


# ── Правило 2 (новое: >5 дней без комментария) ──

def test_rule2_violation_no_comments():
    deal = _deal(audit_rule=2, timeline=[])
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_2"


def test_rule2_violation_old_comment():
    deal = _deal(audit_rule=2, timeline=[
        {"author_id": 100, "comment": "x", "created": "2026-05-29T10:00:00+00:00"},
    ])
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1


def test_rule2_no_violation_recent_comment():
    deal = _deal(audit_rule=2, timeline=[
        {"author_id": 200, "comment": "x", "created": "2026-06-06T12:00:00+00:00"},
    ])
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


# ── Правило 3 (новое: >3 дней без комментария) ──

def test_rule3_violation_no_comments():
    deal = _deal(
        stage_id="C18:UC_UFPFKK", stage_name="Показ", audit_rule=3, timeline=[],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_3"


def test_rule3_no_violation_recent():
    deal = _deal(
        stage_id="C18:UC_UFPFKK", stage_name="Показ", audit_rule=3,
        timeline=[
            {"author_id": 100, "comment": "x", "created": "2026-06-07T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


# ── Правило 4 (новое: >5 дней без комментария) ──

def test_rule4_violation_old_comment():
    deal = _deal(
        stage_id="C18:UC_A15GLR", stage_name="Показ проведен", audit_rule=4,
        timeline=[
            {"author_id": 100, "comment": "x", "created": "2026-05-29T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_4"


def test_rule4_no_violation_recent():
    deal = _deal(
        stage_id="C18:UC_A15GLR", stage_name="Показ проведен", audit_rule=4,
        timeline=[
            {"author_id": 100, "comment": "x", "created": "2026-06-05T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


# ── Правило 5 (без изменений: >7 дней без комментария ответственного) ──

def test_rule5_violation():
    deal = _deal(
        stage_id="C18:LOSE", stage_name="Отложенный спрос", audit_rule=5,
        assigned_by_id=100,
        timeline=[
            {"author_id": 200, "comment": "x", "created": "2026-06-01T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_5"
```

---

## Проверка

```bash
python -m pytest tests/test_buyer_audit.py -v
python -m flake8 src/tools.py
```
