# Fix Deal Audit: ROP Comments + 999 Display

## Task

Fix two issues in the buyer deal audit:

1. **Comments should only count from broker OR their ROP** (not any user)
2. **Don't show `(999 дн.)` when there are no comments**

---

## Issue 1: ROP Comment Authorization

### Current Problem
- Rules 2,3,4 use `_days_since_last_any_comment` — ANY user's comment resets the violation. Wrong.
- Rule 5 uses `_days_since_last_comment(timeline, assigned_by_id)` — only broker's comment. Wrong.

### Required Behavior
A comment should only be counted if from:
- The responsible broker (`assigned_by_id`)
- OR the broker's ROP (user with `WORK_POSITION = "Руководитель отдела продаж (РОП)"` in the same department)

### Implementation

**1. Add new function in `src/tools.py` after `_days_since_last_any_comment` (~line 255):**

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

**2. Add `_build_rop_map()` function in `src/tools.py` before `check_buyer_deal_violations`:**

```python
def _build_rop_map() -> dict[int, int]:
    """Build department_id → ROP user_id mapping."""
    settings = get_settings()
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
        logger.warning("Failed to build ROP map")
        return {}
```

**3. Change `check_buyer_deal_violations` signature to accept `rop_map`:**

```python
def check_buyer_deal_violations(
    deals: list[dict[str, Any]],
    current_time: str,
    rop_map: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
```

At the start of the function, add:
```python
    if rop_map is None:
        rop_map = _build_rop_map()
```

**4. Build `broker_dept_map` before the deal loop. Add after `now` and before `for deal in deals:`:**

```python
    # Build broker→department map for ROP lookup
    broker_ids_set = set()
    for deal in deals:
        bid = _coerce_int(deal.get("assigned_by_id"))
        if bid:
            broker_ids_set.add(bid)
    
    broker_dept_map: dict[int, int] = {}
    if broker_ids_set and rop_map:
        try:
            bx = _get_bitrix()
            for bid in broker_ids_set:
                raw = bx.call("user.get", {"ID": bid})
                u = raw[0] if isinstance(raw, list) and raw else raw
                if isinstance(u, dict):
                    d = u.get("UF_DEPARTMENT", [])
                    if isinstance(d, list) and d:
                        broker_dept_map[bid] = _coerce_int(d[0])
        except Exception:
            logger.warning("Failed to load broker departments")
```

**5. After `assigned_by_id` in the deal loop, add allowed authors:**

```python
        assigned_by_id = _coerce_int(deal.get("assigned_by_id"))
        # Build allowed comment authors: broker + their ROP
        allowed_authors = {assigned_by_id}
        broker_dept = broker_dept_map.get(assigned_by_id)
        if broker_dept and rop_map:
            rop_id = rop_map.get(broker_dept)
            if rop_id:
                allowed_authors.add(rop_id)
```

**6. Replace all comment checks in rules 2,3,4,5:**

- Rules 2,3,4: Replace `_days_since_last_any_comment(timeline, now)` → `_days_since_last_comment_by_authors(timeline, now, allowed_authors)`
- Rule 5: Replace `_days_since_last_comment(timeline, assigned_by_id, now)` → `_days_since_last_comment_by_authors(timeline, now, allowed_authors)`

---

## Issue 2: Hide `(999 дн.)` Display

### Problem
When a deal has no comments, the report shows: `нет комментариев. (999 дн.)` — the 999 is an internal marker.

### Fix

In `src/graph.py`, function `report_dispatcher`, line ~694, change:

```python
if days is not None and days != "":
```
To:
```python
if days is not None and days != "" and days < 999:
```

---

## Issue 3: Update buyer_deal_analyst in graph.py

In `src/graph.py`, function `buyer_deal_analyst`, change:

```python
all_violations = check_buyer_deal_violations(deals, current_time)
```
To:
```python
from tools import _build_rop_map
rop_map = _build_rop_map()
all_violations = check_buyer_deal_violations(deals, current_time, rop_map)
```

---

## Issue 4: Update Tests

In `tests/test_buyer_audit.py`, add this test:

```python
def test_rule2_rop_comment_counts():
    """Комментарий от РОПа засчитывается — нарушения нет."""
    deal = _deal(
        audit_rule=2,
        assigned_by_id=100,
        timeline=[
            {"author_id": 200, "comment": "OK", "created": "2026-06-07T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations(
        [deal], CURRENT, rop_map={0: 200},
    )
    assert violations == []


def test_rule2_stranger_comment_does_not_count():
    """Комментарий от чужого НЕ засчитывается — нарушение есть."""
    deal = _deal(
        audit_rule=2,
        assigned_by_id=100,
        timeline=[
            {"author_id": 999, "comment": "Привет", "created": "2026-06-07T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations(
        [deal], CURRENT, rop_map={0: 200},
    )
    assert len(violations) == 1
    assert "нет комментариев" in violations[0]["reason"]
```

Update all existing tests that call `check_buyer_deal_violations` — they should still work since `rop_map` is optional.

---

## Verification

```bash
python -m pytest tests/test_buyer_audit.py -v
python -m flake8 src/tools.py src/graph.py
python src/main.py  # DRY_RUN — check reports
```
