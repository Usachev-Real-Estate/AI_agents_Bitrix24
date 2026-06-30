# План: Агент отслеживания новых собственников (KPI)

## Концепция

Отдельный скрипт [`src/owner_tracker.py`](src/owner_tracker.py), который:
1. Запрашивает контакты с `TYPE_ID = "Собственник"` через `crm.contact.list`
2. Фильтрует по дате создания — **с 01.06.2026** (начало месяца/периода)
3. Группирует по `ASSIGNED_BY_ID` (ответственный брокер)
4. Сравнивает с **планом: 10 новых собственников в месяц**
5. Показывает прогресс: сколько добавлено, сколько осталось до плана
6. Отправляет отчёт в чат 22358

**LLM не используется.**

---

## API

```python
# crm.contact.list — все контакты-собственники с начала периода
bx.get_all("crm.contact.list", {
    "filter": {
        "TYPE_ID": settings.contact_owner_type_id,  # "Собственник"
        ">=DATE_CREATE": settings.owner_kpi_since,   # "2026-06-01"
    },
    "select": ["ID", "NAME", "LAST_NAME", "ASSIGNED_BY_ID", "DATE_CREATE", "TYPE_ID"],
})
```

---

## Формат отчёта

```
b24-ai-auditor — Новые собственники (KPI: 10/мес)
Период: с 01.06.2026 по 15.06.2026 (день 15 из ~30)

═══════════════════════════════
🟢 ВЫПОЛНЯЮТ ПЛАН (≥10)
═══════════════════════════════

1. 🟢 Иванов Иван (Отдел продаж) — 12 собственников ✅ (+2 сверх плана)
   • Иванов Александр (создан 02.06.2026)
   • Петрова Мария (создан 05.06.2026)
   ...

═══════════════════════════════
🟡 В ГРАФИКЕ (5–9)
═══════════════════════════════

2. 🟡 Петров Пётр (Отдел продаж) — 7 собственников (70% плана, осталось 3)
   ...

═══════════════════════════════
🔴 ОТСТАЮТ (0–4)
═══════════════════════════════

3. 🔴 Сидоров Алексей (Отдел продаж) — 2 собственника (20% плана, осталось 8)
   ...

═══════════════════════════════
📊 СВОДКА
═══════════════════════════════
Всего брокеров: 12
Выполнили план: 3 (25%)
В графике: 5 (42%)
Отстают: 4 (33%)
Всего новых собственников: 68
План на месяц: 120 (10 × 12 брокеров)
Прогресс: 57%
```

**Логика категорий:**
- 🟢 `added >= 10` — «Выполняют план»
- 🟡 `added >= 5` — «В графике» (50%+ плана на середине месяца — ок)
- 🔴 `added < 5` — «Отстают»

---

## Конфигурация

Добавить в [`.env.example`](.env.example):
```bash
# ============================================
# Owner Tracker (KPI: 10 собственников/мес)
# ============================================
CONTACT_OWNER_TYPE_ID=Собственник   # значение TYPE_ID для контакта-собственника
OWNER_KPI_SINCE=2026-06-01          # начало периода отсчёта (YYYY-MM-DD)
OWNER_KPI_TARGET=10                 # цель: сколько собственников в месяц
```

Добавить в [`config.py`](src/config.py):
```python
contact_owner_type_id: str = Field(
    default="Собственник",
    validation_alias="CONTACT_OWNER_TYPE_ID",
)
owner_kpi_since: str = Field(
    default="2026-06-01",
    validation_alias="OWNER_KPI_SINCE",
)
owner_kpi_target: int = Field(
    default=10,
    validation_alias="OWNER_KPI_TARGET",
)
```

---

## Структура `src/owner_tracker.py`

```python
"""Owner KPI tracker: 10 new Owner-type contacts per broker per month."""

# ── Константы ──────────────────────────────────────────
GREEN_THRESHOLD_RATIO = 1.0   # ≥ 100% плана → зелёные
YELLOW_THRESHOLD_RATIO = 0.5  # ≥ 50% плана → жёлтые
                               # < 50% → красные

# ── API-хелперы ────────────────────────────────────────
async def fetch_owner_contacts(bx, type_id: str, since_date: str) -> list[dict]:
    """Получить контакты типа «Собственник» с начала периода."""

async def fetch_user_info(bx, user_ids: set[int]) -> dict[int, dict]:
    """Получить имена и отделы для списка пользователей."""

# ── Анализ ─────────────────────────────────────────────
def group_by_broker(contacts: list[dict]) -> dict[int, list[dict]]:
    """Сгруппировать контакты по ASSIGNED_BY_ID → список контактов."""

def classify_brokers(
    broker_groups: dict[int, list[dict]],
    target: int,
) -> tuple[list, list, list]:
    """Разделить брокеров на зелёных/жёлтых/красных по KPI.
    Returns: (green, yellow, red) — каждый список [(broker_id, count, contacts), ...]"""

# ── Форматирование ─────────────────────────────────────
def format_kpi_report(
    green: list, yellow: list, red: list,
    user_info: dict, target: int,
    since_date: str, now: datetime,
) -> str:
    """Сформировать KPI-отчёт."""

# ── Главная ────────────────────────────────────────────
async def async_main():
    settings = get_settings()
    now = datetime.now(timezone.utc)
    
    bx = Bitrix(settings.b24_webhook_url)
    
    contacts = await fetch_owner_contacts(
        bx, settings.contact_owner_type_id, settings.owner_kpi_since
    )
    
    broker_groups = group_by_broker(contacts)
    user_ids = set(broker_groups.keys())
    user_info = await fetch_user_info(bx, user_ids)
    
    green, yellow, red = classify_brokers(
        broker_groups, settings.owner_kpi_target
    )
    
    report = format_kpi_report(
        green, yellow, red, user_info,
        settings.owner_kpi_target,
        settings.owner_kpi_since, now,
    )
    
    if not settings.dry_run:
        send_chat_message_chunked(settings.report_chat_id, report)
    else:
        print(report)
```

---

## Что нужно сделать (4 пункта)

| # | Действие | Файл |
|---|----------|------|
| 1 | Добавить `CONTACT_OWNER_TYPE_ID`, `OWNER_KPI_SINCE`, `OWNER_KPI_TARGET` | [`.env.example`](.env.example), [`config.py`](src/config.py) |
| 2 | Создать `src/owner_tracker.py` | Новый файл (~160 строк) |
| 3 | Добавить `make owner-track` | [`Makefile`](Makefile:1), [`make.cmd`](make.cmd:1) |
| 4 | Создать промпт для реализации | `plans/prompts/step-51-owner-tracker.md` |

---

## Cron

Раз в день утром (или раз в неделю по пятницам):

```bash
# Ежедневно 10:00 МСК
0 7 * * 1-5 cd /opt/b24-ai-auditor && venv/bin/python src/owner_tracker.py >> logs/cron.log 2>&1
```
