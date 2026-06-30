# План: Исправление недочётов + Оптимизация затрат на DeepSeek API

## Текущая картина расходов на DeepSeek API

Сейчас LLM вызывается в **3 точках** пайплайна. Каждый вызов разбивается на чанки по 100 сущностей:

```
┌─────────────────────────────────────────────────────────────────┐
│ lead_analyst         → N_лидов / 100 вызовов LLM               │
│ buyer_calls_controller → N_сделок_покупателей / 100 вызовов LLM│
│ missed_calls_controller → N_лидов / 100 вызовов LLM            │
└─────────────────────────────────────────────────────────────────┘
```

**Пример:** 500 лидов + 300 сделок = 5 + 3 + 5 = **13 вызовов LLM за один аудит**.
При двух запусках в день (10:00 и 17:00 МСК) = **26 вызовов/день**, ~**520 вызовов/месяц**.

---

## 🎯 Главная идея экономии: детерминизировать всё, что можно

Анализ показал, что **2 из 3 вызовов LLM можно полностью заменить детерминированным Python-кодом**, а третий — существенно сократить.

### Сравнение: сейчас → после оптимизации

| Компонент | Сейчас | После | Экономия |
|-----------|--------|-------|----------|
| `lead_analyst` | 100% лидов через LLM | ~30-40% лидов (только rule_2, rule_3); rule_1 — детерминированно | **60-70%** |
| `buyer_calls_controller` | 100% сделок через LLM | 0% (полностью детерминированно) | **100%** |
| `missed_calls_controller` | 100% лидов через LLM | 0% (полностью детерминированно) | **100%** |

**Итоговая экономия: ~70-80% вызовов LLM**, или примерно с 520 до 100-150 вызовов/месяц.

---

## 📋 Фаза 1: Критические исправления

### 1.1 Вынос конфигурации из кода в `.env`

**Файлы:** `src/graph.py`, `src/config.py`, `.env.example`

Что меняется:
- `REPORT_CHAT_ID = 22358` → `settings.report_chat_id`
- `DEPT_CHAT_MAP: dict[int, int]` → `settings.dept_chat_map` (JSON-строка в `.env`)
- `ANALYST_CHUNK_SIZE = 100` → `settings.analyst_chunk_size`

```python
# config.py — добавить поля:
report_chat_id: int = Field(default=22358, validation_alias="REPORT_CHAT_ID")
dept_chat_map_json: str = Field(default="{}", validation_alias="DEPT_CHAT_MAP_JSON")
analyst_chunk_size: int = Field(default=100, validation_alias="ANALYST_CHUNK_SIZE")

@property
def dept_chat_map(self) -> dict[int, int]:
    return json.loads(self.dept_chat_map_json)
```

```bash
# .env.example — добавить:
REPORT_CHAT_ID=22358
DEPT_CHAT_MAP_JSON={"60":22358,"46":22358,"42":22358,"44":22358,"50":22358,"66":22358}
ANALYST_CHUNK_SIZE=100
```

### 1.2 Кеширование звонков по пользователям (устранение N+1 API-вызовов)

**Файл:** `src/tools.py` — функции `get_all_leads_with_timeline()` и `get_deals_by_funnel_with_timeline()`

Сейчас для каждой сущности вызывается `_fetch_user_calls_for_audit(assigned_id)`. Если 100 лидов у 5 менеджеров — 100 вызовов вместо 5.

**Исправление:** собрать уникальные `user_id` → запросить звонки один раз → прикрепить к сущностям.

```python
# Псевдокод:
unique_user_ids = set(record["assigned_by_id"] for record in records.values())
calls_cache = {
    uid: _fetch_user_calls_for_audit(uid, hours_ago=720)
    for uid in unique_user_ids
}
for record in records.values():
    record["calls"] = calls_cache.get(record["assigned_by_id"], [])
```

### 1.3 Удаление отладочных скриптов из корня

Удалить файлы:
- `tmp_check_deal.py`
- `tmp_check_deal_12670.py`
- `tmp_check_deals.py`
- `tmp_check_lead.py`

Добавить `tmp_*.py` в `.gitignore`.

---

## 📋 Фаза 2: Оптимизация затрат на DeepSeek API

### 2.1 Детерминизация `buyer_calls_controller` и `missed_calls_controller`

**Экономия: 100% вызовов LLM для этих двух агентов.**

Оба агента проверяют одно и то же правило:
> «Найти САМЫЙ ПОСЛЕДНИЙ пропущенный звонок. Если после него нет исходящего — нарушение.»

Это **чисто алгоритмическая задача**, не требующая LLM:

```python
def check_missed_callback_violations(
    entities: list[dict[str, Any]],
    entity_type: str,  # "lead" или "deal"
) -> list[dict[str, Any]]:
    """Детерминированная проверка: последний пропущенный без обратного."""
    violations = []
    id_field = f"{entity_type}_id"
    
    for entity in entities:
        calls = entity.get("calls", [])
        if not calls:
            continue
        
        # 1. Сортируем звонки по start_date
        sorted_calls = sorted(calls, key=lambda c: c.get("start_date", ""))
        
        # 2. Находим последний missed
        last_missed_idx = -1
        for i in range(len(sorted_calls) - 1, -1, -1):
            if sorted_calls[i].get("status") == "missed":
                last_missed_idx = i
                break
        
        if last_missed_idx == -1:
            continue  # Нет пропущенных
        
        # 3. Проверяем, есть ли исходящий после последнего пропущенного
        has_callback = any(
            c.get("call_type") == "outgoing"
            for c in sorted_calls[last_missed_idx + 1:]
        )
        
        if not has_callback:
            violations.append({
                "entity_type": entity_type,
                "entity_id": entity.get(id_field),
                "responsible_id": entity.get("assigned_by_id"),
                "severity": "very high",
                "rule": f"{entity_type}_missed_callback",
                "reason": "Пропущенный звонок без обратного",
                "details": {},
            })
    
    return violations
```

**Важно:** перед удалением LLM-версии стоит сравнить результаты на реальных данных (запустить обе версии параллельно в течение 2-3 дней), чтобы убедиться в идентичности.

### 2.2 Частичная детерминизация `lead_analyst` + фильтрация

**Экономия: 60-70% вызовов LLM для этого агента.**

**Шаг 1 — Предварительная фильтрация лидов:** не отправлять в LLM лиды, которые заведомо не подпадают под правила:

```python
def _lead_needs_llm_check(lead: dict[str, Any]) -> bool:
    """Возвращает True, если лид требует LLM-анализа (rule_2 или rule_3)."""
    status_id = str(lead.get("status_id", "")).upper()
    
    # rule_1 (NEW > 2 часов) — будет проверено детерминированно
    # rule_2 (SPAM) — нужен LLM для проверки обоснования
    if "SPAM" in status_id:
        return True
    
    # rule_3 (Нецелевой) — нужен LLM для проверки обоснования
    # Не NEW, не SPAM, не WON, не LOSE, не QUALIFIED, не AGENT
    skip_statuses = {"NEW", "SPAM", "WON", "LOSE", "QUALIFIED", "AGENT", "UC_52VG81"}
    if not any(s in status_id for s in skip_statuses):
        return True
    
    return False
```

**Шаг 2 — Детерминированная проверка rule_1 (NEW > 2 часов):**

```python
def check_lead_rule1_violations(
    leads: list[dict[str, Any]],
    current_time: datetime,
) -> list[dict[str, Any]]:
    """Детерминированная проверка: лид NEW > 2 часов без комментария."""
    violations = []
    for lead in leads:
        status_id = str(lead.get("status_id", "")).upper()
        if "NEW" not in status_id:
            continue
        
        date_create = _parse_datetime(lead.get("date_create"))
        if date_create is None:
            continue
        
        hours = (current_time - date_create).total_seconds() / 3600
        if hours <= 2:
            continue
        
        # Проверяем, есть ли комментарий от ответственного в timeline
        assigned_id = lead.get("assigned_by_id")
        timeline = lead.get("timeline", [])
        has_broker_comment = any(
            item.get("author_id") == assigned_id and str(item.get("comment", "")).strip()
            for item in timeline
        )
        
        if not has_broker_comment:
            violations.append({
                "entity_type": "lead",
                "entity_id": lead.get("lead_id"),
                "responsible_id": assigned_id,
                "severity": "high",
                "rule": "lead_rule_1",
                "reason": f"Лид находится в статусе «Новый» более 2 часов, необходимо квалифицировать лида. (прошло {hours:.0f} часов)",
                "details": {
                    "lead_id": lead.get("lead_id"),
                    "title": lead.get("title"),
                    "status_name": lead.get("status_name"),
                    "hours_since_creation": round(hours, 2),
                    "has_broker_comment": False,
                },
            })
    return violations
```

**Шаг 3 — LLM только для rule_2 и rule_3 на предварительно отфильтрованных лидах:**

```python
async def lead_analyst(state: AuditState, settings: Settings) -> AuditState:
    leads = state.get("raw_leads", [])
    if not leads:
        return {"violations": []}
    
    current_time = _parse_datetime(state.get("current_time", "")) or datetime.now(timezone.utc)
    
    # Детерминированная проверка rule_1
    rule1_violations = check_lead_rule1_violations(leads, current_time)
    
    # Фильтруем лиды, требующие LLM (rule_2: SPAM, rule_3: Нецелевой)
    llm_leads = [lead for lead in leads if _lead_needs_llm_check(lead)]
    
    if not llm_leads:
        return {"violations": rule1_violations}
    
    # LLM только для rule_2/rule_3
    llm = _make_llm(settings)
    all_violations = list(rule1_violations)
    
    for i in range(0, len(llm_leads), ANALYST_CHUNK_SIZE):
        chunk = llm_leads[i:i + ANALYST_CHUNK_SIZE]
        # ... LLM invoke как сейчас, но с облегчённым промптом (только rule_2, rule_3)
    
    return {"violations": all_violations}
```

### 2.3 Облегчение промпта для `lead_analyst`

После детерминизации rule_1 промпт можно сократить **на ~40%**, убрав описание rule_1. Меньше входных токенов = дешевле каждый вызов.

### 2.4 Отправка только нужных полей в LLM

Сейчас в LLM отправляются полные объекты лидов. Можно отправлять только поля, нужные для rule_2/rule_3:

```python
llm_fields = ("lead_id", "title", "status_id", "status_name", 
              "assigned_by_id", "comments_field", "timeline")
```

---

## 📋 Фаза 3: Средние исправления

### 3.1 Дедупликация кода аналитиков

После детерминизации `buyer_calls_controller` и `missed_calls_controller` их код станет одинаковым (вызов `check_missed_callback_violations` с разными параметрами). Можно объединить:

```python
async def _generic_calls_controller(
    entities: list[dict[str, Any]],
    entity_type: str,
    logger_name: str,
) -> list[dict[str, Any]]:
    return check_missed_callback_violations(entities, entity_type)
```

### 3.2 Кроссплатформенный Makefile

```makefile
# Определяем ОС и пути
PYTHON := python
PIP := pip
ACTIVATE :=

ifeq ($(OS),Windows_NT)
    PYTHON := venv\Scripts\python
    PIP := venv\Scripts\pip
    ACTIVATE := venv\Scripts\activate
else
    PYTHON := venv/bin/python
    PIP := venv/bin/pip
    ACTIVATE := . venv/bin/activate
endif
```

Или, что проще — добавить `make.cmd` для Windows, а `Makefile` переписать под Linux.

### 3.3 Удалить неиспользуемый `BUYER_DEAL_ANALYST_PROMPT`

Удалить из `prompts.py`, т.к. `buyer_deal_analyst` работает детерминированно.

### 3.4 Удалить дублирующийся `_parse_b24_datetime`

Оставить только `_parse_datetime()` (более полная версия) и заменить вызов `_parse_b24_datetime` в `check_lead_qualification`.

---

## 📋 Фаза 4: Nice-to-have улучшения

### 4.1 Ротация логов

```python
# config.py — setup_logging()
from logging.handlers import RotatingFileHandler

file_handler = RotatingFileHandler(
    log_dir / "audit.log",
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
    encoding="utf-8",
)
```

### 4.2 Добавить `pyproject.toml`

Заменить `setup.py` + `requirements.txt` на современный `pyproject.toml` (PEP 517/518).

### 4.3 Добавить `mypy` в линтер

```bash
# requirements-dev.txt — добавить:
mypy>=1.0.0
```

### 4.4 Упростить `build_graph_v2`

```python
from functools import partial

def build_graph_v2(settings: Settings):
    graph = StateGraph(AuditState)
    
    graph.add_node("lead_collector", partial(lead_collector, settings=settings))
    graph.add_node("buyer_collector", partial(buyer_collector, settings=settings))
    # ... вместо семи инлайн-обёрток
```

Для этого нужно изменить сигнатуры функций, чтобы `settings` был keyword-аргументом.

### 4.5 Единый `ThreadPoolExecutor`

Вынести `_BX_EXECUTOR` из `tools.py` и `notify.py` в общий модуль (например, в `config.py`).

### 4.6 Добавить pre-commit хуки

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/psf/black
    rev: 24.1.0
    hooks:
      - id: black
  - repo: https://github.com/pycqa/flake8
    rev: 7.0.0
    hooks:
      - id: flake8
```

---

## 🔢 Оценка экономии на DeepSeek API

| Метрика | Сейчас | После | Экономия |
|---------|--------|-------|----------|
| `lead_analyst` вызовов LLM | N/100 | ~0.3N/100 | **~70%** |
| `buyer_calls_controller` вызовов LLM | M/100 | 0 | **100%** |
| `missed_calls_controller` вызовов LLM | N/100 | 0 | **100%** |
| **Всего вызовов/аудит** | (2N+M)/100 | 0.3N/100 | **~75%** |
| **Вызовов/месяц (пример: N=500, M=300)** | 520 | ~130 | **~75%** |
| **Токенов на вызов (промпт)** | ~3000 | ~1800 | **~40%** |

При стоимости DeepSeek `deepseek-v4-flash` ~$0.14/1M входных токенов, для 500 лидов и 300 сделок (2 аудита/день):

- **Сейчас:** ~520 вызовов × 3000 токенов × $0.14/1M ≈ **$0.22/месяц** — сам API недорогой
- Основная экономия не в деньгах, а во **времени выполнения** и **надёжности** (меньше точек отказа)

**Ключевой вывод:** DeepSeek API сам по себе дёшев. Главная ценность детерминизации — **скорость работы** (нет задержек на LLM), **предсказуемость** (нет галлюцинаций) и **надёжность** (меньше внешних зависимостей).

---

## 🗂️ Порядок выполнения

| Фаза | Шаги | Приоритет |
|------|------|-----------|
| **Фаза 1** | 1.1 Конфигурация в `.env` | 🔴 Критично |
| | 1.2 Кеширование звонков | 🔴 Критично |
| | 1.3 Удаление tmp-файлов | 🔴 Критично |
| **Фаза 2** | 2.1 Детерминизация calls controllers | 🟡 Важно (экономия) |
| | 2.2 Детерминизация lead rule_1 | 🟡 Важно (экономия) |
| | 2.3 Сокращение промпта | 🟡 Важно (экономия) |
| | 2.4 Отправка только нужных полей | 🟡 Важно (экономия) |
| **Фаза 3** | 3.1 Дедупликация кода | 🟢 Средне |
| | 3.2 Кроссплатформенный Makefile | 🟢 Средне |
| | 3.3-3.4 Удаление мёртвого кода | 🟢 Средне |
| **Фаза 4** | 4.1-4.6 Nice-to-have | ⚪ Низко |

---

## ⚠️ Предостережения при детерминизации

1. **Сравнить результаты до/после** — запустить обе версии параллельно в `DRY_RUN` режиме и сравнить выхлоп.
2. **Проверить краевые случаи** — даты без таймзоны, пустые звонки, звонки с неизвестным статусом.
3. **Не удалять старый код сразу** — оставить LLM-версию за feature-флагом на случай регресса.
