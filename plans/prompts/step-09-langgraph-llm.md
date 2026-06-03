# Промпт для Cursor — `prompts.py` + `graph.py` с LLM (Этап 3)

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).  
> **Внимание:** Этот промпт перепишет `src/prompts.py` и `src/graph.py`.

---

Полностью перепиши `src/prompts.py` и `src/graph.py` — замени заглушки реальной интеграцией с LLM через RouterAI и LangGraph tool-calling.

## Контекст

- Проект: `b24-ai-auditor`
- LLM: `ChatOpenAI` с `base_url="https://routerai.ru/api/v1"` и `api_key` из `settings.routerai_api_key`
- Инструменты уже реализованы в `src/tools.py` (5 штук, импорт: `from tools import get_deal_context, check_calls, check_lead_qualification, create_violation_task, send_management_report`)
- Конфигурация: `from config import Settings, get_settings`
- Модели: `deepseek/deepseek-chat` (V3) для Auditor/Dispatcher, `deepseek/deepseek-reasoner` (R1) для Analyst

---

## Файл 1: `src/prompts.py`

Замени плейсхолдеры реальными системными промптами на русском языке:

```python
"""System prompts for LangGraph agent nodes (Russian)."""

AUDITOR_SYSTEM_PROMPT = """\
Ты — аудитор CRM Битрикс24. Твоя задача — собрать полный контекст по каждой сделке из списка.

У тебя есть инструменты:
- get_deal_context(deal_id) — получить поля сделки и комментарии из таймлайна
- check_calls(user_id, hours_ago) — проверить исходящие звонки менеджера
- check_lead_qualification(max_hours) — проверить необработанные лиды

ПРАВИЛА СБОРА ДАННЫХ:
1. Для каждого deal_id из списка вызови get_deal_context(deal_id).
2. Для каждого ответственного менеджера (assigned_by_id) из полученных сделок вызови check_calls(user_id, hours_ago=24).
3. Вызови check_lead_qualification(max_hours=1) для проверки лидов.

Собери ВСЕ данные в одном месте. Не анализируй — только собирай. Верни результат в формате JSON.
"""

ANALYST_SYSTEM_PROMPT = """\
Ты — строгий контролёр регламента CRM Битрикс24. На вход подаётся JSON с собранными данными по сделкам, звонкам и лидам.

Твоя задача — найти ВСЕ нарушения регламента. Ниже жёсткие правила:

ПРАВИЛО 1 — Квалификация лидов:
- Если лид имеет статус "NEW" и с момента создания прошло более 1 часа — это НАРУШЕНИЕ.
- Фиксируй: lead_id, title, hours_since_creation.

ПРАВИЛО 2 — Комментарии в сделках:
- Если стадия сделки "Переговоры" (stage_id содержит "NEGOTIATION" или "C2:PREPARATION" или подобное) И в таймлайне нет комментариев за последние 3 дня — это НАРУШЕНИЕ.
- Фиксируй: deal_id, title, stage_id, последняя дата комментария.

ПРАВИЛО 3 — Активность по сделке:
- Если стадия сделки не "Успешно реализована" (не "WON") и в таймлайне нет комментариев за последние 7 дней — это НАРУШЕНИЕ.
- Фиксируй: deal_id, title, stage_id, дней с последнего комментария.

ПРАВИЛО 4 — Звонки менеджера:
- Если у менеджера за последние 24 часа нет ни одного успешного исходящего звонка (successful_calls == 0) — это НАРУШЕНИЕ.
- Фиксируй: user_id, total_calls, successful_calls.

Верни СТРОГО структурированный JSON:
{
    "violations": [
        {
            "rule": "rule_1 | rule_2 | rule_3 | rule_4",
            "severity": "high | medium | low",
            "description": "краткое описание нарушения на русском",
            "details": {...}
        }
    ],
    "summary": "краткая сводка на русском"
}

Не придумывай нарушения, которых нет в данных. Будь точен.
"""

DISPATCHER_SYSTEM_PROMPT = """\
Ты — диспетчер мер реагирования на нарушения регламента CRM.

У тебя есть инструменты:
- create_violation_task(user_id, deal_id, description) — поставить задачу брокеру-нарушителю
- send_management_report(chat_id, report_text) — отправить сводку в чат руководителей

На вход подаётся JSON со списком нарушений от Аналитика.

ПРАВИЛА ДИСПЕТЧЕРИЗАЦИИ:
1. Для каждого нарушения severity="high" — создай задачу на ответственного через create_violation_task.
2. Собери все нарушения в текстовую сводку и отправь через send_management_report в чат MANAGEMENT_CHAT_ID (если он задан в настройках).
3. Если violations пуст — отправь сообщение "Нарушений не найдено" в чат руководителей (только если MANAGEMENT_CHAT_ID задан).
4. Учитывай DRY_RUN: если он true, инструменты сами пропустят мутации — это нормально.

Действуй строго по инструкции. Не добавляй лишних действий.
"""
```

---

## Файл 2: `src/graph.py`

Полностью перепиши с реальной LLM-интеграцией:

```python
"""LangGraph workflow: Auditor -> Analyst -> Dispatcher with LLM."""

import asyncio
import json
import logging
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent

from config import Settings
from prompts import ANALYST_SYSTEM_PROMPT, AUDITOR_SYSTEM_PROMPT, DISPATCHER_SYSTEM_PROMPT
from tools import (
    check_calls,
    check_lead_qualification,
    create_violation_task,
    get_deal_context,
    send_management_report,
)

logger = logging.getLogger(__name__)

# Инструменты сбора данных (только чтение)
AUDITOR_TOOLS = [get_deal_context, check_calls, check_lead_qualification]

# Инструменты действий (мутации, учитывают DRY_RUN)
DISPATCHER_TOOLS = [create_violation_task, send_management_report]


class CRMState(TypedDict, total=False):
    """LangGraph state for the CRM audit pipeline."""

    deal_ids: list[int]
    dry_run: bool
    status: str
    messages: list[str]
    collected_data: list[dict[str, Any]]
    violations: list[dict[str, Any]]
    actions_taken: list[dict[str, Any]]


def _make_v3_llm(settings: Settings) -> ChatOpenAI:
    """Create DeepSeek-V3 LLM via RouterAI for tool-calling nodes."""
    return ChatOpenAI(
        api_key=settings.routerai_api_key,
        base_url=settings.routerai_base_url,
        model=settings.routerai_v3_model,
        temperature=0.1,
    )


def _make_r1_llm(settings: Settings) -> ChatOpenAI:
    """Create DeepSeek-R1 LLM via RouterAI for reasoning (Analyst)."""
    return ChatOpenAI(
        api_key=settings.routerai_api_key,
        base_url=settings.routerai_base_url,
        model=settings.routerai_r1_model,
        temperature=0.1,
    )


async def auditor_node(state: CRMState, settings: Settings) -> CRMState:
    """Auditor: collect CRM data using DeepSeek-V3 + tools."""
    logger.info("Auditor: collecting data for %d deals", len(state.get("deal_ids", [])))
    
    llm = _make_v3_llm(settings)
    agent = create_react_agent(llm, AUDITOR_TOOLS)
    
    deal_list = "\n".join(f"- deal_id={did}" for did in state.get("deal_ids", []))
    user_message = f"Собери контекст для следующих сделок:\n{deal_list}\nТакже проверь звонки ответственных менеджеров и квалификацию лидов."
    
    result = await agent.ainvoke(
        {"messages": [SystemMessage(content=AUDITOR_SYSTEM_PROMPT), HumanMessage(content=user_message)]}
    )
    
    # Extract collected data from agent messages
    messages = list(state.get("messages", []))
    messages.append(f"auditor: collected context for {len(state.get('deal_ids', []))} deals")
    
    return {
        **state,
        "messages": messages,
        "collected_data": result.get("messages", []),
    }


async def analyst_node(state: CRMState, settings: Settings) -> CRMState:
    """Analyst: detect violations using DeepSeek-R1 (reasoning)."""
    logger.info("Analyst: analyzing collected data for violations")
    
    llm = _make_r1_llm(settings)
    
    # Format collected data as JSON string for the analyst
    collected_str = json.dumps(state.get("collected_data", []), ensure_ascii=False, default=str)
    
    response = await llm.ainvoke([
        SystemMessage(content=ANALYST_SYSTEM_PROMPT),
        HumanMessage(content=f"Проанализируй следующие данные CRM и найди нарушения регламента:\n\n{collected_str}"),
    ])
    
    # Parse violations from response
    violations: list[dict[str, Any]] = []
    try:
        content = response.content if hasattr(response, "content") else str(response)
        # Try to extract JSON from the response
        if "{" in content:
            json_start = content.index("{")
            json_str = content[json_start:]
            parsed = json.loads(json_str)
            violations = parsed.get("violations", [])
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse Analyst response as JSON: %s", exc)
        violations = [{"rule": "parse_error", "description": "Не удалось разобрать ответ аналитика"}]
    
    logger.info("Analyst: found %d violations", len(violations))
    
    messages = list(state.get("messages", []))
    messages.append(f"analyst: found {len(violations)} violations")
    
    return {**state, "messages": messages, "violations": violations}


async def dispatcher_node(state: CRMState, settings: Settings) -> CRMState:
    """Dispatcher: apply corrective actions using DeepSeek-V3 + tools."""
    violations = state.get("violations", [])
    logger.info("Dispatcher: processing %d violations (dry_run=%s)", len(violations), settings.dry_run)
    
    if not violations:
        messages = list(state.get("messages", []))
        messages.append("dispatcher: no violations to process")
        return {**state, "messages": messages, "actions_taken": [], "status": "completed"}
    
    llm = _make_v3_llm(settings)
    agent = create_react_agent(llm, DISPATCHER_TOOLS)
    
    violations_json = json.dumps(violations, ensure_ascii=False)
    chat_id_info = f"MANAGEMENT_CHAT_ID={settings.management_chat_id}" if settings.management_chat_id else "MANAGEMENT_CHAT_ID не задан"
    
    user_message = (
        f"Обработай следующие нарушения регламента:\n{violations_json}\n\n"
        f"Настройки: DRY_RUN={settings.dry_run}, ADMIN_USER_ID={settings.admin_user_id}, {chat_id_info}"
    )
    
    result = await agent.ainvoke(
        {"messages": [SystemMessage(content=DISPATCHER_SYSTEM_PROMPT), HumanMessage(content=user_message)]}
    )
    
    messages = list(state.get("messages", []))
    messages.append(f"dispatcher: processed {len(violations)} violations")
    
    return {
        **state,
        "messages": messages,
        "actions_taken": result.get("messages", []),
        "status": "completed",
    }


def build_graph(settings: Settings):
    """Build and compile the audit LangGraph."""
    graph = StateGraph(CRMState)

    async def auditor(state: CRMState) -> CRMState:
        return await auditor_node(state, settings)

    async def analyst(state: CRMState) -> CRMState:
        return await analyst_node(state, settings)

    async def dispatcher(state: CRMState) -> CRMState:
        return await dispatcher_node(state, settings)

    graph.add_node("auditor", auditor)
    graph.add_node("analyst", analyst)
    graph.add_node("dispatcher", dispatcher)
    graph.add_edge(START, "auditor")
    graph.add_edge("auditor", "analyst")
    graph.add_edge("analyst", "dispatcher")
    graph.add_edge("dispatcher", END)

    return graph.compile()


async def run_audit(settings: Settings, deal_ids: list[int] | None = None) -> CRMState:
    """Run the full audit graph once.

    Args:
        settings: Application settings.
        deal_ids: Optional list of deal IDs to audit. If None, uses [1, 2, 3] as default test set.

    Returns:
        Final graph state after all nodes complete.
    """
    if deal_ids is None:
        deal_ids = [1, 2, 3]  # Default test set
    
    app = build_graph(settings)
    initial: CRMState = {
        "deal_ids": deal_ids,
        "dry_run": settings.dry_run,
        "messages": [],
        "collected_data": [],
        "violations": [],
        "actions_taken": [],
    }
    return await app.ainvoke(initial)
```

**Также обнови `src/main.py`** чтобы он передавал `deal_ids` в `run_audit`:

В функции `main()`:
```python
# Test deal IDs — замени на реальные или загрузку из CRM
test_deal_ids = [1, 2, 3]
result = await run_audit(settings, deal_ids=test_deal_ids)
```

---

## Проверка

После создания файлов:
1. `.\make.cmd lint` — должен пройти без ошибок
2. `.\make.cmd run` — должен запустить граф с реальными LLM-вызовами (если RouterAI ключ задан)
3. При отсутствии ключа — ожидаема ошибка аутентификации, это нормально
