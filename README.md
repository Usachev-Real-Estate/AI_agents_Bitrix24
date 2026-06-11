# b24-ai-auditor

AI-аудитор для Битрикс24 на базе LangGraph + DeepSeek (через RouterAI).

Автоматический контроль соблюдения регламента работы с CRM: лиды, сделки покупателей и продавцов, уведомления о нарушениях в чаты РОПов и сводный отчёт руководству.

## Архитектура v2

```mermaid
flowchart TB
    subgraph Collectors["Collectors (3 агента)"]
        LC["lead_collector\nвсе лиды + таймлайн"]
        BC["buyer_collector\nсделки покупателей + таймлайн"]
        SC["seller_collector\nсделки продавцов + таймлайн"]
    end

    subgraph Analysts["Analysts (4 агента)"]
        LA["lead_analyst\nанализ лидов"]
        BDA["buyer_deal_analyst\nанализ сделок"]
        BCC["buyer_calls_controller\nпропущенные звонки (сделки)"]
        MCC["missed_calls_controller\nпропущенные звонки (лиды)"]
    end

    subgraph Output["Dispatcher"]
        RD["report_dispatcher\nсводка → чат 22358\nотделы → чаты РОПов"]
    end

    LC --> LA --> MCC
    BC --> BDA --> BCC
    SC --> MERGE["merge"]
    MCC --> MERGE
    BCC --> MERGE
    MERGE --> RD

    B24["Bitrix24 API\ncrm + voximplant + im"] --> Collectors
    RA["RouterAI\nDeepSeek-R1"] --> Analysts
    RD --> CHATS["Чаты РОПов\n(6 отделов)"]
```

**Маршрутизация отчётов:** сводка уходит в общий чат (`REPORT_CHAT_ID = 22358` в `graph.py`). Отчёты по отделам — в чаты РОПов (`DEPT_CHAT_MAP`: Кретов, Горяинов, Каратевский, Трофимова, Волкова, Шпырная). Отделы без маппинга пропускаются.

## Стек

- **Язык:** Python 3.11+
- **Оркестрация:** LangGraph
- **LLM:** DeepSeek-R1 через RouterAI (аналитики)
- **CRM:** fast_bitrix24
- **Конфигурация:** pydantic-settings + `.env`
- **Деплой:** Docker + cron (пн–пт, 10:00 и 17:00 МСК)

Подробный чеклист деплоя: [DEPLOY.md](DEPLOY.md).

## Быстрый старт

### 1. Клонирование и настройка

```bash
git clone https://github.com/DanilaYukin/AI_agents_CRM.git b24-ai-auditor
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

Заполните `.env`: webhook Битрикс24, ключ RouterAI, `REPORT_SINCE`, ID воронок.

### 3. Проверка подключения

```bash
python src/test_b24.py
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
| `ROUTERAI_API_KEY` | API-ключ RouterAI |
| `ROUTERAI_BASE_URL` | Базовый URL RouterAI |
| `ROUTERAI_R1_MODEL` | Модель для аналитиков (`deepseek/deepseek-v4-flash`) |
| `ROUTERAI_V3_MODEL` | Модель (`deepseek/deepseek-v4-flash`) |
| `DRY_RUN` | `true` — только чтение, без мутаций в CRM |
| `LOG_LEVEL` | Уровень логирования (INFO, DEBUG) |
| `MANAGEMENT_CHAT_ID` | ID чата для сводных отчётов (зарезервировано; сводка сейчас в `graph.py`: 22358) |
| `ADMIN_USER_ID` | ID админа для экстренных уведомлений |

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
| `.\make.cmd docker-build` | сборка образа |

## Структура проекта

```
b24-ai-auditor/
├── src/
│   ├── config.py          # Pydantic Settings
│   ├── main.py            # Точка входа
│   ├── tools.py           # Инструменты (Bitrix24 API + звонки)
│   ├── graph.py           # LangGraph граф v2
│   ├── prompts.py         # Системные промпты
│   └── notify.py          # Отправка уведомлений в Bitrix24
├── tests/
├── logs/
├── plans/prompts/         # Промпты для Cursor (step-01..47)
├── scripts/
│   └── list_departments.py
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
