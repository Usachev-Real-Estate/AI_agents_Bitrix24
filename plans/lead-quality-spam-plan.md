# Контроль качества лидов «Спам» / «Нецелевой»

Постоянный контроль (отдельный job, не основной `main.py` аудит).

## Правила

| Rule | Суть |
|---|---|
| `lead_leaked` | Живой клиент (входящий / текст в timeline или BitrixGPT), закрыт в Спам/Нецелевой без нормальной отработки |
| `lead_wrong_qualification` | В BitrixGPT/timeline есть бюджет/район/срок/намерение, статус Спам/Нецелевой неадекватен |

Не путать с `lead_rule_2` / `lead_rule_3` (только «есть ли комментарий-обоснование»).

## Источники данных

1. Таймлайн комментариев лида  
2. Поле `COMMENTS` (часто BitrixGPT)  
3. Входящий звонок (activity) — вспомогательный сигнал  

**Не** требуем исходящий звонок: на портале типичен путь «входящий → сразу квалификация».

**Исключения (не нарушения):**
- брокер написал «Спам» / «это спам» (короткая пометка в timeline/COMMENTS);
- агент пробивала / пробивка агента / звонок агента / риелтор пробивал (любая похожая формулировка);
- звонок сам себе / личный / тестовый; повышение рейтинга / накрутка; прозвон линии;
- по телефону лида уже есть сделка в любой воронке (`crm.duplicate.findbycomm` → контакт/компания → `crm.deal.list`).

## Запуск

```bash
.venv/bin/python src/lead_quality_audit.py
DRY_RUN=true .venv/bin/python src/lead_quality_audit.py
make lead-quality
```

Env: `LEAD_QUALITY_ENABLED`, `LEAD_QUALITY_SINCE` (default `2026-07-01`), `LEAD_QUALITY_CHUNK_SIZE`.

Cron: пн–пт 11:00 МСК → отчёт **только администратору** в личный чат (`CONTACT_SOURCE_LOCK_NOTIFY_USER_ID` / `ADMIN_USER_ID`). В чаты РОПов и `REPORT_CHAT_ID` не уходит.  
Дедуп: SQLite `lead_quality_findings` UNIQUE(lead_id, rule).

## Файлы

- `src/lead_quality_audit.py`
- `src/prompts.py` — `LEAD_QUALITY_ANALYST_PROMPT`
- `src/db.py` — `lead_quality_findings`
- `tests/test_lead_quality_audit.py`
