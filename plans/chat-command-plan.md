# План: Чат-команда для скоринга брокера

## Концепция

Руководитель пишет в чат 22358 сообщение с командой — бот отвечает скорингом брокера.

```
Руководитель: проверь 40

Бот:        b24-ai-auditor — Скоринг брокера
            👤 Иванов Иван (Отдел продаж)
            📋 Нарушения CRM: 4 ⚠️
            🏠 Собственники: 12 ✅ (120% KPI)
            📊 ОЦЕНКА: 🟡 СРЕДНЕ
```

## Как работает

```
Cron (каждую минуту)
  │
  ▼
src/chat_poller.py
  │
  ├─ 1. Читает новые сообщения из чата 22358 (im.dialog.messages.get)
  ├─ 2. Ищет команду: "проверь <ID>" 
  ├─ 3. Запускает скоринг брокера
  ├─ 4. Отправляет результат в чат (im.message.add)
  └─ 5. Запоминает ID последнего обработанного сообщения (data/chat_last_id.txt)
```

**Важно:** скрипт обрабатывает только НОВЫЕ сообщения (с ID > последнего обработанного). Это исключает повторную обработку.

---

## API

### Чтение сообщений из чата

```python
# im.dialog.messages.get — последние 10 сообщений из чата
bx.call("im.dialog.messages.get", {
    "DIALOG_ID": f"chat{chat_id}",
    "LIMIT": 10,
})
```

Ответ содержит массив сообщений, каждое с полями: `id`, `chat_id`, `author_id`, `message`, `date_create`.

### Отправка сообщения в чат

Уже есть в [`notify.py`](src/notify.py):
```python
send_chat_message(chat_id, report)
```

---

## Формат команды

Регулярное выражение:
```python
import re
pattern = re.compile(r"(?:провер(?:ь|ка)|!score|/score)\s+(\d+)", re.IGNORECASE)
```

Поддерживаемые форматы:
- `проверь 40`
- `Проверка 40`
- `!score 40`
- `/score 40`

---

## Хранение последнего обработанного ID

Файл: `data/chat_last_id.txt` — одна строка с числом (ID сообщения).

```python
LAST_ID_FILE = Path("data/chat_last_id.txt")

def get_last_processed_id() -> int:
    try:
        return int(LAST_ID_FILE.read_text().strip())
    except Exception:
        return 0

def set_last_processed_id(msg_id: int) -> None:
    LAST_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_ID_FILE.write_text(str(msg_id))
```

---

## Структура `src/chat_poller.py`

```python
"""Chat command poller: reads chat 22358 for broker score requests."""

import re
from pathlib import Path

# ── Константы ──────────────────────────────────────────
COMMAND_PATTERN = re.compile(
    r"(?:провер(?:ь|ка)|!score|/score)\s+(\d+)",
    re.IGNORECASE,
)
LAST_ID_FILE = Path("data/chat_last_id.txt")

# ── Хелперы ────────────────────────────────────────────
def get_last_processed_id() -> int: ...
def set_last_processed_id(msg_id: int) -> None: ...

# ── Чат ────────────────────────────────────────────────
async def fetch_new_messages(bx, chat_id: int, since_id: int) -> list[dict]:
    """Получить новые сообщения из чата."""

def find_command(messages: list[dict]) -> list[tuple[int, int]]:
    """Найти команды в сообщениях.
    Returns: [(broker_id, message_id), ...]"""

# ── Скоринг (импорт из broker_score.py) ─────────────────
from broker_score import (
    get_broker_info,
    get_violations,
    get_owner_contacts,
    score_broker,
    format_scorecard,
)

# ── Главная ────────────────────────────────────────────
async def async_main():
    settings = get_settings()
    bx = Bitrix(settings.b24_webhook_url)
    
    chat_id = settings.report_chat_id  # 22358
    last_id = get_last_processed_id()
    
    messages = await fetch_new_messages(bx, chat_id, last_id)
    if not messages:
        return
    
    commands = find_command(messages)
    for broker_id, msg_id in commands:
        now = datetime.now(timezone.utc)
        since_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        
        broker = get_broker_info(broker_id)
        if not broker:
            send_chat_message(chat_id, f"❌ Брокер с ID {broker_id} не найден")
            continue
        
        violations = get_violations(broker_id, since_date)
        owners = get_owner_contacts(broker_id, since_date, settings.contact_owner_type_id)
        score = score_broker(violations, owners, settings.owner_kpi_target)
        report = format_scorecard(broker, violations, owners, score, since_date, now)
        
        send_chat_message(chat_id, report)
    
    # Обновить last_id до максимального обработанного
    if messages:
        max_id = max(int(m["id"]) for m in messages)
        set_last_processed_id(max_id)
```

---

## Cron

Каждую минуту (для быстрого отклика):

```bash
* * * * * cd /opt/b24-ai-auditor && venv/bin/python src/chat_poller.py >> logs/cron.log 2>&1
```

**Оптимизация:** можно раз в 2 минуты (`*/2 * * * *`), если задержка в минуту не критична.

---

## Что нужно сделать (5 пунктов)

| # | Действие | Файл |
|---|----------|------|
| 1 | Создать `src/broker_score.py` — логика скоринга (функции) | Новый файл (~120 строк) |
| 2 | Создать `src/chat_poller.py` — чтение чата + вызов скоринга | Новый файл (~100 строк) |
| 3 | Добавить `data/chat_last_id.txt` в `.gitignore` | `.gitignore` |
| 4 | Добавить `make chat-poll` | [`Makefile`](Makefile:1), [`make.cmd`](make.cmd:1) |
| 5 | Добавить cron-строку | [`crontab.txt`](crontab.txt) |

---

## Демо: как это выглядит для руководителя

```
[Чат 22358]

Руководитель:           проверь 40
(через 30-60 секунд)
b24-ai-auditor:         b24-ai-auditor — Скоринг брокера
                        Дата: 15.06.2026
                        
                        👤 Иванов Иван (Отдел продаж)
                        ═══════════════════════════════
                        📋 НАРУШЕНИЯ CRM (за 30 дней)
                          Лиды: 2, Сделки: 1, Звонки: 1
                          ⚠️ Рекомендация: обратить внимание...
                        
                        🏠 НОВЫЕ СОБСТВЕННИКИ (за 30 дней)
                          Добавлено: 12
                          KPI (10/мес): ✅ выполнено (120%)
                        
                        📊 ОЦЕНКА: 🟡 СРЕДНЕ
```
