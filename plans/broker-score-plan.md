# План: Скоринг брокера (по запросу)

## Концепция

CLI-скрипт [`src/broker_score.py`](src/broker_score.py), который по ID брокера собирает все метрики за 30 дней и выдаёт персональный отчёт.

**Запуск:**
```bash
make broker-score BROKER_ID=123
# или
python src/broker_score.py 123
```

Не ставится в cron — только ручной запуск по необходимости.

---

## Источники данных

| Метрика | Источник | Период |
|---------|----------|--------|
| Нарушения CRM | Таблица `violations` в `data/violations.db` | Последние 30 дней |
| Новые собственники | `crm.contact.list` API Bitrix24 | Последние 30 дней |
| Имя и отдел брокера | Таблица `brokers` в `data/violations.db` | — |

---

## Формат отчёта

```
b24-ai-auditor — Скоринг брокера
Дата: 15.06.2026

👤 Иванов Иван (Отдел продаж)
═══════════════════════════════

📋 НАРУШЕНИЯ CRM (за 30 дней)
  Лиды: 2
    • lead_rule_1: Лид в «Новый» > 2 часов — 2 раза
  Сделки: 1
    • buyer_stage_2: Сделка на «Подбор» > 2 дней — 1 раз
  Звонки: 1
    • lead_missed_callback: Пропущенный без обратного — 1 раз

🏠 НОВЫЕ СОБСТВЕННИКИ (за 30 дней)
  Добавлено: 12
  KPI (10/мес): ✅ выполнено (120%)

═══════════════════════════════
📊 ОЦЕНКА: 🟡 СРЕДНЕ
═══════════════════════════════
🔴 Нарушения: 4 (много)
🟢 Собственники: 12 (хорошо)
⚠️ Рекомендации:
  • Обратить внимание на скорость квалификации лидов
  • Не задерживать сделки на этапе «Подбор»
```

### Логика оценки

| Компонент | 🟢 Хорошо | 🟡 Средне | 🔴 Плохо |
|-----------|----------|----------|---------|
| Нарушения CRM | 0–2 | 3–5 | 6+ |
| Собственники | ≥10 | 5–9 | 0–4 |

**Общая оценка:**
- 🟢 Зелёный: оба компонента 🟢
- 🟡 Жёлтый: один 🟡 или смесь
- 🔴 Красный: оба 🔴

---

## Структура `src/broker_score.py`

```python
"""Broker scorecard: CRM violations + owner KPI over 30 days."""

import sys
from datetime import datetime, timedelta, timezone

# ── Данные ─────────────────────────────────────────────
def get_broker_info(broker_id: int) -> dict | None:
    """Имя и отдел из таблицы brokers."""

def get_violations(broker_id: int, since_date: str) -> dict:
    """Нарушения CRM за период.
    Returns: {"total": N, "by_rule": {rule: count}, "by_type": {lead/deal/call: count}}"""

def get_owner_contacts(broker_id: int, since_date: str, type_id: str) -> dict:
    """Контакты-собственники за период.
    Returns: {"total": N, "contacts": [...]}"""

# ── Оценка ─────────────────────────────────────────────
def score_broker(violations: dict, owners: dict, kpi_target: int) -> dict:
    """Рассчитать оценку: green/yellow/red + рекомендации."""

# ── Форматирование ─────────────────────────────────────
def format_scorecard(broker: dict, violations: dict, owners: dict,
                     score: dict, since_date: str, now: datetime) -> str:
    """Сформировать текстовый отчёт."""

# ── Главная ────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("Usage: python src/broker_score.py <BROKER_ID>")
        sys.exit(1)
    
    broker_id = int(sys.argv[1])
    settings = get_settings()
    now = datetime.now(timezone.utc)
    since_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    
    # 1. Инфо о брокере (из БД)
    broker = get_broker_info(broker_id)
    if not broker:
        print(f"Брокер с ID {broker_id} не найден в БД")
        sys.exit(1)
    
    # 2. Нарушения (из БД)
    violations = get_violations(broker_id, since_date)
    
    # 3. Собственники (из API Bitrix24)
    owners = get_owner_contacts(broker_id, since_date, settings.contact_owner_type_id)
    
    # 4. Оценка
    score = score_broker(violations, owners, settings.owner_kpi_target)
    
    # 5. Отчёт
    report = format_scorecard(broker, violations, owners, score, since_date, now)
    print(report)
    
    # Отправка в чат 22358
    if not settings.dry_run:
        send_chat_message_chunked(settings.report_chat_id, report)
```

---

## Что нужно сделать (4 пункта)

| # | Действие | Файл |
|---|----------|------|
| 1 | Создать `src/broker_score.py` | Новый файл (~180 строк) |
| 2 | Добавить `make broker-score` | [`Makefile`](Makefile:1), [`make.cmd`](make.cmd:1) |
| 3 | Создать промпт | `plans/prompts/step-55-broker-score.md` |

---

## Запуск

```bash
# Конкретный брокер
make broker-score BROKER_ID=40

# С отправкой в чат
DRY_RUN=false python src/broker_score.py 40
```
