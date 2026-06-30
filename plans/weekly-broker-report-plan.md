# План: Хранение нарушений + Недельный отчёт по брокерам

## SQLite: сколько выдержит?

**Короткий ответ:** с запасом на годы.

| Параметр | Значение |
|----------|----------|
| Макс. размер БД | 281 TB (теоретический) |
| Практический предел | Десятки GB без проблем |
| Строк в таблице | Миллионы (с индексами — доли секунды на запрос) |

**Расчёт для нашего проекта (50 брокеров):**
- ~5 нарушений на брокера за запуск
- 2 запуска/день × 5 дней = 10 запусков/неделя
- 50 × 5 × 10 = **2 500 записей/неделя**
- ~500 байт на запись = **1.25 MB/неделя**, **65 MB/год**
- Даже через 10 лет — 650 MB, SQLite справляется играючи

SQLite используется в продакшене Chromium, iOS, Android, PostgreSQL-клиентах — для нашей задачи более чем достаточно.

---

## Архитектура

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  audit_v2()  │────▶│ report_dispatcher│────▶│  src/db.py      │
│  (2 раза/день)│     │  (отправка в чаты) │     │  save_violations│
└──────────────┘     └──────────────────┘     │  upsert_brokers │
                                              └────────┬────────┘
                                                       │
                                              ┌────────▼────────┐
                                              │ data/violations.db│
                                              │  ┌─────────────┐ │
                                              │  │ audit_runs  │ │
                                              │  │ violations  │ │
                                              │  │ brokers     │ │
                                              │  └─────────────┘ │
                                              └────────┬────────┘
                                                       │
┌──────────────┐     ┌──────────────────┐             │
│ make weekly- │────▶│ src/weekly_report│◄────────────┘
│ report       │     │ .py              │
│ (пт 17:30)   │     │                  │────▶ Чат REPORT_CHAT_ID
└──────────────┘     └──────────────────┘
```

---

## Схема БД

### Таблица `audit_runs`
```sql
CREATE TABLE IF NOT EXISTS audit_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_time TEXT NOT NULL,            -- ISO: 2026-06-15T10:00:00+00:00
    total_leads INTEGER DEFAULT 0,
    total_buyer_deals INTEGER DEFAULT 0,
    total_seller_deals INTEGER DEFAULT 0,
    total_violations INTEGER DEFAULT 0
);
```

### Таблица `violations`
```sql
CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
    entity_type TEXT NOT NULL,         -- 'lead' или 'deal'
    entity_id INTEGER NOT NULL,
    responsible_id INTEGER NOT NULL,
    responsible_name TEXT NOT NULL,
    department TEXT NOT NULL DEFAULT '',
    rule TEXT NOT NULL,                -- 'lead_rule_1', 'buyer_stage_3', ...
    severity TEXT NOT NULL,            -- 'high', 'medium', 'very high'
    reason TEXT NOT NULL,
    detected_at TEXT NOT NULL,         -- ISO
    UNIQUE(audit_run_id, entity_type, entity_id, rule)
);

CREATE INDEX IF NOT EXISTS idx_violations_responsible 
    ON violations(responsible_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_violations_detected 
    ON violations(detected_at);
```

### Таблица `brokers` (снимок активных брокеров)
```sql
CREATE TABLE IF NOT EXISTS brokers (
    responsible_id INTEGER PRIMARY KEY,
    responsible_name TEXT NOT NULL,
    department TEXT NOT NULL DEFAULT '',
    lead_count INTEGER DEFAULT 0,     -- лидов в последнем аудите
    deal_count INTEGER DEFAULT 0,     -- сделок в последнем аудите
    last_seen TEXT NOT NULL           -- дата последнего аудита
);
```

**Зачем таблица `brokers`:** чтобы знать ВСЕХ брокеров и показывать «чистых» (0 нарушений). Обновляется при каждом аудите.

---

## Формат недельного отчёта

```
b24-ai-auditor — Недельный отчёт по брокерам
Период: 09.06.2026 – 15.06.2026

═══════════════════════════════
🔴 БРОКЕРЫ С НАРУШЕНИЯМИ (12)
═══════════════════════════════

1. 🔴 Иванов Иван (Отдел продаж) — 8 нарушений
   📋 Лиды: 3 (rule_1 — 2, rule_2 — 1)
   📞 Звонки: 2 (пропущенные без обратного)
   🏠 Сделки: 3 (buyer_stage_2 — 2, buyer_stage_3 — 1)
   ⚠️ Рекомендация: обратить внимание на скорость квалификации лидов
   ⚠️ Рекомендация: не пропускать входящие звонки без обратного

2. 🟡 Петров Пётр (Отдел продаж) — 3 нарушения
   🏠 Сделки: 3 (buyer_stage_4 — 3)
   ⚠️ Рекомендация: заполнять результат показа в сделках

...

═══════════════════════════════
🟢 ЧИСТЫЕ БРОКЕРЫ (5)
═══════════════════════════════
(без нарушений, с активными лидами/сделками)

1. 🟢 Сидоров Алексей (Отдел продаж) — 12 лидов, 5 сделок
2. 🟢 Кузнецова Мария (Отдел продаж) — 8 лидов, 3 сделки
...

═══════════════════════════════
📊 СВОДКА
═══════════════════════════════
Всего брокеров с лидами/сделками: 17
С нарушениями: 12 (71%)
Чистых: 5 (29%)
Всего нарушений: 47
```

**Важно:** чистые брокеры показываются только если у них `lead_count > 0` или `deal_count > 0` (из таблицы `brokers`). Брокеры без лидов и сделок исключаются — у них просто нечего нарушать.

---

## SQL-запросы для отчёта

### Брокеры с нарушениями (с детализацией)
```sql
SELECT 
    v.responsible_id,
    v.responsible_name,
    v.department,
    COUNT(*) as total_violations,
    SUM(CASE WHEN v.entity_type = 'lead' AND v.rule NOT LIKE '%missed%' THEN 1 ELSE 0 END) as lead_violations,
    SUM(CASE WHEN v.entity_type = 'deal' AND v.rule NOT LIKE '%missed%' THEN 1 ELSE 0 END) as deal_violations,
    SUM(CASE WHEN v.rule LIKE '%missed_callback%' THEN 1 ELSE 0 END) as missed_call_violations,
    GROUP_CONCAT(DISTINCT v.rule) as rules
FROM violations v
WHERE v.detected_at >= :week_start
GROUP BY v.responsible_id
ORDER BY total_violations DESC;
```

### Чистые брокеры
```sql
SELECT 
    b.responsible_id,
    b.responsible_name,
    b.department,
    b.lead_count,
    b.deal_count
FROM brokers b
LEFT JOIN violations v ON b.responsible_id = v.responsible_id 
    AND v.detected_at >= :week_start
WHERE v.responsible_id IS NULL
    AND (b.lead_count > 0 OR b.deal_count > 0)
ORDER BY b.responsible_name;
```

---

## Рекомендации по правилам (маппинг rule → совет)

```python
RULE_ADVICE: dict[str, str] = {
    "lead_rule_1": "обратить внимание на скорость квалификации лидов (статус «Новый» > 2 часов)",
    "lead_rule_2": "указывать причину перевода лида в СПАМ",
    "lead_rule_3": "указывать причину перевода лида в нецелевые",
    "lead_missed_callback": "не пропускать входящие звонки по лидам без обратного",
    "buyer_stage_1": "не задерживать сделки на этапе «Первый контакт» более 1 дня",
    "buyer_stage_2": "добавлять комментарии к сделкам на этапе «Подбор»",
    "buyer_stage_3": "заполнять дату показа в сделках на этапе «Показ»",
    "buyer_stage_4": "заполнять результат показа в сделках на этапе «Показ проведен»",
    "buyer_stage_5": "регулярно комментировать сделки в «Отложенном спросе»",
    "buyer_missed_callback": "не пропускать входящие звонки по сделкам без обратного",
}
```

---

## Что нужно сделать (6 файлов)

| # | Действие | Файл |
|---|----------|------|
| 1 | **Создать** модуль `src/db.py` | Новый файл: инициализация БД, `save_audit_run()`, `save_violations()`, `upsert_brokers()` |
| 2 | **Изменить** `report_dispatcher` — после отправки отчётов сохранять нарушения в БД | `src/graph.py` |
| 3 | **Создать** скрипт `src/weekly_report.py` | Новый файл: запрос к БД, форматирование отчёта, отправка в чат |
| 4 | **Добавить** цель `weekly-report` | `Makefile` + `make.cmd` |
| 5 | **Добавить** volume `data/` | `docker-compose.yml` (для персистентности БД) |
| 6 | **Добавить** `data/` в `.gitignore` | `.gitignore` |

---

## Детали реализации

### 1. `src/db.py` — интерфейс

```python
import sqlite3
from pathlib import Path

DB_PATH = Path("data/violations.db")

def get_connection() -> sqlite3.Connection:
    """Return connection with WAL mode for better concurrent reads."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db() -> None:
    """Create tables if not exist (idempotent)."""

def save_audit_run(run_time: str, total_leads: int, total_buyer_deals: int,
                   total_seller_deals: int, total_violations: int) -> int:
    """Insert audit_run row, return run_id."""

def save_violations(audit_run_id: int, violations: list[dict], 
                    user_map: dict[int, str], now: str) -> int:
    """INSERT OR IGNORE violations, return count saved."""

def upsert_brokers(user_map: dict[int, str], leads: list, buyer_deals: list,
                   seller_deals: list, now: str) -> None:
    """Update brokers table: name, dept, lead/deal counts, last_seen."""

def get_weekly_stats(week_start: str) -> tuple[list, list]:
    """Return (violators_list, clean_brokers_list) for weekly report."""
```

### 2. Точка сохранения в `report_dispatcher`

После успешной отправки отчётов (строка 727 в graph.py), добавить:

```python
from db import save_audit_run, save_violations, upsert_brokers

run_id = save_audit_run(now, len(raw_leads), len(buyers_deals), 
                         seller_deals_count, current_count)
save_violations(run_id, violations, user_map, now)
upsert_brokers(user_map, raw_leads, buyers_deals, 
               state.get("raw_sellers_deals", []), now)
```

### 3. `src/weekly_report.py`

```python
"""Generate and send weekly broker performance report."""
from datetime import datetime, timedelta, timezone
from db import get_weekly_stats
from notify import send_chat_message_chunked
from config import get_settings

def main():
    settings = get_settings()
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    
    violators, clean = get_weekly_stats(week_start)
    
    report = format_weekly_report(violators, clean, week_start, now)
    send_chat_message_chunked(settings.report_chat_id, report)

if __name__ == "__main__":
    main()
```

### 4. Makefile

```makefile
weekly-report:
	$(PYTHON) src/weekly_report.py
```

### 5. docker-compose.yml

```yaml
volumes:
  - ./data:/app/data   # добавить к существующим
```

### 6. .gitignore

```
# Database
data/
```

---

## Cron: когда запускать недельный отчёт

Добавить в crontab запуск по пятницам в 17:30 МСК (14:30 UTC):

```bash
30 14 * * 5 cd /opt/b24-ai-auditor && venv/bin/python src/weekly_report.py >> logs/cron.log 2>&1
```
