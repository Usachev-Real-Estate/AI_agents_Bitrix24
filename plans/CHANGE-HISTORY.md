# История изменений b24-ai-auditor

Сводка по `plans/` и `plans/prompts/step-*.md`: что уже пробовали, какие решения приняли, чего избегать.  
Актуальные правила аудита: [`violations-rules-reference.md`](violations-rules-reference.md).  
Источники — промпты step-02…62 и планы в корне `plans/`. Даты ориентировочные по mtime/коммитам.

**Как пользоваться:** перед сменой правил звонков/покупателей/отчётов сверься с разделами «Пробовали» и «Не делать снова».

---

## Эпохи продукта

| Эпоха | Суть | Итог |
|---|---|---|
| **v1** | LangGraph ReAct: Auditor→Analyst→Dispatcher + `@tool` (`get_deal_context`, `check_calls`, …) | Медленно, дорого по LLM; инструменты удалены в cleanup 2026-07 |
| **v2 collectors** | 3 Python-сборщика + 4 LLM-аналитика + dispatcher | Стабильный каркас |
| **Детерминизация** | Calls + lead_rule_1 + buyer stages → Python; LLM только spam/нецелевой | Экономия ~70–80% LLM ([`fix-and-optimize-plan.md`](fix-and-optimize-plan.md)) |
| **Сателлиты** | weekly / tasks / owners / score / chat_poller / exclusive | Отдельные cron entry points |
| **Cleanup 2026-07** | Junk scripts, orphan report, dead v1 tools; exclusive закоммичен отдельно | Правила аудита не трогали |

---

## Хронология по step-промптам

### Bootstrap (step-02…16) — скелет и v1

| Step | Что сделали |
|---|---|
| 02–06 | cursor rules, gitignore/env, requirements, README/Makefile |
| 07–08 | `test_b24`, полные v1 tools через fast_bitrix24 |
| 09–11 | prompts + graph с LLM (RouterAI→DeepSeek), улучшение агент-промптов, `get_active_deals` |
| 12–13 | make.cmd/test-b24, модели v4-flash, Docker без `.env` в образе |
| 14 | `get_all()` вместо `call()` для list-методов (пагинация) |
| 15–16 | live run, разовые `send_status.py` / `send_report.py` (позже удалены как junk) |

### v2 (step-17…25) — параллельный пайплайн и отчёты

| Step | Что сделали |
|---|---|
| 17 | T1/T2 + 3 коллектора, `AuditState`, без LLM |
| 17b | Parallel timeline через `ThreadPoolExecutor` (~10x) |
| 18 | 4 LLM-аналитика + fan-out |
| 18b | Chunking по 100 (иначе JSONDecodeError на ~670 лидах) |
| 19 | Dispatcher → user #154 |
| 19b | **Merge-узел** перед dispatcher (иначе seller ветка опережала и отчёт уходил рано) |
| 19c–f | CRM-ссылки, split/chunk отчётов, имена/отделы, anti double-dispatch (`report_sent`) |
| 20 | Очистка v1-промптов, UTF-8 в коллекторах |
| 21 | Чат **22358** + `send_chat_message` |
| 22–25 | Исключения CONVERTED/Агент, статусы по-русски, группировка по отделам, ФИО |

### Звонки (step-26…43) — долгая отладка, важные выводы

| Step | Пробовали | Урок |
|---|---|---|
| 26 | Искать звонки в `calls[]`, не в timeline | Верно |
| 27–28 | `user.get` **без** `FILTER`; fallback отделов | `FILTER` на `user.get` ломает UF_DEPARTMENT |
| 29–30 | Полные reason; убрать узкий `CRM_ENTITY_ID` для сделок | Слишком узкий фильтр → 0 call data |
| 31 | Переход на `crm.activity.list` (voximplant через webhook часто пуст) | Hybrid: voximplant → fallback activity |
| 32–33 | Dispatcher ждал всех веток; AUTHOR_ID; dedup | Timing/дубликаты — классика fan-in |
| 34 | **Не** считать `COMPLETED!=Y` как missed для исходящих | Главный баг классификации |
| 35–36 | entity_id=0 filter, порог комментария >5 | Не фильтровать в аналитиках агрессивно |
| 37–38 | Только missed без callback; проверка **последнего** missed | Текущая семантика `check_missed_callback_violations` |
| 39 | Missed по SUBJECT | **Не сработало** — SUBJECT одинаковый для всех входящих |
| 40–42 | Debug лида #1538, поиск поля missed | SUBJECT-детекция отвергнута |
| 43 | Три подхода к API подряд | Оставили устойчивый hybrid + правила Python |

### Стабилизация и роутинг (step-44…49)

| Step | Что сделали |
|---|---|
| 44 | Стабилизация отчётов, список отделов, дата |
| 45 | Маршрутизация в чаты РОПов (`DEPT_CHAT_MAP`) |
| 46–47 | Git/push, README + DEPLOY.md |
| 48 | Post-review: RotatingFileHandler, Makefile targets |
| 49 | `dept_id_map` в `save_violations` / `upsert_brokers` |

### Сателлиты (планы + step-52…54)

| План / step | Модуль | Заметки |
|---|---|---|
| [`weekly-broker-report-plan.md`](weekly-broker-report-plan.md) | `db.py`, `weekly_report.py` | SQLite violations; только `is_routine=1` |
| [`task-auditor-plan.md`](task-auditor-plan.md) | `task_auditor.py` | Без LLM; бэк-офис + РОП |
| [`owner-tracker-plan.md`](owner-tracker-plan.md) | `owner_tracker.py` | KPI собственников |
| [`broker-score-plan.md`](broker-score-plan.md) | `broker_score.py` | По запросу / CLI |
| [`chat-command-plan.md`](chat-command-plan.md) | `chat_poller.py` | Cron каждую минуту; `data/chat_last_id.txt` |
| 52–54 | Owner tracker | Эмодзи в IM; все брокеры включая 0; exclude tech users / fallback имён |

### Правила сделок покупателей (step-56…62)

| Step | Изменение | Статус |
|---|---|---|
| 56 | Вместо UF — давность комментария; `_days_since_last_any_comment` | Сделано, затем уточнено |
| 57–58 | Reason без «999 дн.» при отсутствии комментариев | Сделано |
| 59 / 59-60 | Комментарии **только брокер или РОП** (`_days_since_last_comment_by_authors`) | **Текущая политика** |
| 61 | Баг `_is_lead_spam_status`, улучшения отчётов, `RULES_ADVICE` → config | Частично в коде |
| 62 | Упростить «Показ»: только комментарий >10 симв. каждые 7 дней; убрать UF/activities/даты | **Предложено, в коде на 2026-07-23 НЕ применено** (cleanup сознательно сохранил старую логику дел/даты показа). Промпт: `prompts/step-62-simplify-pokaz-rule.md` |

### Прочее из планов

| Документ | Содержание |
|---|---|
| [`architecture.md`](architecture.md) / [`architecture-v2.md`](architecture-v2.md) | v1 vs v2 |
| [`mcp-integration-plan.md`](mcp-integration-plan.md), [`deploy-mcp-changes.md`](deploy-mcp-changes.md) | MCP = **документация**, не прокси API; `DIALOG_ID=chat{id}`; удалён `send_management_report` |
| [`fix-and-optimize-plan.md`](fix-and-optimize-plan.md) | Детерминизация LLM; вынос chat/map в env |

### Недавние коммиты (вне step-файлов)

| Коммит / тема | Суть |
|---|---|
| Commission 2026-08-15 | `buyer_commission_reminder`: только напоминания, смена ответственного на пул выключена (`BUYER_COMMISSION_ENFORCE_ENABLED=false`). 14.08 19:00 вернули 14092/15420/15464/15948 прежним брокерам. |
| Funnel tweak 2026-08-14c | Воронка «Общая база»: `general_base_no_plan` — 2 дня с даты переноса на живое дело или комментарий с планом (не «связаться»). Автор комментария любой (пул часто user 1). Только отчёт, без повторного переноса. Сделки, перенесённые 14.08, флажатся с 16.08. |
| Funnel tweak 2026-08-14b | Текст переноса в Общую базу: предупреждение «в случае невыполнения… будет перенесена», а не «Сделка перенесена». |
| Funnel tweak 2026-08-14 | «Проиграна»: любой комментарий брокера/РОПа или дело снимает; нарушение только если нет ни комментария, ни дела (окно 24ч снято). Офер/Агент по-прежнему с lookback. |
| Funnel tweak 2026-08-13f | Назначение встречи: комментарий брокера **или РОПа**, либо непросроченное дело (просроченное дело не снимает). Напоминание за 2ч — с тем же условием. |
| Funnel tweak 2026-08-13e | ID Афины на сделках: `UF_CRM_1780911079` (поле лида `UF_CRM_1780911032` на сделках не существует — ложные «не заполнено»). |
| Funnel tweak 2026-08-13d | «Проиграна»/Офер/Агент: комментарий за 24ч до входа на стадию считается (брокер пишет причину и сразу меняет стадию). |
| Funnel move 2026-08-13c | Перенос: не трогаем Проиграна / Офер / ID Афины / Поиск клиента (только чат РОПа). `GENERAL_BASE_MOVE_AFTER` — старт переносов с 19:00 13.08. |
| Funnel move 2026-08-13 | Включён автоперенос: лиды NEW>24ч → «Общие лиды»; сделки Продавцы → Общая база / «Продавцы» (`C26:NEW`); Покупатели → Общая база / «Покупатели» (`C26:PREPARATION`). Поиск клиента не переносится. Ответственного не снимаем. |
| Funnel tweak 2026-08-13b | Quality-аудит только админу в личку. Показы: только живое дело + 14 дн. Продавцы: NEW = комментарий брокера за 24ч + напоминание за 2ч; comment-only на Переговорах/Сайте/Поиске; ID Афины на Сайте и Поиске; Поиск клиента не в Общую базу. |
| Buyers funnel 2026-08 | «Показ»→«Первый показ»; `UC_DVW1P9`→«Повторный показ» с тем же `buyer_stage_3`; Переговоры/Дожим/Офер сняты с аудита (`buyer_stage_4` без стадий) |
| Lead quality Spam/Нецелевой | Отдельный job `lead_quality_audit`: `lead_leaked` / `lead_wrong_qualification`; источники timeline + BitrixGPT COMMENTS; без упора на исходящий; дедуп SQLite; cron пн–пт 11:00 МСК → 22358. План: `lead-quality-spam-plan.md` |
| `72dc57c` | Routine gate: MAX_LEADS 500→10000, minute skew; `broker_score` → `settings.rules_advice` (чинился ImportError poller) |
| `f9e1a97` | Удалены junk `fix_*` / корневые test / migrate / `send_full_report_general` |
| `85cd0ee` | Удалены dead v1 `@tool` и неиспользуемые comment-хелперы |
| `848efae` | `exclusive_expiry` (milestones 7/3/1) + config/db/cron |
| `6e31f84` | README sync; убран unused `prev_violators_count` |

---

## Пробовали и отвергли / не повторять

1. **Missed = `COMPLETED != "Y"`** для всех звонков — ломает исходящие.
2. **Missed по SUBJECT** (`"Пропущенный"` и т.п.) — Bitrix часто не отличает.
3. **`user.get` + `FILTER`** — не работает для UF_DEPARTMENT.
4. **Только `voximplant.statistic.get`** через входящий webhook — часто пусто без прав; нужен fallback `crm.activity.list`.
5. **LLM для missed calls / buyer stages** — заменено Python (дорого и нестабильно).
6. **Отчёт без merge-узла** — dispatcher уходит с неполными violations.
7. **MCP как runtime API** — это docs-сервер для разработки.
8. **Считать любой комментарий на сделке** — заменено на broker+ROP only (step-59). Исключение 2026-08-14c: в «Общей базе» автор любой, но текст должен быть планом действий (не «связаться»).
9. **Показывать «(999 дн.)»** пользователю — внутренний маркер.
10. **Seller audit: только платные источники + исходящий как замена комментарию** — отвергнуто регламентом 2026-08; касание = comment/дело в таймлайне.

---

## Текущее состояние (якорь)

- **Пайплайн:** collectors → analysts (lead/buyer/seller/calls deterministic) → merge → dispatcher → ROP chats + summary chat.
- **LLM в main audit:** нет (lead_rule_2/3 → Python `check_lead_rule2_rule3_violations`, 2026-08-07). LLM остаётся в `lead_quality_audit`.
- **Buyer «Первый/Повторный показ» (rule 3):** дело + дата показа + горизонт 14 дней от входа на этап (`crm.stagehistory.list`, fallback DATE_CREATE) на `UC_UFPFKK` и `UC_DVW1P9`.
- **Buyer extras:** `buyer_podbor_stale`, `buyer_ofer_comment`, `buyer_lost_no_reason` (комментарий брокера/РОПа любой давности или дело) / `buyer_agent_no_comment`. Задаток/Сделка без аудита. В «Общую базу» не идут Офер и Проиграна.
- **Leads:** `lead_rule_1` по-прежнему 2 ч без комментария; NEW > 24 ч → `move_lead_to_shared_pool` (`lead_new_over_24h`), ответственного не снимаем.
- **Sellers:** каденс comment-only на Переговорах/Сайте/Поиске; NEW = комментарий брокера/РОПа **или** непросроченное дело за 24ч + напоминание за 2ч. В «Общая база» / «Продавцы» уходят каденс/14дн/Отложенная. Не переносим: Поиск клиента, Проиграна, нет ID Афины.
- **Общая база (cat 26):** `general_base_no_plan` — 2 дня с даты переноса на живое дело или комментарий с планом дальнейших действий. Не автоперенос. Комментарий: фильтр по содержанию, не по автору (исключение из broker+ROP only — карточка часто на пуле).
- **Persistence:** `data/violations.db`; weekly опирается на `is_routine=1`.
- **Cron:** main 2×/день; tasks/weekly/owners пт; poller каждую минуту; exclusive ежедневно 07:00 UTC.
- **Не трогать без явного запроса:** пороги `check_*_violations`, graph edges, prompts, DRY_RUN semantics.

---

## Индекс исходников

- Оставшиеся непросмотренные/неприменённые промпты: [`plans/prompts/`](prompts/) (сейчас только `step-62-simplify-pokaz-rule.md`).
- Применённые step-02…61 **удалены** (2026-07-23); их смысл сохранён в этой истории.
- Справочник правил: [`violations-rules-reference.md`](violations-rules-reference.md)
- Планы фич: architecture*, fix-and-optimize, weekly/task/owner/broker/chat, mcp*

При новой крупной политике (например применение step-62) — дописать сюда дату, решение и коммит; сам промпт после внедрения можно удалить.
