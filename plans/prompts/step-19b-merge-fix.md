# Промпт для Cursor — v2 Merge Fix: синхронизация веток перед Dispatcher

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`.

**Проблема:** `seller_collector` (1 узел) заканчивается быстрее чем цепочки `lead_collector → lead_analyst → missed_calls` и `buyer_collector → buyer_deal_analyst → buyer_calls`. Dispatcher срабатывает по первому пришедшему ребру и видит 0 violations.

**Решение:** добавить `merge` узел — все три ветки сходятся в него, и только потом dispatcher.

---

## Задача 1: Добавить merge-узел

Добавить ПЕРЕД `build_graph_v2`:

```python
async def merge_node(state: AuditState) -> AuditState:
    """No-op merge: waits for all branches before dispatcher."""
    return state
```

---

## Задача 2: Перестроить рёбра в `build_graph_v2()`

Заменить три прямых ребра → `report_dispatcher` на схему с `merge`:

**Было:**
```python
    graph.add_edge("missed_calls_controller", "report_dispatcher")
    ...
    graph.add_edge("buyer_calls_controller", "report_dispatcher")
    ...
    graph.add_edge("seller_collector", "report_dispatcher")

    graph.add_edge("report_dispatcher", END)
```

**Стало:**
```python
    # Merge node
    graph.add_node("merge", merge_node)

    # All branches → merge
    graph.add_edge("missed_calls_controller", "merge")
    graph.add_edge("buyer_calls_controller", "merge")
    graph.add_edge("seller_collector", "merge")

    # Merge → dispatcher → END
    graph.add_edge("merge", "report_dispatcher")
    graph.add_edge("report_dispatcher", END)
```

Итоговая функция `build_graph_v2`:

```python
def build_graph_v2(settings: Settings):
    """Build v2 audit graph: 3 collectors → 4 analysts → merge → dispatcher."""
    graph = StateGraph(AuditState)

    async def lead_col(state: AuditState) -> AuditState:
        return await lead_collector(state, settings)

    async def buyer_col(state: AuditState) -> AuditState:
        return await buyer_collector(state, settings)

    async def seller_col(state: AuditState) -> AuditState:
        return await seller_collector(state, settings)

    async def lead_an(state: AuditState) -> AuditState:
        return await lead_analyst(state, settings)

    async def buyer_deal_an(state: AuditState) -> AuditState:
        return await buyer_deal_analyst(state, settings)

    async def buyer_calls(state: AuditState) -> AuditState:
        return await buyer_calls_controller(state, settings)

    async def missed_calls(state: AuditState) -> AuditState:
        return await missed_calls_controller(state, settings)

    async def dispatcher(state: AuditState) -> AuditState:
        return await report_dispatcher(state, settings)

    # Nodes
    graph.add_node("lead_collector", lead_col)
    graph.add_node("buyer_collector", buyer_col)
    graph.add_node("seller_collector", seller_col)
    graph.add_node("lead_analyst", lead_an)
    graph.add_node("buyer_deal_analyst", buyer_deal_an)
    graph.add_node("buyer_calls_controller", buyer_calls)
    graph.add_node("missed_calls_controller", missed_calls)
    graph.add_node("report_dispatcher", dispatcher)
    graph.add_node("merge", merge_node)

    # Parallel collectors from START
    graph.add_edge(START, "lead_collector")
    graph.add_edge(START, "buyer_collector")
    graph.add_edge(START, "seller_collector")

    # Collector → Analyst chains
    graph.add_edge("lead_collector", "lead_analyst")
    graph.add_edge("lead_analyst", "missed_calls_controller")

    graph.add_edge("buyer_collector", "buyer_deal_analyst")
    graph.add_edge("buyer_deal_analyst", "buyer_calls_controller")

    # All branches → merge
    graph.add_edge("missed_calls_controller", "merge")
    graph.add_edge("buyer_calls_controller", "merge")
    graph.add_edge("seller_collector", "merge")

    # Merge → dispatcher → END
    graph.add_edge("merge", "report_dispatcher")
    graph.add_edge("report_dispatcher", END)

    return graph.compile()
```

---

## Проверка

1. `.\make.cmd lint` — должен пройти
2. `.\make.cmd dry-run` — dispatcher должен отработать ПОСЛЕ всех аналитиков
3. В логах: `Dispatcher: formatting report for N violations` где N > 0 (если аналитики нашли нарушения)
