# b24-ai-auditor

AI-аудитор для Битрикс24 на базе LangGraph + DeepSeek API.

Автоматический контроль соблюдения регламента работы с CRM: лиды, сделки покупателей и продавцов, уведомления о нарушениях в чаты РОПов и сводный отчёт руководству.

## Архитектура v2

```mermaid
flowchart TB
    subgraph Collectors["Collectors"]
        LC["lead_collector\nвсе лиды + таймлайн"]
        BC["buyer_collector\nсделки покупателей + таймлайн"]
        SC["seller_collector\nсделки продавцов + таймлайн"]
        GC["general_base_collector\nОбщая база + таймлайн"]
    end

    subgraph Analysts["Analysts"]
        LA["lead_analyst\nанализ лидов"]
        BDA["buyer_deal_analyst\nанализ сделок"]
        SDA["seller_deal_analyst\nанализ продавцов"]
        GBA["general_base_analyst\n2 дня на план"]
        BCC["buyer_calls_controller\nпропущенные звонки (сделки)"]
        MCC["missed_calls_controller\nпропущенные звонки (лиды)"]
    end

    subgraph Output["Dispatcher"]
        RD["report_dispatcher\nсводка → чат 22358\nотделы → чаты РОПов"]
    end

    LC --> LA --> MCC
    BC --> BDA --> BCC
    SC --> SDA
    GC --> GBA
    SDA --> MERGE["merge"]
    GBA --> MERGE
    MCC --> MERGE
    BCC --> MERGE
    MERGE --> RD

    B24["Bitrix24 API\ncrm + voximplant + im"] --> Collectors
    DS["DeepSeek API\ndeepseek-v4-flash"] --> Analysts
    RD --> CHATS["Чаты РОПов\n(6 отделов)"]
```

> **Важно:** аналитики основного аудита — детерминированные (Python), LLM (DeepSeek) используется только в контроле качества квалификации лидов (`lead_quality_audit.py`).

**Маршрутизация отчётов:** сводка уходит в общий чат (`REPORT_CHAT_ID = 22358` в `graph.py`). Отчёты по отделам — в чаты РОПов (`DEPT_CHAT_MAP`: Кретов, Горяинов, Трофимова, Волкова, Шпырная). Отделы без маппинга пропускаются.

## Стек

- **Язык:** Python 3.11+
- **Оркестрация:** LangGraph
- **LLM:** DeepSeek API (`deepseek-v4-flash`, аналитики лидов и звонков)
- **CRM:** fast_bitrix24
- **Конфигурация:** pydantic-settings + `.env`
- **Деплой:** Docker + cron (пн–пт, 10:00 и 17:00 МСК)

Подробный чеклист деплоя: [DEPLOY.md](DEPLOY.md).

## Аналитический дашборд

Веб-дашборд по воронкам на `pars.afina-crm.ru/dashboard` — движение лидов и
сделок, конверсии, деньги, разложение до карточек Bitrix.

Данные берутся не из Bitrix напрямую, а из витрины `data/analytics.db`,
которую наполняет ETL: движение по стадиям невозможно восстановить из текущего
состояния сделки — `crm.deal.list` знает только, где она сейчас. Историю даёт
`crm.stagehistory.list`, и её надо накапливать.

```
Bitrix24 REST ──(cron /15 мин)──> ETL ──> data/analytics.db ──> FastAPI ──> nginx+TLS
                              src/analytics/                     src/web/    /dashboard
```

Быстрый старт:

```bash
python src/web/manage.py gen-secret >> .env   # ключ подписи сессий
python src/analytics/etl.py --probe           # проверить доступ к API
python src/analytics/etl.py --backfill        # первичная загрузка за 12 месяцев
python src/web/manage.py adduser director     # завести пользователя
python src/web/server.py                      # http://127.0.0.1:8080/dashboard/
```

Что важно знать про цифры:

- **Срез и когорта — разные вещи.** «Сколько стоит на стадии сейчас» отвечает
  на вопрос загрузки, «сколько сделок периода когда-либо дошли до стадии» — на
  вопрос денег. На дашборде они разведены и подписаны, потому что смешение
  этих двух — типовой способ принять дорогое неправильное решение.
- **У каждой суммы есть покрытие.** Поле «Комиссия» заполнено не везде, и
  «12,4 млн ₽» без приписки «заполнено у 68% сделок» вводит в заблуждение ровно
  там, где решается вопрос о деньгах.
- **Пороги берутся из своих данных.** «Зависшая сделка» — та, что стоит дольше
  75-го перцентиля своей же стадии, а не дольше выдуманного числа дней.

Детали установки, nginx и проверки после развёртывания: [DEPLOY.md](DEPLOY.md).

Как устроен каждый аудит, какие правила проверяются и по какому расписанию запускаются задачи: [plans/AUDITS.md](plans/AUDITS.md).

## Быстрый старт

### 1. Клонирование и настройка

```bash
git clone https://github.com/Usachevofishial-git/AI_agents_Bitrix24.git b24-ai-auditor
cd b24-ai-auditor
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux
pip install -r requirements-dev.txt
```

### 2. Конфигурация

```bash
copy .env.example .env      # Windows
# cp .env.example .env      # Linux
```

Заполните `.env`: webhook Битрикс24, ключ DeepSeek API, `REPORT_SINCE`, ID воронок.

### 3. Проверка подключения

```bash
python scripts/test_b24.py
```

### 4. Запуск аудита

```powershell
# Windows (PowerShell):
.\make.cmd run
.\make.cmd dry-run

# Linux (если установлен make):
make run
make dry-run
```

## Деплой

### Docker

```bash
docker build -t b24-ai-auditor:latest .
docker run --rm --env-file .env -v $(pwd)/logs:/app/logs b24-ai-auditor:latest
```

### Cron (Linux)

Актуальное расписание: **10:00 и 17:00 МСК** (07:00 и 14:00 UTC), пн–пт.

```bash
# Вариант 1: прямой запуск на хосте
echo "0 7,14 * * 1-5 cd /opt/b24-ai-auditor && venv/bin/python src/main.py >> logs/cron.log 2>&1" | crontab -

# Вариант 2: запуск через Docker
echo "0 7,14 * * 1-5 cd /opt/b24-ai-auditor && docker run --rm --env-file .env -v \$(pwd)/logs:/app/logs b24-ai-auditor:latest >> logs/cron.log 2>&1" | crontab -
```

Или скопировать готовую строку из `crontab.txt`:

```bash
crontab crontab.txt
crontab -l
```

## Переменные окружения

| Переменная | Описание |
|---|---|
| `B24_WEBHOOK_URL` | URL входящего вебхука Битрикс24 |
| `B24_USER_ID` | ID пользователя для API-запросов |
| `BUYERS_CATEGORY_ID` | ID воронки покупателей (по умолчанию 18) |
| `SELLERS_CATEGORY_ID` | ID воронки продавцов (по умолчанию 0) |
| `REPORT_SINCE` | Дата начала отчётного периода (YYYY-MM-DD) |
| `DEEPSEEK_API_KEY` | API-ключ DeepSeek ([platform.deepseek.com](https://platform.deepseek.com)) |
| `DEEPSEEK_BASE_URL` | Базовый URL API (по умолчанию `https://api.deepseek.com`) |
| `DEEPSEEK_MODEL` | Модель для аналитиков (по умолчанию `deepseek-v4-flash`) |
| `DEEPSEEK_V3_MODEL` | Резервная модель (по умолчанию `deepseek-v4-flash`) |
| `DRY_RUN` | `true` — только чтение, без мутаций в CRM |
| `LOG_LEVEL` | Уровень логирования (INFO, DEBUG) |
| `MANAGEMENT_CHAT_ID` | ID чата для сводных отчётов (зарезервировано; сводка сейчас в `graph.py`: 22358) |
| `ADMIN_USER_ID` | ID админа для экстренных уведомлений |
| `ANALYTICS_DB_PATH` | Файл витрины (по умолчанию `data/analytics.db`) |
| `ANALYTICS_MONTHS_BACK` | Глубина истории при первичной загрузке |
| `DASHBOARD_SECRET_KEY` | Ключ подписи сессий. Без него дашборд не стартует |
| `DASHBOARD_COOKIE_SECURE` | `true` в проде; `false` только для отладки по http |

Полный список с пояснениями — в `.env.example`.

## Makefile / make.cmd

На Windows без GNU Make используйте **`.\make.cmd`**.

| Команда | Описание |
|---|---|
| `.\make.cmd install` | venv + зависимости |
| `.\make.cmd lint` | flake8 |
| `.\make.cmd test` | pytest |
| `.\make.cmd test-b24` | проверка Bitrix24 |
| `.\make.cmd run` | полный аудит |
| `.\make.cmd dry-run` | только чтение |
| `.\make.cmd exclusive-expiry` | напоминания по эксклюзивам |
| `.\make.cmd docker-build` | сборка образа |
| `make etl` / `etl-full` / `etl-backfill` | загрузка витрины |
| `make dashboard` | запуск веб-дашборда |
| `make dashboard-adduser USER_LOGIN=...` | завести пользователя дашборда |

## Структура проекта

```
b24-ai-auditor/
├── src/
│   ├── config.py              # Pydantic Settings
│   ├── main.py                # Точка входа аудита
│   ├── tools.py               # Bitrix24 API + правила аудита
│   ├── graph.py               # LangGraph граф v2
│   ├── prompts.py             # Системные промпты LLM
│   ├── notify.py              # Отправка в чаты Bitrix24
│   ├── db.py                  # SQLite (violations, brokers, exclusive)
│   ├── weekly_report.py       # Недельный отчёт по брокерам
│   ├── task_auditor.py        # Просроченные задачи
│   ├── owner_tracker.py       # KPI собственников
│   ├── broker_score.py        # Скоринг брокера
│   ├── chat_poller.py         # Чат-команды
│   ├── exclusive_expiry.py    # Напоминания по эксклюзивам
│   ├── analytics/             # Витрина: ETL из Bitrix + метрики
│   │   ├── schema.py          #   схема data/analytics.db
│   │   ├── client.py          #   REST-клиент с лимитом темпа и повторами
│   │   ├── stages.py          #   лента пребывания на стадиях
│   │   ├── etl.py             #   backfill / incremental / full
│   │   └── metrics.py         #   определения всех цифр дашборда
│   └── web/                   # Дашборд: FastAPI + Jinja2 + SVG-графики
│       ├── security.py        #   запрет по умолчанию, сессии, CSRF
│       ├── manage.py          #   управление пользователями
│       └── templates/, static/
├── tests/
├── logs/
├── data/                      # SQLite + runtime (gitignored)
├── plans/                     # Планы и промпты разработки
├── scripts/
│   ├── list_departments.py
│   └── test_b24.py
├── Dockerfile
├── docker-compose.yml
├── crontab.txt
├── Makefile
├── make.cmd
├── requirements.txt
├── requirements-dev.txt
├── DEPLOY.md
└── README.md
```
