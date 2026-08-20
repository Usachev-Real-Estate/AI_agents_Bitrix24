# Техническое задание: b24-ai-auditor (по реализации в коде)

Документ составлен **по факту кода**. Это не MCP-продукт: рантайм — пакетный CLI-аудитор Bitrix24. MCP в репозитории — только клиентская заготовка к **внешнему** документационному серверу Bitrix.

**Имя пакета:** `b24-ai-auditor`  
**Версия:** `0.1.0` (`pyproject.toml`, `src/__init__.py`)  
**Описание в манифесте:** «AI-аудитор для Битрикс24 на LangGraph + DeepSeek API»  
**Авторы в манифесте:** `{name = "Your Name"}` (плейсхолдер)  
**Репозиторий в README/DEPLOY:** `https://github.com/Usachevofishial-git/AI_agents_Bitrix24.git`

---

## 1. Назначение и структура проекта

Система **не** является HTTP API, веб-UI и **не** хостит MCP-сервер. Это **пакетное Python-приложение**: по расписанию (cron + Docker) читает CRM портала через **входящий вебхук** REST, проверяет регламент работы брокеров (лиды, сделки покупателей/продавцов, «Общая база», звонки, задачи, KPI, рейтинг), пишет нарушения в SQLite и отправляет сообщения в чаты Bitrix24. Часть джобов **меняет** данные на портале (перенос стадий, откат полей, заполнение UF), если `DRY_RUN=false`.

LLM (DeepSeek через OpenAI-совместимый API) в основном аудите **не вызывается**. Вызов LLM есть только в отдельном джобе качества лидов `lead_quality_audit.py`.

### Дерево верхнего уровня

| Путь | Назначение |
|---|---|
| `src/` | Рабочий код: аудит, джобы, конфиг, БД |
| `tests/` | pytest |
| `scripts/` | Разовые/служебные CLI (в Docker-образ **не** копируются) |
| `plans/` | Планы разработки, история решений, PDF-регламенты воронок; **не исполняется** |
| `data/` | SQLite, CSV/Excel, файлы состояния (`chat_last_id.txt`); в `.gitignore` |
| `logs/` | `audit.log`, `cron.log`; в `.gitignore` |
| `.env` / `.env.example` | Секреты и параметры; `.env` в `.gitignore` |
| `Dockerfile`, `docker-compose.yml`, `crontab.txt` | Деплой |
| `Makefile`, `make.cmd` | Локальные команды Linux/Windows |
| `requirements.txt`, `requirements-dev.txt`, `pyproject.toml` | Зависимости |
| `README.md`, `DEPLOY.md` | Документация запуска |
| `mcp.json.example` | Пример подключения **чужого** MCP документации в Cursor |
| `.pre-commit-config.yaml`, `.flake8`, `.gitignore` | Линт/git |
| `.cursor/` | Правила/скиллы IDE (не рантайм) |
| `.venv/` | Локальное окружение |

### Точки входа (рантайм)

| Точка входа | Как запускается | Роль |
|---|---|---|
| `src/main.py` | `python src/main.py`; Docker `CMD`; cron 10:00/17:00 МСК пн–пт | Главный аудит LangGraph v2 |
| `src/task_auditor.py` | cron пт 17:00 МСК | Просроченные задачи |
| `src/weekly_report.py` | cron пт 18:00 МСК | Недельный отчёт брокеров |
| `src/owner_tracker.py` | cron пт 18:00 МСК | KPI контактов-собственников |
| `src/chat_poller.py` | cron каждую минуту | Команды в чате |
| `src/exclusive_expiry.py` | cron ежедневно 10:00 МСК | Напоминания по эксклюзивам |
| `src/contact_source_lock.py` | cron каждые 5 мин | Откат SOURCE контактов |
| `src/deal_source_lock.py` | cron каждые 5 мин | Откат SOURCE сделок продавцов |
| `src/buyer_base_rate_lock.py` | cron каждые 5 мин | Базовая ставка покупателей |
| `src/buyer_commission_reminder.py` | cron пн–пт ежечасно 9–19 МСК | Напоминания по комиссии |
| `src/broker_rating_collectors.py --daily` | cron пн–пт 19:00 МСК | Сбор дневных метрик рейтинга |
| `src/broker_rating_report.py` | cron пн–пт 17:30 МСК | Публикация рейтинга |
| `src/lead_quality_audit.py` | cron пн–пт 11:00 МСК | LLM-контроль Спам/Нецелевой |
| `src/broker_score.py` | вручную `make broker-score BROKER_ID=…`; также из `chat_poller` | Скоринг одного брокера |
| `src/fill_buyer_base_rate.py` | через `scripts/fill_buyer_base_rate.py` | Разовое заполнение ставки |
| `src/kc_owner_import.py` | через `scripts/import_kc_owners.py` | Импорт лидов КЦ |

HTTP-серверов, SSE-эндпоинтов, systemd unit-файлов, GitHub Actions **в репозитории нет**.

### Язык и пакеты

- **Python:** `>=3.11`; Docker-образ `python:3.11-slim`
- **Менеджер пакетов:** pip + `requirements.txt`; editable-install `pip install -e .[dev]` в Makefile
- **Оркестрация:** LangGraph (`langgraph>=0.2.0`)
- **LLM-клиент:** `langchain-openai>=0.2.0` (`ChatOpenAI`)
- **CRM:** `fast-bitrix24>=1.0.0`
- **Конфиг:** `pydantic-settings>=2.0.0`, `python-dotenv>=1.0.0`

Источник: `README.md`, `pyproject.toml`, `requirements.txt`, `Dockerfile`, `crontab.txt`, `src/main.py`, `src/graph.py`.

---

## 2. Состав MCP-серверов

### 2.1. MCP-серверы, реализованные этим проектом

**Не обнаружено.** В `src/`, `scripts/`, зависимостях нет FastMCP, `mcp` SDK, обработчиков `tools/list`, `initialize`, stdio/SSE-сервера. Рантайм-прокси API через MCP **сознательно отвергнут** (`plans/CHANGE-HISTORY.md`: «MCP как runtime API»).

План `plans/mcp-integration-plan.md` предлагал MCP-клиент в LangGraph (`get_api_method_info`) — **не реализовано**. Скрипт `test_mcp_server.py`, на который ссылается план, **в репозитории отсутствует**. Артефакты `mcp_tools_list.json` / `mcp_test_output.txt` в `.gitignore`.

### 2.2. Внешний документационный MCP (только разработка)

В репозитории есть **клиентский пример** для Cursor/Claude Desktop:

**Файл:** `mcp.json.example`

```json
{
  "mcpServers": {
    "b24-dev-mcp": {
      "url": "https://mcp-dev.bitrix24.tech/mcp",
      "timeout": 30000
    }
  }
}
```

| Параметр | Значение в коде/плане |
|---|---|
| Имя | `b24-dev-mcp` |
| Назначение | Документация REST Bitrix24 для IDE, **не** вызовы к порталу |
| Файл входа в этом репо | не обнаружено (сервер внешний) |
| Команда запуска из этого репо | не обнаружено |
| SDK проекта | не используется |
| URL | `https://mcp-dev.bitrix24.tech/mcp` |
| Транспорт | HTTP; план фиксирует ответы **SSE** (`text/event-stream`) |
| Порт | не задан (URL хостинга Bitrix) |
| Авторизация на транспорте | в примере нет заголовков/токенов; план: «без авторизации» |
| Регистрация | скопировать в конфиг Cursor MCP; токенов нет |

План (`plans/mcp-integration-plan.md`) после теста (скрипт сейчас отсутствует) фиксирует: сервер `b24-dev-mcp` **v0.2.0**, протокол JSON-RPC 2.0, `protocolVersion: "2024-11-05"`. Версию протокола/сервера **из исполняемого кода репозитория подтвердить нельзя**.

В среде Cursor этот же внешний сервер может быть подключён как `user-b24-dev-mcp` (каталог инструментов IDE). Это конфигурация клиента, не код проекта.

Источник: `mcp.json.example`, `plans/mcp-integration-plan.md`, `plans/deploy-mcp-changes.md`, `.gitignore`.

---

## 3. Инструменты (tools)

### 3.1. MCP-инструменты этого репозитория

**Не обнаружено** (проект не регистрирует MCP tools).

### 3.2. Инструменты внешнего документационного MCP

Ниже — схема внешнего сервера документации (не мутирует портал; REST CRM не вызывает). В Cursor-каталоге дополнительно есть `mcp_auth` (обёртка клиента IDE; в плане проекта его нет).

| Имя | Назначение | Вход | Возврат | REST портала | Побочные эффекты |
|---|---|---|---|---|---|
| `bitrix-search` | Поиск по документации REST | `query` string **обяз.**; `limit` int\|null, default null; `doc_type` array enum `method\|event\|other\|app_development_docs` \| null, default null | Краткий список совпадений | нет | нет |
| `bitrix-method-details` | Карточка метода REST | `method` string **обяз.**; `field` string default `"all"`; `filter` string\|null default null | Текст: параметры, возврат, ошибки, примеры | нет | нет |
| `bitrix-event-details` | Документация события | `title_or_hint` string **обяз.** | Markdown | нет | нет |
| `bitrix-article-details` | Статья не-метод | `title_or_hint` string **обяз.** | Markdown | нет | нет |
| `bitrix-app-development-doc-details` | Документация разработки приложений | `title_or_hint` string **обяз.** | Plain text | нет | нет |

Подтверждения повторного выполнения / allowlist операций **не обнаружено** (это docs-сервер).

### 3.3. LangChain `@tool` в рантайме аудита

Остаток v1: декоратор `@tool` (`langchain_core.tools`). Граф **не** отдаёт их LLM. Коллекторы вызывают `.invoke(...)`.

| Имя | Назначение | Параметры | Возврат | REST | Побочные эффекты |
|---|---|---|---|---|---|
| `get_all_leads_with_timeline` | Все лиды + таймлайн + звонки ответственных | нет | `{"leads": [...], "total": int}` или `{"error", "leads": [], "total": 0}` | `crm.lead.list`, `crm.status.list`, `crm.timeline.comment.list`, `voximplant.statistic.get`, `crm.activity.list` | только чтение |
| `get_deals_by_funnel_with_timeline` | Сделки воронки + таймлайн + активности + история стадий | `category_id`: int, **обязателен**, без default | `{"deals", "category_id", "total"}` или error | `crm.deal.list`, `crm.status.list`, `crm.timeline.comment.list`, `crm.activity.list`, `crm.stagehistory.list`, плюс звонки как у лидов | только чтение |

`get_general_base_deals_with_timeline` — обычная функция **без** `@tool`; вызывается напрямую из `general_base_collector`.

Защита от повторного выполнения у этих tools: **не обнаружена** (идемпотентное чтение).

Источник: `src/tools.py` (декораторы около строк 2805, 2891), `src/graph.py` (`lead_collector` / `buyer_collector` / `seller_collector`).

### 3.4. Операции, изменяющие данные на портале

Это **не MCP-tools**. Подтверждения оператором **нет**. Защита: флаг `DRY_RUN` (по умолчанию `true`). Повторное выполнение ограничено только прикладной логикой SQLite / снапшотами, не протоколом MCP.

| Операция | Где | REST | Что меняет | Защита от повтора |
|---|---|---|---|---|
| Перенос сделки в «Общую базу» | `tools._move_deal_to_general_base` | `crm.item.update` (`entityTypeId=2`, `categoryId`, `stageId`) | воронка/стадия; ответственный **не** меняется | `DRY_RUN`; `GENERAL_BASE_MOVE_AFTER`; «Поиск клиента» не переносится |
| Перенос лида в «Общие лиды» | `tools.move_lead_to_shared_pool` | `crm.lead.update` `STATUS_ID=1` | статус лида | `DRY_RUN`; `GENERAL_BASE_MOVE_AFTER` |
| Сообщения в чат | `notify.send_chat_message*` | `im.message.add` | сообщения | `DRY_RUN` в вызывающих джобах |
| Личные сообщения | `notify.send_user_chat_message*` | `im.message.add` (`DIALOG_ID` = user id, `SYSTEM`) | личка | `DRY_RUN`; опционально прямой HTTP к другому вебхуку |
| Напоминание «Назначение встречи» | `graph.seller_deal_analyst` | `im.message.add` | личка брокеру | `DRY_RUN`; повтор при каждом аудите, если условие ещё истинно — **отдельной идемпотентности нет** |
| Откат SOURCE контакта | `contact_source_lock.revert_contact_source` | `crm.contact.update` `SOURCE_ID` | поле источника | снапшот SQLite; `DRY_RUN` |
| Откат SOURCE сделки | `deal_source_lock.revert_deal_source` | `crm.deal.update` `SOURCE_ID` | поле источника | снапшот; `DRY_RUN` |
| Запись/откат базовой ставки | `buyer_base_rate_lock` / `fill_buyer_base_rate` | `crm.deal.update` `UF_CRM_1785315943350` | UF | снапшот; `DRY_RUN` |
| Смена ответственного на пул (комиссия) | `buyer_commission_reminder.reassign_deal_to_pool` | `crm.deal.update` `ASSIGNED_BY_ID` | ответственный | **выключено** (`BUYER_COMMISSION_ENFORCE_ENABLED=false`); PK `(deal_id, enforce_date)` |
| Эксклюзивы | `exclusive_expiry` | только `im.message.add` (+ чтение `crm.item.list`, `profile`) | сообщения | PK `(item_id, end_date, days_before)` |
| Импорт КЦ | `kc_owner_import` | `crm.contact.add`, `crm.deal.add`, `crm.timeline.comment.add` | создание контакта/сделки/комментария | ручной запуск; `DRY_RUN`; лог `data/kc_import_results.jsonl` |
| Заполнение Циан | `scripts/fill_cian_leads_from_csv.py` | `crm.lead.update`, `crm.timeline.comment.add` | поля лида + комментарий | ручной; `DRY_RUN` |
| Разовый перенос покупателей | `scripts/move_unfixed_buyer_violations.py` | `crm.item.update` | стадия | ручной; `DRY_RUN` |

**Не обнаружено:** `tasks.task.add`, `crm.deal.get` (кроме исторического плана), `im.notify.personal.add` (есть только в docstring `notify.py`), `im.notify.system.add` / `send_management_report` (удалены по плану).

Массового удаления сущностей CRM в коде **не обнаружено**.

Источник: `src/tools.py`, `src/notify.py`, `src/graph.py`, `src/contact_source_lock.py`, `src/deal_source_lock.py`, `src/buyer_base_rate_lock.py`, `src/buyer_commission_reminder.py`, `src/exclusive_expiry.py`, `src/kc_owner_import.py`, `scripts/*.py`.

---

## 4. Ресурсы и промпты MCP

**В этом репозитории не реализованы** примитивы MCP `resources` и `prompts`.

План теста внешнего сервера: Resources = 0, Prompts = 0. Повторно из файлов репо это не проверить (`test_mcp_server.py` отсутствует).

Промпты **LangChain/DeepSeek** (не MCP):

| Константа | Файл | Используется? |
|---|---|---|
| `LEAD_QUALITY_ANALYST_PROMPT` | `src/prompts.py` | да, `lead_quality_audit.py` |
| `LEAD_ANALYST_PROMPT` | `src/prompts.py` | **нет импортов** — заготовка/справочник политики; rule_2/3 считаются в Python |

Источник: `src/prompts.py`, `src/lead_quality_audit.py`, `plans/mcp-integration-plan.md`.

---

## 5. Интеграция с Битрикс24

### Способ подключения

**Только входящий вебхук** (`B24_WEBHOOK_URL`). Клиент: `fast_bitrix24.Bitrix(webhook_url)` и точечные `httpx.post(webhook + method)` для `crm.item.*` / `im.message.add` с альтернативным вебхуком.

OAuth / локальное приложение / исходящие вебхуки **в коде не обнаружены**.

Действия на портале выполняются **от имени владельца вебхука**. Отдельная проверка «кто нажал в UI» есть только у lock-джобов через `MODIFY_BY_ID` карточки.

Права (scope) **в коде не заданы** — определяются при создании вебхука в UI портала. По вызываемым методам фактически нужны как минимум: CRM (лиды, сделки, контакты, активности, таймлайн, стадии, смарт-процесс), пользователи, структура, задачи, IM, телефония (voximplant), лента (`log.blogpost.*`). Точный список scope в репозитории **не зафиксирован**.

### Вызываемые методы REST

| Метод | Где |
|---|---|
| `crm.lead.list` | `tools.py`, `lead_quality_audit.py`, `broker_rating_collectors.py`, `scripts/fill_cian_leads_from_csv.py` |
| `crm.lead.get` | `lead_quality_audit.py` (телефоны) |
| `crm.lead.update` | `tools.py` (пул), `scripts/fill_cian_leads_from_csv.py` |
| `crm.deal.list` | `tools.py`, lock/fill/commission, `lead_quality_audit.py`, скрипты |
| `crm.deal.update` | `deal_source_lock`, `buyer_base_rate_lock`, `fill_buyer_base_rate`, `buyer_commission_reminder` |
| `crm.deal.add` | `kc_owner_import.py` |
| `crm.item.update` | `tools.py` (Общая база), `scripts/move_unfixed_buyer_violations.py` |
| `crm.item.list` | `exclusive_expiry.py` (смарт-процесс) |
| `crm.contact.list` | `contact_source_lock.py`, `owner_tracker.py` |
| `crm.contact.update` | `contact_source_lock.py` |
| `crm.contact.add` | `kc_owner_import.py` |
| `crm.timeline.comment.list` | `tools.py` |
| `crm.timeline.comment.add` | `kc_owner_import.py`, `scripts/fill_cian_leads_from_csv.py` |
| `crm.activity.list` | `tools.py`, `lead_quality_audit.py`, `scripts/send_show_plan_reminders.py` |
| `crm.stagehistory.list` | `tools.py` |
| `crm.status.list` | `tools.py` |
| `crm.duplicate.findbycomm` | `lead_quality_audit.py` (телефон → дубли) |
| `voximplant.statistic.get` | `tools.py` |
| `user.get` | много модулей |
| `user.search` | `graph.py` (fallback) |
| `user.current` | `scripts/test_b24.py` |
| `department.get` | `graph.py`, `task_auditor.py`, `owner_tracker.py`, `broker_rating_collectors.py`, `scripts/list_departments.py`, `scripts/send_show_plan_reminders.py` |
| `tasks.task.list` | `task_auditor.py`, `broker_rating_collectors.py`, `scripts/test_b24.py`, `scripts/back_office_july_tasks_report.py` |
| `tasks.task.get` | `scripts/back_office_july_tasks_report.py` |
| `im.message.add` | `notify.py` |
| `im.dialog.messages.get` | `chat_poller.py`, `scripts/back_office_july_tasks_report.py` |
| `log.blogpost.get` | `broker_rating_collectors.py` |
| `log.blogpost.getusers.important` | `broker_rating_collectors.py` |
| `profile` | `exclusive_expiry.verify_notify_sender` |

Метод `batch` **напрямую не вызывается**. `fast_bitrix24.get_all` внутри себя ходит по страницам (и, по комментарию в `contact_source_lock.py`, может упираться в `OPERATION_TIME_LIMIT` на больших выборках — поэтому lock-джобы пагинируют `bx.call(..., start)` вручную).

### Сущности портала

Лиды, сделки (воронки 0 / 18 / 26), контакты, компании (только фильтр `COMPANY_ID` при поиске сделок по дублю телефона), задачи, пользователи, подразделения, активности/дела, комментарии таймлайна, история стадий, смарт-процесс entityTypeId **1080** («Эксклюзивы»), звонки Voximplant, сообщения IM, посты ленты.

### Пользовательские поля UF_*

| Код | Назначение в коде |
|---|---|
| `UF_CRM_1659375809326` | Дата встречи (аудит сделок, читается) |
| `UF_CRM_1774361998551` | Результат показа (читается) |
| `UF_CRM_1780911079` | ID Афины **на сделках** (`seller_afina_id_missing`) |
| `UF_CRM_1780911032` | ID Афины **на лидах** (скрипт Циан; на сделках не используется) |
| `UF_CRM_1785315943350` | Базовая ставка покупателей (запись/откат) |
| `UF_CRM_1774521607469` | Стоимость объекта при импорте КЦ (`"0\|RUB"`) |
| `UF_CRM_1747291787883` | Тип недвижимости при импорте КЦ (`"356"` = квартира) |
| `UF_CRM_1774364892961` | Название ЖК при импорте КЦ |
| `ufCrm20_1784712129125` | Дата окончания эксклюзива (смарт-процесс; имя настраивается env) |
| `ufCrm20_1784712031409` | Адрес эксклюзива |
| `UF_DEPARTMENT` | Отдел пользователя (не CRM UF карточки) |
| `UF_BLOG_POST_IMPRTNT` | Признак важного поста ленты |

Системные поля: `SOURCE_ID`, `OPPORTUNITY`, `STATUS_ID`, `STAGE_ID`, `ASSIGNED_BY_ID`, `TYPE_ID` контакта (`UC_2G0TD3` = собственник).

### Пагинация и лимиты

- `get_all` (fast_bitrix24) — основной путь списков.
- Ручной `start` / `next`: `contact_source_lock`, `deal_source_lock`, `buyer_base_rate_lock`, `buyer_commission_reminder`, `exclusive_expiry` (`crm.item.list`).
- `crm.duplicate.findbycomm`: пачки по 20 телефонов.
- `crm.stagehistory.list`: чанки по 50 `OWNER_ID`; при ошибке — по одной сделке.
- `im.dialog.messages.get`: `LIMIT: 50`.
- `kc_owner_import`: `time.sleep(pause_sec)` default 0.35 с между созданиями.

Обработки `QUERY_LIMIT_EXCEEDED`, явного rate limiter, экспоненциальных retry **не обнаружено**. Комментарий про `OPERATION_TIME_LIMIT` есть только у контактов (~70k — полный seed снапшотов отключён, lazy mode).

Источник: перечисленные модули `src/` и `scripts/`.

---

## 6. Скрипты и автоматизации

### Штатный cron (`crontab.txt`)

Рабочий каталог в crontab: `/home/Ai_agents_crm`. Запуск: `docker run --rm --env-file .env -v logs -v data`.

| # | Расписание | Команда | Меняет CRM? |
|---|---|---|---|
| 1 | пн–пт 10:00 и 17:00 МСК | `src/main.py` (CMD образа) | да, при `DRY_RUN=false`: переносы, чаты, напоминания встреч |
| 2 | пт 17:00 МСК | `task_auditor.py` | чаты |
| 3 | пт 18:00 МСК | `weekly_report.py` | чаты |
| 4 | пт 18:00 МСК | `owner_tracker.py` | чаты |
| 5 | каждую минуту | `chat_poller.py` | чаты |
| 6 | ежедневно 10:00 МСК | `exclusive_expiry.py` | личные сообщения |
| 7 | каждые 5 мин | `contact_source_lock.py` | `crm.contact.update` |
| 8 | каждые 5 мин | `deal_source_lock.py` | `crm.deal.update` |
| 9 | каждые 5 мин | `buyer_base_rate_lock.py` | `crm.deal.update` UF |
| 10 | пн–пт каждый час 9–19 МСК | `buyer_commission_reminder.py` | чаты; смена ответственного выключена |
| 11 | пн–пт 19:00 МСК | `broker_rating_collectors.py --daily` | нет (SQLite) |
| 12 | пн–пт 17:30 МСК | `broker_rating_report.py` | чат |
| 13 | пн–пт 11:00 МСК | `lead_quality_audit.py` | личка админу |
| 14 | `5 16 13 8 *` | повтор `main.py` | разовая дата **13.08.2026 19:05 МСК** — уже в прошлом относительно 18.08.2026 |

CI-планировщика нет.

### Разовые / служебные (`scripts/`)

| Скрипт | Назначение | Запуск | Мутации |
|---|---|---|---|
| `test_b24.py` | Проверка вебхука | вручную / `make` (цель в README, в Makefile **нет** `test-b24`) | чтение (`user.current`, сделки, задачи, активности) |
| `list_departments.py` | Печать отделов | вручную | нет |
| `run_audit_no_rop_chats.py` | Аудит с `DRY_RUN=true` и пустым `DEPT_CHAT_MAP_JSON`, выгрузка в `data/audit_manual_no_rop.*` | вручную | нет (форсирует dry-run) |
| `fill_buyer_base_rate.py` | CLI обёртка разового заполнения ставки | `scripts/…` | `crm.deal.update` |
| `import_kc_owners.py` | Импорт Excel → контакт+сделка | `make import-kc-owners` | создание CRM |
| `fill_cian_leads_from_csv.py` | Заполнение лидов из CSV Циан | вручную | `crm.lead.update`, комментарий |
| `move_unfixed_buyer_violations.py` | Разовый перенос нарушителей-покупателей; исключает 3 ФИО и правила lost/ofer | вручную; читает `logs/buyer-violations-2026-08-14.log` | `crm.item.update` |
| `send_show_plan_reminders.py` | Разовые напоминания по показам в чаты отделов | вручную | чаты |
| `seller_meeting_plan_review.py` | Разовая выборка продавцов NEW без плана → личка | вручную | чаты |
| `export_sellers_deals_with_timeline_to_csv.py` | Выгрузка CSV | вручную | нет |
| `export_sellers_kc5_comment_activity_to_csv.py` | Выгрузка сделок источника «24» | вручную | нет |
| `back_office_july_tasks_report.py` | Отчёт по закрытым задачам июля 2026 | вручную | читает чаты задач; `requests` не в `requirements.txt` |

Источник: `crontab.txt`, `Makefile`, заголовки `scripts/*.py`.

---

## 7. Обработка событий

**Не обнаружено.** Нет HTTP-приёмника, `event.bind`, `ONCRMDEALUPDATE`, проверки `application_token`, исходящих вебхуков.

Вместо событий — **поллинг**: cron и `im.dialog.messages.get` раз в минуту (`data/chat_last_id.txt`).

Идемпотентность точечная (SQLite PK), не на уровне событий Bitrix.

Источник: отсутствие обработчиков в `src/`; `chat_poller.py`.

---

## 8. Хранение данных

**Не stateless.** SQLite `data/violations.db` (WAL, FK). Путь: `src/db.py` `DB_PATH`.

| Таблица | Назначение |
|---|---|
| `audit_runs` | Прогоны аудита; `is_routine`, `report_since` |
| `violations` | Нарушения; UNIQUE(run, type, id, rule) |
| `brokers` | Справочник брокеров с последнего аудита |
| `opened_leads_log` | **Создаётся, записей/чтений в коде нет** |
| `exclusive_expiry_notifications` | Уже отправленные milestone 7/3/1 |
| `contact_source_snapshots` | SOURCE контакта |
| `deal_source_snapshots` | SOURCE сделки |
| `deal_base_rate_snapshots` | Базовая ставка |
| `buyer_commission_notifications` | Последние напоминания (deal + role) |
| `buyer_commission_enforcements` | Факт переназначения на пул |
| `broker_shared_leads` | Переносы в «Общие лиды» для рейтинга |
| `broker_daily_metrics` | Дневные метрики рейтинга |
| `broker_ratings` | Снапшоты рейтинга |
| `lead_quality_findings` | Дедуп LLM-находок PK (lead_id, rule) |
| `schema_meta` | Миграции |

Файлы состояния: `data/chat_last_id.txt`; CSV мотивации; Excel импорта КЦ; `data/kc_import_results.jsonl`. Кэша Redis/memcached **нет**.

Источник: `src/db.py`, `src/chat_poller.py`, `src/kc_owner_import.py`.

---

## 9. Логирование и аудит

- `config.setup_logging`: консоль + `logs/audit.log`, RotatingFileHandler 10 МБ × 5, уровень из `LOG_LEVEL` (default INFO).
- Формат: `%(asctime)s %(levelname)s [%(name)s] %(message)s`.
- Cron: `>> logs/cron.log`.
- Вызовы MCP **не логируются** (их нет в рантайме).
- Мутации: `logger.info` с ID сущностей, `DRY_RUN: would …`.
- Запись `B24_WEBHOOK_URL` / токенов в лог **не найдена**.
- В лог попадают ФИО, ID пользователей, тексты причин нарушений, ссылки на карточки (домен из вебхука без `/rest/…`).
- Поле `COMMENTS` / таймлайн (в т.ч. содержимое BitrixGPT) уходит в LLM в `lead_quality_audit` — это не файл лога, но передача ПДн во внешний API.

Отдельного журнала «кто вызвал tool» нет.

Источник: `src/config.py`, `src/notify.py`, `src/lead_quality_audit.py`.

---

## 10. Обработка ошибок

| Ситуация | Поведение |
|---|---|
| Нет `.env` / нет `B24_WEBHOOK_URL` или `DEEPSEEK_API_KEY` | `main.py`: `ValidationError` → exit 1 |
| Недоступен портал / ошибка REST в коллекторе | `get_*_with_timeline` ловит Exception, возвращает `error` и пустой список; аудит идёт дальше с 0 сущностей |
| Ошибка таймлайна одной карточки | debug, пустой timeline |
| `voximplant` пуст/ошибка | fallback `crm.activity.list` |
| `user.get` не удался | fallback `user.search`; иначе `"ID:{uid}"` |
| Ошибка отправки отчёта | `logger.exception`, БД всё равно пишется |
| Чанк LLM качества упал | exception в лог, остальные чанки продолжаются; битый JSON → пустой список |
| Превышение лимитов Bitrix | отдельной ветки нет — общее Exception |
| Неверные параметры `@tool` | схема LangChain `category_id` обязателен; отдельного «ошибка для модели» нет, т.к. LLM tools не вызывает |
| `DRY_RUN` | мутации пропускаются с `dry_run_skipped` |

«Осмысленная ошибка модели» для MCP **не применима**. Для LLM качества: при сбое чанка находки этого чанка теряются без ретрая.

Источник: `src/main.py`, `src/tools.py`, `src/graph.py`, `src/lead_quality_audit.py`.

---

## 11. Безопасность

| Тема | Факт |
|---|---|
| Хранение секретов | `.env` (gitignored); Docker `--env-file .env`; в образ `.env` не копируется |
| Имена секретов | `B24_WEBHOOK_URL`, `DEEPSEEK_API_KEY`, опционально `EXCLUSIVE_NOTIFY_WEBHOOK_URL` |
| Allowlist операций | нет универсального; есть списки правил нарушений `ALLOWED_VIOLATION_RULES`; lock только SOURCE / базовая ставка |
| Массовое удаление | не обнаружено |
| Разграничение пользователей системы | одно приложение, один вебхук; нет ролей приложения |
| От чьего имени | владелец входящего вебхука; `exclusive_expiry` сверяет `profile` с `EXCLUSIVE_NOTIFY_FROM_USER_ID` (warning, не блок) |
| Lock SOURCE | брокеры/РОПы по `UF_DEPARTMENT` + `WORK_POSITION`; исключаются `B24_USER_ID`, имена из JSON, бэк-офис |
| Чат-команды | любой, кто пишет в `REPORT_CHAT_ID`; проверки автора **нет** |
| Docker | non-root user `auditor` |

Источник: `src/config.py`, `Dockerfile`, `src/contact_source_lock.py`, `src/chat_poller.py`, `src/exclusive_expiry.py`.

---

## 12. Конфигурация

Обязательны для создания `Settings`: **`B24_WEBHOOK_URL`**, **`DEEPSEEK_API_KEY`**. Остальное имеет default (в т.ч. `DRY_RUN=true`). `DEEPSEEK_API_KEY` обязателен даже джобам без LLM.

| Имя | Назначение |
|---|---|
| `B24_WEBHOOK_URL` | Входящий вебхук |
| `B24_USER_ID` | Исключение из SOURCE-lock (владелец вебхука) |
| `BUYERS_CATEGORY_ID` | Воронка покупателей (пример 18) |
| `SELLERS_CATEGORY_ID` | Воронка продавцов (пример 0) |
| `REPORT_SINCE` | Нижняя граница DATE_CREATE лидов/сделок аудита |
| `DEEPSEEK_API_KEY` | Ключ LLM |
| `DEEPSEEK_BASE_URL` | default `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | default `deepseek-v4-flash` |
| `DEEPSEEK_V3_MODEL` | **нигде кроме Settings не читается** |
| `DRY_RUN` | Запрет мутаций |
| `GENERAL_BASE_MOVE_AFTER` | Пауза автопереноса; пусто = сразу |
| `FORCE_ROUTINE_AUDIT` | Писать violations в БД вне окна cron 10/17 |
| `LOG_LEVEL` | Уровень логов |
| `MANAGEMENT_CHAT_ID` | **только поле Settings, не используется** |
| `ADMIN_USER_ID` | Fallback получателя lock-алертов |
| `REPORT_CHAT_ID` | Сводка и чат-команды (default 22358) |
| `DEPT_CHAT_MAP_JSON` | `{dept_id: chat_id}` |
| `ANALYST_CHUNK_SIZE` | **только Settings, не используется** |
| `BACK_OFFICE_DEPT_ID` / `BACK_OFFICE_CHAT_ID` | Аудит задач |
| `CONTACT_OWNER_TYPE_ID` | Тип контакта-собственника |
| `OWNER_KPI_SINCE` / `OWNER_KPI_TARGET` / `OWNER_EXCLUDE_USER_IDS_JSON` / `OWNER_SALES_DEPT_IDS_JSON` | KPI собственников / рейтинг |
| `RULES_ADVICE_JSON` | Тексты советов в скоринге |
| `TASK_AUDITOR_EXCLUDE_USERS_JSON` | Исключения аудита задач |
| `EXCLUSIVE_*` | Смарт-процесс 1080, поля дат, milestones, skip stages, получатели, отдельный вебхук |
| `CONTACT_SOURCE_LOCK_*` / `DEAL_SOURCE_LOCK_ENABLED` | Lock источника |
| `BUYER_BASE_RATE_LOCK_ENABLED` / `BUYER_BASE_RATE_CSV` | Базовая ставка |
| `BUYER_COMMISSION_*` | Напоминания комиссии / выключенный enforce |
| `BROKER_RATING_*` | Веса, период, исключения рейтинга |
| `LEAD_QUALITY_*` | Контроль Спам/Нецелевой |

Источник: `src/config.py`, `.env.example`.

---

## 13. Развёртывание

**Как в репо:** локальный venv **или** Docker + crontab хоста. systemd/K8s/CI **не описаны**.

Требования: Python 3.11+, Docker (для продакшен-варианта в `DEPLOY.md`), доступ к порталу и DeepSeek, каталоги `logs/`, `data/`.

Порядок (`DEPLOY.md`): установить Docker → clone → `cp .env.example .env` → заполнить ключи → `docker build` → тестовый `DRY_RUN=true` → `crontab crontab.txt`.

Ограничения образа: `Dockerfile` копирует **только** `src/` и `requirements.txt`. Скрипты из `scripts/`, CSV в `data/`, `openpyxl` в образ не входят. `docker-compose.yml` запускает лишь `python src/main.py` один раз (`restart: "no"`), без cron внутри контейнера.

Makefile ставит venv как `venv/`, на сервере в правилах проекта упоминается `.venv/` — оба варианта встречаются.

Источник: `DEPLOY.md`, `Dockerfile`, `docker-compose.yml`, `crontab.txt`, `Makefile`.

---

## 14. Тесты

- **223** теста (`pytest --collect-only`), 19 файлов `tests/test_*.py`.
- Запуск: `make test` / `pytest`. `norecursedirs = ["scripts"]`.
- Покрытие: детерминированные правила лидов/покупателей/продавцов/общей базы/звонков, lock SOURCE/ставки, комиссия, эксклюзивы, рейтинг, quality-аудит (парсинг/фильтры), config, routine gate, импорт КЦ.
- **Моки Bitrix:** `unittest.mock.MagicMock` / `patch` на функциях revert/update; живого портала тесты не требуют.
- **Нет тестов:** граф целиком, notify, chat_poller, MCP, `scripts/`.
- LLM в тестах quality не вызывается к сети (проверяются хелперы/формат отчёта).

Источник: `tests/`, `pyproject.toml`, `tests/conftest.py`.

---

## 15. Внешние зависимости

| Сервис / библиотека | Зачем | Если недоступен |
|---|---|---|
| Портал Bitrix24 REST (входящий вебхук) | Все данные и мутации | Коллекторы пустые / джоб падает; отчёты пустые или exception |
| DeepSeek API (`DEEPSEEK_BASE_URL`) | Только `lead_quality_audit` | Чанки quality пропускаются; основной аудит работает |
| `fast_bitrix24` | REST-клиент | Приложение не стартует |
| `langgraph` | Оркестрация аудита | `main.py` не работает |
| `langchain-openai` | ChatOpenAI | quality-аудит не работает |
| `httpx` | `exclusive_expiry`, опциональный webhook в `notify` | **не в `requirements.txt`**; вероятно транзитивно от langchain-openai |
| `openpyxl` | импорт КЦ | только в extra `dev` `pyproject.toml`; **нет в `requirements-dev.txt`** |
| `requests` | один разовый скрипт бэк-офиса | не в requirements |
| Внешний MCP docs | только IDE | на рантайм не влияет |

Источник: `requirements.txt`, `pyproject.toml`, импорты `src/exclusive_expiry.py`, `src/kc_owner_import.py`, `scripts/back_office_july_tasks_report.py`.

---

## 16. Интерфейс пользователя

**Веб-UI нет.**

| Интерфейс | Что это |
|---|---|
| CLI | `python src/*.py`, `make`, `make.cmd` |
| Docker | one-shot контейнер |
| Чаты Bitrix24 | сводки, отделы, личка админа; команды в `REPORT_CHAT_ID`: `проверь/!score N`, `/weekly`, `/dept …`, `/rating [отдел]` |
| Cursor MCP | опционально документация API при разработке; **не** управление аудитором |

Управление продом — правка `.env` и crontab, не MCP-клиент.

Источник: `src/chat_poller.py`, `README.md`, `mcp.json.example`.

---

## 17. Известные ограничения

Явных `TODO`/`FIXME` в `src/` **не найдено**.

Заготовки и мёртвое:

| Что | Статус |
|---|---|
| MCP runtime / `get_api_method_info` / `test_mcp_server.py` | не реализовано / файл отсутствует |
| `LEAD_ANALYST_PROMPT` | не используется |
| `MANAGEMENT_CHAT_ID`, `ANALYST_CHUNK_SIZE`, `DEEPSEEK_V3_MODEL` | только конфиг |
| `opened_leads_log` | таблица без кода |
| `@tool` на коллекторах | не LLM-tools; наследие v1 |
| `im.notify.personal.add` | docstring без вызова |
| `buyer_stage_1` / `buyer_stage_4` | код есть, в `BUYERS_STAGE_AUDIT_RULE` стадий с номерами 1 и 4 **нет** |
| `SELLERS_PAID_SOURCE_IDS` | «legacy labels only» |
| README: LLM в аналитиках главного графа | **устарело** относительно кода |
| `DEPLOY.md` чаты РОПов 17708… vs `.env.example` все → 22358 | расхождение документов |
| crontab #14 на 13.08.2026 | одноразовая дата в прошлом |
| Hardcode портала | комментарии `b24-po7frr`; `print` чеклиста UI в `contact_source_lock.py` (`https://<portal>.bitrix24.ru/crm/configs/`) |
| Hardcode ID | чат default 22358; отделы `[42,44,46,50,60,66]`; пользователи 32/154/378, exclude KPI, `BROKER_USER_IDS` в импорте КЦ; стадии `C18:*`, `C26:*`, `UC_*`; entity 1080 |
| `GENERAL_BASE_CATEGORY_ID = 26` | константа, не env |
| Chat poller без ACL | любая команда из чата |
| Нет retry лимитов Bitrix | см. §5 |
| Горизонт показа 14 дней | только «Повторный показ», не «Первый» (`BUYER_SHOW_HORIZON_STAGE_IDS`) |
| Docker без `scripts/` | разовые миграции только с хоста |

Источник: `src/config.py`, `src/tools.py`, `src/prompts.py`, `src/db.py`, `src/contact_source_lock.py`, `README.md`, `DEPLOY.md`, `crontab.txt`, `plans/CHANGE-HISTORY.md`.

---

## Не удалось установить

1. Какие **scope** выданы боевому входящему вебхуку (в коде не хранятся).
2. Фактические **значения** `.env` на сервере (намеренно не читались).
3. Совпадает ли прод-crontab с файлом `crontab.txt` на хосте.
4. Живая версия внешнего MCP (`v0.2.0` только в плане; скрипт теста отсутствует).
5. Используется ли `mcp.json.example` в реальном Cursor у команды (в репо нет обязательного `mcp.json`).
6. Есть ли второй вебхук `EXCLUSIVE_NOTIFY_WEBHOOK_URL` в проде.
7. Полный состав поля `COMMENTS` BitrixGPT (формат портала) — код только парсит текст.
8. Юридическое лицо / заказчик / договор — в манифесте автор-плейсхолдер `"Your Name"`.
9. Покрывает ли вебхук модуль телефонии: код предполагает, что `voximplant.statistic.get` часто пуст.
10. Почему `DEPT_CHAT_MAP` в примере указывает все отделы в один чат 22358, а `DEPLOY.md` — на другие `chat_id`.
11. Назначение каталогов `data/chat_17884_files`, `data/expenses_august_2026_files` — код на них не ссылается.
12. Используется ли таблица `opened_leads_log` внешней утилитой вне репозитория.

---

**Итог для передачи на юрлицо:** передаётся **batch-аудитор CRM** (cron/Docker, SQLite, чаты Bitrix24, опционально DeepSeek для одного джоба), а не платформа MCP. MCP в составе поставки — пример конфига IDE к публичной документации Bitrix24; к порталу заказчика он не подключается и данные CRM не меняет.
