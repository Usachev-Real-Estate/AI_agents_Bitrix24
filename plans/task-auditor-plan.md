# План: Агент контроля задач Бэк-Офиса и РОП

## Концепция

Отдельный скрипт [`src/task_auditor.py`](src/task_auditor.py), который:
1. Находит сотрудников Бэк-Офиса (по ID отдела) и РОПов (по `WORK_POSITION`)
2. Для каждого запрашивает активные задачи через `tasks.task.list`
3. Проверяет дедлайны, считает просрочку
4. Формирует отчёт и отправляет в чаты

**LLM не используется** — все проверки детерминированные.

---

## Как определяются пользователи

### Бэк-Офис
```python
# user.search с фильтром по отделу
bx.call("user.search", {
    "FILTER": {"UF_DEPARTMENT": settings.back_office_dept_id}
})
```
Нужен `BACK_OFFICE_DEPT_ID` в `.env`.

### РОП
```python
# user.search с фильтром по должности
bx.call("user.search", {
    "FILTER": {"WORK_POSITION": "Руководитель отдела продаж (РОП)"}
})
```
**Важно:** у каждого РОПа нужно определить его отдел через `UF_DEPARTMENT`, чтобы отправить отчёт в правильный чат из `DEPT_CHAT_MAP`.

---

## API для задач

```python
# tasks.task.list для одного пользователя (только активные задачи)
bx.get_all("tasks.task.list", {
    "filter": {"RESPONSIBLE_ID": user_id, "REAL_STATUS": [2, 3, 4]},
    "select": ["ID", "TITLE", "DEADLINE", "STATUS", "CREATED_DATE", "RESPONSIBLE_ID"],
})
```

**Статусы задач Bitrix24:**
| Код | Значение | Учитываем? |
|-----|----------|------------|
| 2 | Принята | ✅ Активная |
| 3 | Выполняется | ✅ Активная |
| 4 | На проверке | ✅ Активная |
| 5 | Завершена | ❌ Пропускаем |
| 6 | Отложена | ❌ Пропускаем |

**Правило просрочки:**
```python
deadline = parse_date(task["DEADLINE"])
if deadline and deadline < now and task["STATUS"] in [2, 3, 4]:
    task["is_overdue"] = True
    task["days_overdue"] = (now - deadline).days
```

---

## Формат отчёта

### Глобальный отчёт (REPORT_CHAT_ID)

```
b24-ai-auditor — Контроль задач
Дата: 15.06.2026

═══════════════════════════════
🔴 БЭК-ОФИС — 5 сотрудников
═══════════════════════════════

👤 Иванова Анна — 12 задач (3 просрочено)
   1. 🔴 #456 «Подготовить договор» — просрочена на 3 дн. (дедлайн: 12.06.2026)
   2. 🔴 #478 «Отправить счёт» — просрочена на 1 дн. (дедлайн: 14.06.2026)
   3. 🟡 #490 «Проверить документы» — дедлайн 18.06.2026
   ...

👤 Петрова Мария — 8 задач (0 просрочено)
   ...

═══════════════════════════════
🔴 РОПы — 6 руководителей
═══════════════════════════════

👤 Кретов Дмитрий (Отдел продаж 1) — 5 задач (1 просрочено)
   1. 🔴 #512 «Согласовать план» — просрочена на 2 дн. (дедлайн: 13.06.2026)
   ...

═══════════════════════════════
📊 СВОДКА
═══════════════════════════════
Бэк-Офис: 45 задач, 12 просрочено (27%)
РОПы: 18 задач, 2 просрочено (11%)
Всего: 63 задачи, 14 просрочено (22%)
```

### Отчёт для Бэк-Офиса (BACK_OFFICE_CHAT_ID)

Такой же как секция «Бэк-Офис» выше, но с напоминанием:
```
⚠️ Коллеги, проверьте просроченные задачи. 
Сроки выполнения — важная часть регламента.
```

### Отчёт для каждого РОПа (чат из DEPT_CHAT_MAP)

Персональный отчёт только по этому РОПу:
```
b24-ai-auditor — Ваши задачи
Дата: 15.06.2026

👤 Кретов Дмитрий — 5 задач (1 просрочено)
   ...
```

---

## Конфигурация (.env)

Добавить:
```bash
# ============================================
# Task Auditor (Бэк-Офис + РОП)
# ============================================
BACK_OFFICE_DEPT_ID=XX           # ID отдела «Бэк-офис» (узнать через user.search)
BACK_OFFICE_CHAT_ID=XXXXX        # Чат для отчётов по Бэк-Офису
```

В [`config.py`](src/config.py) добавить поля:
```python
back_office_dept_id: int = Field(default=0, validation_alias="BACK_OFFICE_DEPT_ID")
back_office_chat_id: int = Field(default=0, validation_alias="BACK_OFFICE_CHAT_ID")
```

---

## Структура `src/task_auditor.py`

```python
"""Task auditor: Back-Office + ROP task deadline monitoring."""

# ── Константы ──────────────────────────────────────────
ACTIVE_TASK_STATUSES = [2, 3, 4]  # принята, выполняется, на проверке

# ── API-хелперы ────────────────────────────────────────
def fetch_users_by_dept(dept_id: int) -> list[dict]:
    """Найти пользователей по UF_DEPARTMENT."""

def fetch_users_by_position(position: str) -> list[dict]:
    """Найти пользователей по WORK_POSITION."""

def fetch_user_tasks(user_id: int) -> list[dict]:
    """Получить активные задачи пользователя."""

# ── Анализ ─────────────────────────────────────────────
def classify_tasks(tasks: list[dict], now: datetime) -> dict:
    """Разделить задачи на просроченные / активные / остальные.
    Returns: {"overdue": [...], "active": [...], "total": N}"""

# ── Форматирование ─────────────────────────────────────
def format_task_report(users_data: list[dict], title: str, now: datetime) -> str:
    """Сформировать текстовый отчёт."""

def format_single_rop_report(user_data: dict, now: datetime) -> str:
    """Персональный отчёт для одного РОПа."""

# ── Главная ────────────────────────────────────────────
def main():
    settings = get_settings()
    now = datetime.now(timezone.utc)
    
    # 1. Собрать пользователей
    back_office_users = fetch_users_by_dept(settings.back_office_dept_id)
    rop_users = fetch_users_by_position("Руководитель отдела продаж (РОП)")
    
    # 2. Для каждого — получить задачи
    for user in back_office_users + rop_users:
        user["tasks"] = fetch_user_tasks(user["ID"])
        user["stats"] = classify_tasks(user["tasks"], now)
    
    # 3. Сформировать и отправить ОДИН отчёт
    #    - Всё → REPORT_CHAT_ID (чат 22358)
```

---

## Что нужно сделать (6 пунктов)

| # | Действие | Файл |
|---|----------|------|
| 1 | Добавить `BACK_OFFICE_DEPT_ID` | [`.env.example`](.env.example), [`config.py`](src/config.py) |
| 2 | Создать `src/task_auditor.py` | Новый файл (~180 строк) |
| 3 | Добавить `make task-check` | [`Makefile`](Makefile:1), [`make.cmd`](make.cmd:1) |
| 4 | Найти ID отдела «Бэк-офис» | Скрипт `scripts/list_departments.py` или через `user.search` |
| 5 | Создать промпт для реализации | `plans/prompts/step-50-task-auditor.md` |

**Все отчёты → чат 22358 (`REPORT_CHAT_ID`).** Отдельные чаты для Бэк-Офиса/РОПов пока не нужны.

---

## Cron-расписание

Запуск раз в день утром (10:00 МСК = 07:00 UTC), пн–пт:

```bash
0 7 * * 1-5 cd /opt/b24-ai-auditor && venv/bin/python src/task_auditor.py >> logs/cron.log 2>&1
```

---

## Производительность

Для 5 сотрудников Бэк-Офиса + 6 РОПов = 11 вызовов `tasks.task.list`. 
Каждый вызов возвращает активные задачи (обычно 5-20 на человека).
Общее время выполнения: ~5-10 секунд.
