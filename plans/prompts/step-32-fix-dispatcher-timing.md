# Промпт для Cursor — Dispatcher: слать всегда (ждать всех аналитиков)

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`.

**Проблема:** Dispatcher срабатывает до Agent 6/7 → звонки не попадают в отчёт.

**Решение:** убрать `report_sent` блокировку. Dispatcher шлёт отчёт каждый раз когда вызывается. Последний вызов будет после всех аналитиков = полный отчёт.

---

## Задача: Убрать `report_sent` из `report_dispatcher`

**Было:**
```python
    if state.get("report_sent"):
        logger.info("Dispatcher: report already sent, skipping")
        return state
```

**Стало:**
```python
    # Always send — LangGraph fan-in may call dispatcher multiple times.
    # The last call will have the complete violations list.
```

Также убрать `"report_sent": True` из return.

---

## Проверка

1. `.\make.cmd lint`
2. `.\make.cmd dry-run` — в чате 22358 последний отчёт будет с `📞 ЗВОНКИ`
