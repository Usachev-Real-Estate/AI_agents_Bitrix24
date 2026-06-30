# Промпт для Cursor — Доработка всех отчётов по результатам аудита

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/tools.py`, `src/graph.py`, `src/weekly_report.py`, `src/broker_score.py`, `src/owner_tracker.py`, `src/task_auditor.py`, `src/chat_poller.py`, `src/config.py`, `src/db.py`, `.env.example`.

---

## ⚠️ Задача 0: КРИТИЧЕСКИЙ БАГ — сломанная функция `_is_lead_spam_status`

**Файл:** `src/tools.py`, строки 90–100

После `return` в функции `_is_lead_spam_status` нет пустой строки перед orphaned docstring. Код выглядит так:

```python
def _is_lead_spam_status(status_id: str) -> bool:
    """True if lead is in spam stage (JUNK on portal, or legacy SPAM code)."""
    return status_id == LEAD_STATUS_JUNK or "SPAM" in status_id

    """Check whether mutating Bitrix24 API calls are permitted.
    ...
```

Нужно убрать orphaned docstring (строки 94–100), т.к. это остатки от копипасты функции `is_mutation_allowed`. Оставить только:

```python
def _is_lead_spam_status(status_id: str) -> bool:
    """True if lead is in spam stage (JUNK on portal, or legacy SPAM code)."""
    return status_id == LEAD_STATUS_JUNK or "SPAM" in status_id
```

---

## Задача 1: Main Audit Report — детализация сводки

**Файл:** `src/graph.py`, функция `report_dispatcher` (строки 656–669)

### 1a. Добавить в сводку распределение лидов по статусам

Сейчас в сводке есть `NEW (rule_1): N` и `Общие Лиды (без аудита): N`. Добавить ещё:

```python
# В начало файла, в блок импорта из tools добавить:
from tools import (
    ...
    LEAD_STATUS_CONVERTED,
    LEAD_STATUS_JUNK,
    LEAD_STATUS_NECELEVOY,
    LEAD_STATUS_AGENT,
)

# В report_dispatcher, перед формированием summary, добавить подсчёт:
qualified_count = sum(
    1 for lead in raw_leads
    if _lead_status_id(lead) == LEAD_STATUS_CONVERTED
)
spam_count = sum(
    1 for lead in raw_leads
    if _lead_status_id(lead) == LEAD_STATUS_JUNK
)
necelevoy_count = sum(
    1 for lead in raw_leads
    if _lead_status_id(lead) == LEAD_STATUS_NECELEVOY
)
agent_count = sum(
    1 for lead in raw_leads
    if _lead_status_id(lead) == LEAD_STATUS_AGENT
)

# Обновить summary:
summary = (
    f"b24-ai-auditor v2 — сводка\n"
    f"Дата: {now}\n\n"
    f"Всего нарушений: {len(violations)}\n"
    f"Отделов: {len(dept_groups)}\n"
    f"Лидов в CRM: {len(raw_leads)}\n"
    f"  NEW (rule_1): {new_leads_count}\n"
    f"  Квалифицирован: {qualified_count}\n"
    f"  Спам: {spam_count}\n"
    f"  Нецелевой: {necelevoy_count}\n"
    f"  Агент: {agent_count}\n"
    f"  Общие Лиды (без аудита): {shared_leads_count}\n"
    f"Сделок покупателей: {len(buyers_deals)} "
    f"(на аудите: {audited_buyers})\n"
    f"Сделок продавцов: {seller_deals_count}\n"
)
```

### 1b. Добавить раздел «сотрудников без нарушений» в отчёты по отделам

В цикле `for dept_name in sorted(dept_groups)`, после заголовка отдела, добавить подсчёт сотрудников без нарушений:

```python
# После строки 711 (после заголовка отдела)
# Получаем всех сотрудников этого отдела
dept_user_ids = set()
for uid, display in user_map.items():
    if f"({dept_name})" in display:
        dept_user_ids.add(uid)

violator_ids = {_coerce_int(v.get("responsible_id", 0)) for v in dept_violations}
clean_in_dept = dept_user_ids - violator_ids - inactive_users

# Добавить строку после "Сделки: N":
lines.append(f"  ✅ Без нарушений: {len(clean_in_dept)}")
```

### 1c. Вернуть блок «📞 ЗВОНКИ» (сейчас отключён, строка 763)

Заменить комментарий `# Блок "📞 ЗВОНКИ" временно отключён` на реальный блок:

```python
# После отчёта по отделу, добавить блок звонков:
call_violations = [
    v for v in dept_violations
    if "missed_callback" in str(v.get("rule", ""))
]
if call_violations:
    lines.append("")
    lines.append("📞 ЗВОНКИ:")
    for v in call_violations:
        uid = _coerce_int(v.get("responsible_id", 0))
        name_only = user_map.get(uid, f"ID:{uid}").split(" (")[0]
        entity_type = str(v.get("entity_type", "?"))
        entity_id = _coerce_int(v.get("entity_id", 0))
        link = _build_crm_link(entity_type, entity_id)
        lines.append(f"   • {name_only} — {link}")
```

### 1d. Удалить мёртвую функцию `_format_report`

Функция `_format_report` (строки 469–548) нигде не вызывается. Удалить её полностью.

---

## Задача 2: Weekly Report — тренды и сводка по типам

**Файл:** `src/weekly_report.py`

### 2a. Добавить сравнение с предыдущей неделей (тренд)

В функцию `format_weekly_report` добавить параметр `prev_violators_count: int = 0` и `prev_total_violations: int = 0`.

В `main()`, перед вызовом `format_weekly_report`, получить данные за предыдущую неделю:

```python
# В main(), после получения violators, clean:
prev_week_start = (now - timedelta(days=14)).isoformat()
prev_violators, _ = get_weekly_stats(prev_week_start)
prev_total = sum(v['total_violations'] for v in prev_violators)

# Передать в format_weekly_report:
main_report = format_weekly_report(
    violators, clean, week_start, now,
    prev_violators_count=len(prev_violators),
    prev_total_violations=prev_total,
)
```

В сводке (строка 108) добавить:

```python
if prev_total_violations > 0 or total_violations > 0:
    delta = total_violations - prev_total_violations
    if delta > 0:
        trend = f"↑ +{delta} к прошлой неделе"
    elif delta < 0:
        trend = f"↓ {delta} к прошлой неделе"
    else:
        trend = "без изменений"
    lines.append(f"Динамика: {trend}")
```

### 2b. Вынести RULE_ADVICE в конфигурацию

**Файл:** `src/config.py`

Добавить поле:

```python
rules_advice_json: str = Field(
    default='{}',
    validation_alias="RULES_ADVICE_JSON",
)

@property
def rules_advice(self) -> dict[str, str]:
    try:
        return json.loads(self.rules_advice_json)
    except Exception:
        return {}
```

**Файл:** `.env.example`

Добавить строку с дефолтными рекомендациями:

```bash
RULES_ADVICE_JSON={"lead_rule_1":"обратить внимание на скорость квалификации лидов (статус «Новый» > 2 часов)","lead_rule_2":"указывать причину перевода лида в «Спам»","lead_rule_3":"указывать причину перевода лида в нецелевые","lead_missed_callback":"не пропускать входящие звонки по лидам без обратного","buyer_stage_1":"не задерживать сделки на этапе «Первый контакт» более 1 дня (стадия снята)","buyer_stage_2":"добавлять комментарии к сделкам на этапе «Подбор» (раз в 5 дней)","buyer_stage_3":"добавлять комментарии к сделкам на этапе «Показ» (раз в 3 дня)","buyer_stage_4":"не забывать комментировать сделки на этапах Переговоры / Дожим / Офер / Задаток / Сделка","buyer_stage_5":"регулярно комментировать сделки в «Отложенном спросе»","buyer_missed_callback":"не пропускать входящие звонки по сделкам без обратного"}
```

**Файл:** `src/weekly_report.py`

Заменить хардкод `RULE_ADVICE` на:

```python
from config import get_settings

def _get_rule_advice() -> dict[str, str]:
    settings = get_settings()
    advice = settings.rules_advice
    if not advice:
        # Fallback to defaults
        return {
            "lead_rule_1": "обратить внимание на скорость квалификации лидов (статус «Новый» > 2 часов)",
            "lead_rule_2": "указывать причину перевода лида в «Спам» (JUNK)",
            "lead_rule_3": "указывать причину перевода лида в нецелевые",
            "lead_missed_callback": "не пропускать входящие звонки по лидам без обратного",
            "buyer_stage_1": "не задерживать сделки на этапе «Первый контакт» более 1 дня (стадия снята)",
            "buyer_stage_2": "добавлять комментарии к сделкам на этапе «Подбор» (раз в 5 дней)",
            "buyer_stage_3": "добавлять комментарии к сделкам на этапе «Показ» (раз в 3 дня)",
            "buyer_stage_4": "не забывать комментировать сделки на этапах Переговоры / Дожим / Офер / Задаток / Сделка",
            "buyer_stage_5": "регулярно комментировать сделки в «Отложенном спросе»",
            "buyer_missed_callback": "не пропускать входящие звонки по сделкам без обратного",
        }
    return advice

# В format_weekly_report использовать:
advice_map = _get_rule_advice()
advice = advice_map.get(rule.strip())
```

### 2c. Добавить сводку по типам нарушений в разрезе отделов

В `format_weekly_report` после сводки добавить (только если `dept_name` задан):

```python
# После строки 108 (перед return)
if dept_name:
    total_lead = sum(v['lead_violations'] for v in violators)
    total_deal = sum(v['deal_violations'] for v in violators)
    total_missed = sum(v['missed_call_violations'] for v in violators)
    if total_violations > 0:
        lines.append(f"По типам: 📋лиды {total_lead} ({(total_lead/total_violations)*100:.0f}%), 🏠сделки {total_deal} ({(total_deal/total_violations)*100:.0f}%), 📞звонки {total_missed} ({(total_missed/total_violations)*100:.0f}%)")
```

---

## Задача 3: Broker Scorecard — улучшения

**Файл:** `src/broker_score.py`

### 3a. Добавить динамику по собственникам

В `format_scorecard`, после строки с `KPI`, добавить сравнение с прошлой неделей:

```python
# В async_main, получить данные за предыдущую неделю:
prev_since_date = (now - timedelta(days=37)).strftime("%Y-%m-%d")
prev_owners = await get_owner_contacts(bx, broker_id, prev_since_date, settings.contact_owner_type_id)
# Вычесть текущие, оставив только за предыдущую неделю
prev_week_only = prev_owners["total"] - owners["total"]

# В format_scorecard добавить параметр prev_week_owners: int = 0
# И после строки KPI:
if prev_week_only > 0:
    delta = owners["total"] - prev_week_only
    if delta > 0:
        lines.append(f"  Динамика: ↑ +{delta} за неделю")
    elif delta < 0:
        lines.append(f"  Динамика: ↓ {delta} за неделю")
```

### 3b. Обработка ошибок API без падения

В `async_main` заменить `sys.exit(1)` на отправку сообщения об ошибке в чат:

```python
# Вместо:
# print(f"Брокер с ID {broker_id} не найден ни в БД, ни в API Bitrix24")
# sys.exit(1)

# Написать:
error_msg = f"❌ Брокер с ID {broker_id} не найден ни в БД, ни в API"
print(error_msg)
if not settings.dry_run:
    from notify import send_chat_message_chunked
    send_chat_message_chunked(settings.report_chat_id, error_msg)
return  # вместо sys.exit(1)
```

### 3c. Убрать дублирование с chat_poller.py

В `broker_score.py` вынести основную логику в отдельную функцию:

```python
async def build_scorecard_for_broker(
    broker_id: int,
    bx: Bitrix,
    settings,
) -> str | None:
    """Build scorecard report for a broker. Returns report string or None on error."""
    now = datetime.now(timezone.utc)
    since_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    
    broker = get_broker_info(broker_id)
    if not broker:
        try:
            users = await bx.get_all("user.get", {"FILTER": {"ID": broker_id}})
            if users:
                u = users[0]
                name = ((u.get("NAME") or "") + " " + (u.get("LAST_NAME") or "")).strip() or f"ID: {broker_id}"
                broker = {"id": broker_id, "name": name, "department": "API (отдел неизвестен)"}
            else:
                return None
        except Exception as e:
            logger.error(f"Error fetching user from API: {e}")
            return None
    
    violations = get_violations(broker_id, since_date)
    owners = await get_owner_contacts(bx, broker_id, since_date, settings.contact_owner_type_id)
    score = score_broker(violations, owners, settings.owner_kpi_target)
    return format_scorecard(broker, violations, owners, score, since_date, now, settings.owner_kpi_target)
```

В `chat_poller.py` заменить дублирующийся код на вызов `build_scorecard_for_broker`.

---

## Задача 4: Owner Tracker — улучшения

**Файл:** `src/owner_tracker.py`, `src/config.py`

### 4a. Показывать KPI за текущий месяц

В `format_owners_report` добавить расчёт KPI:

```python
# После строки 183 (перед return)
# KPI за текущий месяц
settings = get_settings()
kpi_target = settings.owner_kpi_target
# Считаем только контакты за текущий месяц
current_month_start = now.strftime("%Y-%m") + "-01"
monthly_contacts = [
    c for contacts_list in broker_groups.values()
    for c in contacts_list
    if (c.get("DATE_CREATE") or "")[:7] == now.strftime("%Y-%m")
]
monthly_count = len(monthly_contacts)
monthly_pct = int(monthly_count / kpi_target * 100) if kpi_target else 0

lines.extend([
    f"🎯 KPI на {now.strftime('%B')}: {kpi_target} собственников",
    f"   Выполнено: {monthly_count} ({monthly_pct}%)",
])
```

### 4b. Вынести `sales_depts` в конфигурацию

**Файл:** `src/config.py`

Добавить поле:

```python
owner_sales_dept_ids_json: str = Field(
    default='[42, 44, 46, 50, 60, 66]',
    validation_alias="OWNER_SALES_DEPT_IDS_JSON",
)

@property
def owner_sales_dept_ids(self) -> list[int]:
    try:
        return json.loads(self.owner_sales_dept_ids_json)
    except Exception:
        return [42, 44, 46, 50, 60, 66]
```

**Файл:** `src/owner_tracker.py`

Заменить хардкод:

```python
# Было:
sales_depts = [42, 44, 46, 50, 60, 66]  # Все отделы РОПов

# Стало:
sales_depts = settings.owner_sales_dept_ids
```

### 4c. Починить двойной импорт send_chat_message_chunked

На строке 15 был удалён неиспользуемый импорт в прошлой сессии. Проверить, что остался только локальный импорт на строке ~261 (внутри `async_main`). Если на строке 15 остался — удалить.

---

## Задача 5: Task Auditor — улучшения

**Файл:** `src/task_auditor.py`, `src/config.py`

### 5a. Вынести EXCLUDED_USER_NAMES в конфигурацию

**Файл:** `src/config.py`

```python
task_auditor_exclude_users_json: str = Field(
    default='["Агентство Недвижимости", "Вера Волкова", "Светлана Щербакова", "Марина Володина"]',
    validation_alias="TASK_AUDITOR_EXCLUDE_USERS_JSON",
)

@property
def task_auditor_exclude_users(self) -> set[str]:
    try:
        return set(json.loads(self.task_auditor_exclude_users_json))
    except Exception:
        return set()
```

**Файл:** `src/task_auditor.py`

```python
# Было:
EXCLUDED_USER_NAMES = {
    "Агентство Недвижимости",
    "Вера Волкова",
    "Светлана Щербакова",
    "Марина Володина",
}

# Стало:
def _get_excluded_users() -> set[str]:
    settings = get_settings()
    excluded = settings.task_auditor_exclude_users
    if not excluded:
        return {"Агентство Недвижимости", "Вера Волкова", "Светлана Щербакова", "Марина Володина"}
    return excluded
```

И заменить все `EXCLUDED_USER_NAMES` на `_get_excluded_users()`.

### 5b. Добавить топ-3 проблемных сотрудников

В `format_global_report`, перед сводкой, добавить:

```python
# Топ-3 по просрочкам
all_users = back_office_data + rop_data
top_overdue = sorted(
    all_users,
    key=lambda u: len(u["stats"]["overdue"]),
    reverse=True,
)[:3]

if top_overdue and any(len(u["stats"]["overdue"]) > 0 for u in top_overdue):
    lines.extend([
        "═══════════════════════════════",
        "🔴 ТОП-3 ПО ПРОСРОЧКАМ",
        "═══════════════════════════════",
    ])
    for i, u in enumerate(top_overdue, 1):
        name = (u.get("NAME", "") + " " + u.get("LAST_NAME", "")).strip()
        overdue_cnt = len(u["stats"]["overdue"])
        if overdue_cnt > 0:
            lines.append(f"{i}. {name} — {overdue_cnt} просроченных задач")
    lines.append("")
```

### 5c. Отправлять РОПам их отделы отдельно (опционально)

Если нужно, добавить после отправки общего отчёта:

```python
# Отправка individual ROP reports
dept_map = settings.dept_chat_map
if dept_map:
    for u in rop_unique:
        dept_ids = u.get("UF_DEPARTMENT", [])
        if dept_ids:
            primary_dept = int(dept_ids[0])
            chat_id = dept_map.get(primary_dept)
            if chat_id and chat_id != settings.report_chat_id:
                rop_name = (u.get("NAME", "") + " " + u.get("LAST_NAME", "")).strip()
                rop_report = (
                    f"b24-ai-auditor — Ваши задачи\n"
                    f"Дата: {now.strftime('%d.%m.%Y')}\n\n"
                    f"👤 {rop_name}\n"
                    f"Активных задач: {u['stats']['total']}\n"
                    f"Просрочено: {len(u['stats']['overdue'])}\n"
                )
                send_chat_message_chunked(chat_id, rop_report)
```

---

## Задача 6: Chat Poller — новые команды

**Файл:** `src/chat_poller.py`

### 6a. Добавить команду `/weekly` — недельный отчёт из чата

Добавить второй паттерн:

```python
COMMAND_WEEKLY = re.compile(
    r"^(?:/weekly|!weekly|!неделя|недельный отчёт)$",
    re.IGNORECASE,
)
```

В `async_main`, после обработки `find_command`, добавить:

```python
# Check for /weekly command
for m in messages:
    text = m.get("text", "") or m.get("message", "")
    if COMMAND_WEEKLY.search(text.strip()):
        logger.info("Weekly report requested from chat")
        from weekly_report import format_weekly_report
        from db import get_weekly_stats, init_db
        init_db()
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=7)).isoformat()
        violators, clean = get_weekly_stats(week_start)
        report = format_weekly_report(violators, clean, week_start, now)
        if not settings.dry_run:
            send_chat_message_chunked(chat_id, report)
```

### 6b. Добавить команду `/dept <название>` — отчёт по отделу

Добавить паттерн:

```python
COMMAND_DEPT = re.compile(
    r"(?:/dept|!dept|!отдел)\s+(.+)",
    re.IGNORECASE,
)
```

Обработка:

```python
for m in messages:
    text = m.get("text", "") or m.get("message", "")
    match = COMMAND_DEPT.search(text.strip())
    if match:
        dept_query = match.group(1).strip()
        logger.info("Department report requested: %s", dept_query)
        from db import get_weekly_stats, init_db
        init_db()
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=7)).isoformat()
        violators, clean = get_weekly_stats(week_start)
        
        # Filter by department name (fuzzy match)
        dept_violators = [
            v for v in violators
            if dept_query.lower() in (v.get("department") or "").lower()
        ]
        dept_clean = [
            c for c in clean
            if dept_query.lower() in (c.get("department") or "").lower()
        ]
        
        dept_name = dept_violators[0]["department"] if dept_violators else (
            dept_clean[0]["department"] if dept_clean else dept_query
        )
        
        report = format_weekly_report(dept_violators, dept_clean, week_start, now, dept_name=dept_name)
        if not settings.dry_run:
            send_chat_message_chunked(chat_id, report)
```

---

## Задача 7: Lead Opened Poller — мониторинг

**Файл:** `src/lead_opened_poller.py`

### 7a. Добавить логгирование в БД

Добавить таблицу в `db.py` (в `init_db`):

```sql
CREATE TABLE IF NOT EXISTS opened_leads_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_time TEXT NOT NULL,
    leads_fixed INTEGER NOT NULL DEFAULT 0
);
```

В `lead_opened_poller.py`, после `fix_opened_leads`, сохранять результат:

```python
from db import get_connection

def log_opened_fix(leads_fixed: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO opened_leads_log (run_time, leads_fixed) VALUES (?, ?)",
            (now, leads_fixed)
        )
```

Вызывать `log_opened_fix(ok)` после `fix_opened_leads`.

### 7b. Заменить `time.sleep(0.1)` на asyncio

Обернуть в асинхронную функцию:

```python
import asyncio

async def fix_opened_leads_async(*, dry_run: bool) -> int:
    """Async version with concurrent updates."""
    raw = _bx_get_all_sync("crm.lead.list", {
        "filter": {"OPENED": "Y"},
        "select": ["ID"],
    })
    leads = raw if isinstance(raw, list) else list(raw.values()) if isinstance(raw, dict) else []
    lead_ids = sorted(_coerce_int(item.get("ID")) for item in leads if isinstance(item, dict))
    lead_ids = [lid for lid in lead_ids if lid]
    
    if not lead_ids:
        logger.info("No leads with OPENED=Y")
        return 0
    
    if dry_run:
        logger.info("[dry-run] Would set OPENED=N for %d leads: %s", len(lead_ids), lead_ids[:10])
        return len(lead_ids)
    
    bx = _get_bitrix()
    sem = asyncio.Semaphore(5)  # max 5 concurrent
    
    async def update_one(lead_id: int) -> bool:
        async with sem:
            try:
                await asyncio.to_thread(bx.call, "crm.lead.update", {"id": lead_id, "fields": {"OPENED": "N"}})
                return True
            except Exception:
                logger.exception("Failed to update lead_id=%s", lead_id)
                return False
    
    results = await asyncio.gather(*[update_one(lid) for lid in lead_ids])
    ok = sum(results)
    logger.info("Set OPENED=N for %d/%d leads", ok, len(lead_ids))
    return ok
```

---

## Порядок выполнения

| # | Задача | Приоритет |
|---|--------|-----------|
| 0 | Критический баг `_is_lead_spam_status` | 🔴 СРОЧНО |
| 1a | Детализация сводки лидов | 🟡 Важно |
| 1b | Раздел «без нарушений» в отчётах по отделам | 🟡 Важно |
| 1c | Вернуть блок «📞 ЗВОНКИ» | 🟡 Важно |
| 1d | Удалить `_format_report` | 🟢 Чистка |
| 2a | Тренды в weekly report | 🟡 Важно |
| 2b | RULE_ADVICE в конфиг | 🟢 Удобство |
| 2c | Сводка по типам нарушений | 🟡 Важно |
| 3a | Динамика по собственникам | 🟢 Удобство |
| 3b | Обработка ошибок без падения | 🟡 Важно |
| 3c | Дедупликация broker_score/chat_poller | 🟢 Чистка |
| 4a | KPI за текущий месяц в owner tracker | 🟡 Важно |
| 4b | sales_depts в конфиг | 🟢 Удобство |
| 4c | Починить двойной импорт | 🟢 Чистка |
| 5a | EXCLUDED_USER_NAMES в конфиг | 🟢 Удобство |
| 5b | Топ-3 проблемных сотрудников | 🟡 Важно |
| 5c | РОПам — отдельные отчёты | 🟢 Опционально |
| 6a | Команда /weekly | 🟡 Важно |
| 6b | Команда /dept | 🟢 Удобство |
| 7a | Логирование opened_leads в БД | 🟢 Удобство |
| 7b | asyncio вместо sleep | 🟢 Оптимизация |

---

## Проверка

1. `.\make.cmd lint`
2. `.venv/bin/python -m pytest tests/ -v`
3. `.\make.cmd dry-run` — проверить сводку и отчёты по отделам
4. `python src/weekly_report.py` — проверить тренды
5. `python src/broker_score.py <ID>` — проверить динамику
6. `python src/owner_tracker.py` — проверить KPI за месяц
7. `python src/task_auditor.py` — проверить топ-3
