# step-57-fix-no-comment-reason.md

## Проблема

В отчёте сделка без комментариев показывает:
```
На этапе «Показ» последний комментарий более 3 дней назад. (999 дн.)
```

А должно быть:
```
На этапе «Показ» нет комментариев.
```

## Задача

В [`check_buyer_deal_violations()`](src/tools.py:436-470) для правил 2, 3, 4 различать два случая:
- `days_since_comment >= 999` → «нет комментариев»
- `days_since_comment > threshold` → «последний комментарий более X дней назад»

---

## Правило 2 (Подбор) — строка 436-446

**Было:**
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

**Стало:**
```python
        elif rule_num == 2:
            days_since_comment = _days_since_last_any_comment(timeline, now)
            if days_since_comment > 5:
                if days_since_comment >= 999:
                    reason = f"На этапе «{stage_name}» нет комментариев."
                else:
                    reason = (
                        f"На этапе «{stage_name}» последний комментарий "
                        f"более {days_since_comment} дней назад."
                    )
                violations.append(_violation(
                    deal,
                    "buyer_stage_2",
                    reason,
                    {**base_details,
                     "days_since_last_comment": days_since_comment},
                ))
```

---

## Правило 3 (Показ) — строка 448-458

**Было:**
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

**Стало:**
```python
        elif rule_num == 3:
            days_since_comment = _days_since_last_any_comment(timeline, now)
            if days_since_comment > 3:
                if days_since_comment >= 999:
                    reason = f"На этапе «{stage_name}» нет комментариев."
                else:
                    reason = (
                        f"На этапе «{stage_name}» последний комментарий "
                        f"более {days_since_comment} дней назад."
                    )
                violations.append(_violation(
                    deal,
                    "buyer_stage_3",
                    reason,
                    {**base_details,
                     "days_since_last_comment": days_since_comment},
                ))
```

---

## Правило 4 (Показ проведен) — строка 460-470

**Было:**
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

**Стало:**
```python
        elif rule_num == 4:
            days_since_comment = _days_since_last_any_comment(timeline, now)
            if days_since_comment > 5:
                if days_since_comment >= 999:
                    reason = f"На этапе «{stage_name}» нет комментариев."
                else:
                    reason = (
                        f"На этапе «{stage_name}» последний комментарий "
                        f"более {days_since_comment} дней назад."
                    )
                violations.append(_violation(
                    deal,
                    "buyer_stage_4",
                    reason,
                    {**base_details,
                     "days_since_last_comment": days_since_comment},
                ))
```

---

## Проверка

```bash
python -m pytest tests/test_buyer_audit.py -v
python -m flake8 src/tools.py
```

Убедиться, что в тестах `test_rule2_violation_no_comments`, `test_rule3_violation_no_comments` проверяется текст «нет комментариев», а не «более 999 дней назад».
