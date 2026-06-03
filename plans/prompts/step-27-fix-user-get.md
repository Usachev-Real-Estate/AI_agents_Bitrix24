# Промпт для Cursor — Исправить user.get (без FILTER)

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`.

**Проблема:** `_build_user_map` вызывает `user.get` с параметром `FILTER`, но `user.get` не поддерживает `FILTER`. Из-за этого `UF_DEPARTMENT` пустой → названия отделов не резолвятся → группировка по отделам не работает.

---

## Задача: Исправить вызов `user.get`

В функции `_build_user_map`, строка 391:

**Было:**
```python
raw = _bx_call_sync("user.get", {"FILTER": {"ID": uid}})
user: dict[str, Any] | None = None
if isinstance(raw, list) and raw:
    user = raw[0] if isinstance(raw[0], dict) else None
elif isinstance(raw, dict):
    user = raw
```

**Стало:**
```python
user: dict[str, Any] | None = None
raw = _bx_call_sync("user.get", {"ID": uid})
if isinstance(raw, dict) and raw:
    user = raw
else:
    # Fallback: try user.search with FILTER
    raw = _bx_call_sync("user.search", {"FILTER": {"ID": uid}})
    if isinstance(raw, list) and raw:
        user = raw[0] if isinstance(raw[0], dict) else None
```

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — в отчётах должны быть названия отделов: `Иванов Иван (Отдел продаж)`
