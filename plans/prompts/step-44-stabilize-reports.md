# Промпт для Cursor — Стабилизация отчётов + дата + список отделов

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `src/graph.py`, `.env`, `crontab.txt`.

---

## Контекст

После анализа логов (step-43) были внесены изменения в `tools.py` для корректной классификации звонков. Теперь нужно стабилизировать `graph.py` и подготовиться к маршрутизации отчётов по чатам РОПов:

1. **Вернуть ребро `seller_collector → merge`** — `raw_sellers_deals` должен доходить до dispatcher'а.
2. **Временно убрать блок `📞 ЗВОНКИ`** — отдельный отчёт по звонкам.
3. **Временно убрать статистику звонков** — `📞 Входящих/Исходящих`.
4. **Убрать отладочный код** для LEAD #1538.
5. **Установить дату начала контроля** — `REPORT_SINCE=2026-06-02`.
6. **Обновить расписание** — запуск в 10:00 и 17:00 МСК (пн-пт).

---

## Шаг 0 — Получить список отделов из Битрикс24

Выполни Python-скрипт для получения ID и названий всех отделов:

```python
import sys
sys.path.insert(0, 'src')
from config import get_settings
from fast_bitrix24 import Bitrix

bx = Bitrix(get_settings().b24_webhook_url)
depts = bx.get_all('department.get')
print("ID | Название")
print("-" * 60)
for d in depts:
    if isinstance(d, dict):
        print(f"{d.get('ID', '?')} | {d.get('NAME', '?')}")
```

**Выведи результат пользователю** — он сопоставит ID отделов с ID чатов РОПов:
- Кретов → chat_id=17710
- Горяинов → chat_id=17712
- Каратевский → chat_id=17716
- Трофимова → chat_id=17708
- Волкова → chat_id=17714

---

## Задача: 4 изменения в `src/graph.py` + 1 в `.env` + 1 в `crontab.txt`

### Изменение 1 — Вернуть ребро `seller_collector → merge`

Добавить ребро после `buyer_calls_controller → merge`.

```python
# БЫЛО (строки 906-907):
graph.add_edge("missed_calls_controller", "merge")
graph.add_edge("buyer_calls_controller", "merge")

# СТАЛО:
graph.add_edge("missed_calls_controller", "merge")
graph.add_edge("buyer_calls_controller", "merge")
graph.add_edge("seller_collector", "merge")
```

---

### Изменение 2 — Убрать статистику звонков `📞 Входящих/Исходящих`

Удалить блок подсчёта звонков и строку `stats_line` из отчёта по отделу.

```python
# БЫЛО (строки 710-751):
            dept_user_ids: set[int] = set()
            for v in dept_violations:
                uid = _coerce_int(v.get("responsible_id", 0))
                if uid:
                    dept_user_ids.add(uid)

            incoming_total = 0
            outgoing_total = 0
            since_date = (
                settings.report_since if settings.report_since else "2000-01-01"
            )
            for entities in (
                state.get("raw_leads", []),
                state.get("raw_buyers_deals", []),
            ):
                for entity in entities:
                    if _coerce_int(entity.get("assigned_by_id", 0)) in dept_user_ids:
                        for call in entity.get("calls", []):
                            call_date = str(call.get("start_date", ""))[:10]
                            if call_date < since_date:
                                continue
                            if call.get("call_type") == "incoming":
                                incoming_total += 1
                            elif call.get("call_type") == "outgoing":
                                outgoing_total += 1

            stats_line = (
                f"📞 Входящих: {incoming_total} | Исходящих: {outgoing_total}"
            )

            lines = [
                f"b24-ai-auditor v2 — Отдел: {dept_name}",
                f"Дата: {now}",
                (
                    "Сотрудников с нарушениями: "
                    f"{len({v.get('responsible_id', 0) for v in dept_violations})}"
                ),
                f"Всего нарушений: {len(dept_violations)}",
                f"  Лиды: {len(lead_v)}",
                f"  Сделки: {len(deal_v)}",
                stats_line,
                "",
            ]

# СТАЛО:
            lines = [
                f"b24-ai-auditor v2 — Отдел: {dept_name}",
                f"Дата: {now}",
                (
                    "Сотрудников с нарушениями: "
                    f"{len({v.get('responsible_id', 0) for v in dept_violations})}"
                ),
                f"Всего нарушений: {len(dept_violations)}",
                f"  Лиды: {len(lead_v)}",
                f"  Сделки: {len(deal_v)}",
                "",
            ]
```

---

### Изменение 3 — Убрать блок `📞 ЗВОНКИ`

Удалить весь блок с `call_violations` (отдельный отчёт по звонкам).

```python
# БЫЛО (строки 798-830):
            call_violations = [
                v for v in dept_violations if v.get("severity") == "very high"
            ]
            if call_violations:
                call_lines = [
                    f"📞 ЗВОНКИ — Отдел: {dept_name}",
                    f"Нарушений по звонкам: {len(call_violations)}",
                    "",
                ]
                for v in call_violations:
                    call_entity_type = str(v.get("entity_type", "?"))
                    call_entity_id = _coerce_int(v.get("entity_id", 0))
                    call_reason = v.get("reason", "—")
                    call_link = _build_crm_link(call_entity_type, call_entity_id)
                    call_etype = (
                        "Лид" if call_entity_type == "lead" else "Сделка"
                    )
                    call_lines.append(
                        f"🔴🔴 {call_etype} #{call_entity_id} | {call_reason}",
                    )
                    call_lines.append(f"   {call_link}")

                call_report = "\n".join(call_lines)
                call_chunks = send_chat_message_chunked(
                    REPORT_CHAT_ID,
                    call_report,
                )
                total_chunks += call_chunks
                logger.info(
                    "Dispatcher: dept '%s' calls report sent (%d violations)",
                    dept_name,
                    len(call_violations),
                )

# СТАЛО:
            # Блок "📞 ЗВОНКИ" временно отключён
```

---

### Изменение 4 — Убрать DEBUG-код для LEAD #1538

Удалить отладочный блок в `missed_calls_controller`.

```python
# БЫЛО (строки 397-412):
    for lead in leads:
        if _coerce_int(lead.get("lead_id", 0)) == 1538:
            calls_data = lead.get("calls", [])
            missed = [c for c in calls_data if c.get("status") == "missed"]
            logger.info(
                "DEBUG lead 1538: total_calls=%d, missed=%d, subjects=%s",
                len(calls_data),
                len(missed),
                [c.get("subject_preview", "")[:50] for c in missed],
            )
            logger.info(
                "LEAD 1538 FULL: %s",
                json.dumps(lead.get("calls", []), ensure_ascii=False, default=str)[
                    :2000
                ],
            )

# СТАЛО:
    # DEBUG lead 1538 — убран
```

---

### Изменение 5 — Установить дату начала контроля `REPORT_SINCE=2026-06-02`

В файле `.env` заменить текущую дату на 2 июня 2026.

```ini
# БЫЛО (.env, строка 12):
REPORT_SINCE=2026-05-29

# СТАЛО:
REPORT_SINCE=2026-06-02
```

**Как это работает:** фильтры `>=DATE_CREATE` в `get_all_leads_with_timeline`, `get_deals_by_funnel_with_timeline` и `_fetch_user_calls_for_audit` будут брать данные только начиная с 02.06.2026.

---

### Изменение 6 — Обновить расписание в `crontab.txt`

Запуск два раза в день: 10:00 и 17:00 МСК (UTC+3 → 07:00 и 14:00 UTC), только в будни.

```txt
# БЫЛО (crontab.txt, строки 1-6):
# b24-ai-auditor: запуск каждые 2 часа в рабочее время (пн-пт, 9:00-19:00)
# Вариант 1: запуск на хосте (без Docker)
# 0 9-19/2 * * 1-5 cd /opt/b24-ai-auditor && /opt/b24-ai-auditor/venv/bin/python src/main.py >> logs/cron.log 2>&1

# Вариант 2: запуск в Docker-контейнере
0 9-19/2 * * 1-5 cd /opt/b24-ai-auditor && docker run --rm --env-file .env -v $(pwd)/logs:/app/logs b24-ai-auditor:latest >> logs/cron.log 2>&1

# СТАЛО:
# b24-ai-auditor: запуск в 10:00 и 17:00 МСК (07:00 и 14:00 UTC), пн-пт
# Вариант 1: запуск на хосте (без Docker)
# 0 7,14 * * 1-5 cd /opt/b24-ai-auditor && /opt/b24-ai-auditor/venv/bin/python src/main.py >> logs/cron.log 2>&1

# Вариант 2: запуск в Docker-контейнере
0 7,14 * * 1-5 cd /opt/b24-ai-auditor && docker run --rm --env-file .env -v $(pwd)/logs:/app/logs b24-ai-auditor:latest >> logs/cron.log 2>&1
```

---

## Проверка

1. **Шаг 0:** Выполни скрипт получения отделов, покажи результат пользователю.
2. `.\make.cmd lint` — не должно быть ошибок.
3. `.\make.cmd dry-run` — проверить в логах:
   - Отчёт уходит **один раз** (не два).
   - В сводке есть `Сделок продавцов: N` (данные seller_collector доходят).
   - Нет блоков `📞 ЗВОНКИ` и `📞 Входящих/Исходящих`.
   - Нет `DEBUG lead 1538` в логах.
   - В логах фильтрация: `report_since=2026-06-02`.
4. Проверить `crontab.txt` — расписание `0 7,14 * * 1-5`.
