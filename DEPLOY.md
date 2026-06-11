# Деплой b24-ai-auditor на сервер

## 1. Подготовка сервера

```bash
# Установить Docker (если нет)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

Перелогиньтесь или выполните `newgrp docker`, чтобы группа `docker` применилась.

## 2. Клонирование и настройка

```bash
cd /opt
git clone https://github.com/DanilaYukin/AI_agents_CRM.git b24-ai-auditor
cd b24-ai-auditor

# Создать .env из примера
cp .env.example .env
nano .env  # Вставить реальные ключи и URL
```

**Важно:** в `.env` установить:

- `DRY_RUN=false` — для боевого запуска (сообщения в чаты)
- `LOG_LEVEL=INFO`
- `REPORT_SINCE=2026-06-02` (или актуальную дату начала контроля)
- `B24_WEBHOOK_URL`, `ROUTERAI_API_KEY`, `BUYERS_CATEGORY_ID`, `SELLERS_CATEGORY_ID`

Файл `.env` не коммитится в git — храните только на сервере.

## 3. Сборка Docker-образа

```bash
docker build -t b24-ai-auditor:latest .
```

## 4. Тестовый запуск

```bash
# Dry-run (без мутаций в CRM — DRY_RUN из .env или переопределение)
docker run --rm --env-file .env \
  -e DRY_RUN=true \
  -v $(pwd)/logs:/app/logs \
  b24-ai-auditor:latest

# Проверить логи
tail -100 logs/audit.log
```

В логах ожидаются: сбор лидов/сделок, аналитики, `report sent to ROP chat`, сводка в chat 22358.

## 5. Установка cron

Расписание: **10:00 и 17:00 МСК** (07:00 и 14:00 UTC), понедельник–пятница.

```bash
# Активировать crontab (в crontab.txt — вариант Docker)
crontab crontab.txt

# Проверить
crontab -l
```

Убедитесь, что каталог `logs/` существует и доступен для записи:

```bash
mkdir -p logs
```

## 6. Мониторинг

```bash
# Логи аудита
tail -f /opt/b24-ai-auditor/logs/audit.log

# Логи cron
tail -f /opt/b24-ai-auditor/logs/cron.log
```

## 7. Обновление

```bash
cd /opt/b24-ai-auditor
git pull
docker build -t b24-ai-auditor:latest .
# Cron продолжит использовать новый образ при следующем запуске
```

## 8. Откат

```bash
cd /opt/b24-ai-auditor
git checkout <previous-commit>
docker build -t b24-ai-auditor:latest .
```

## Справка: чаты РОПов

| Отдел (ID) | chat_id |
|------------|---------|
| Кретов (60) | 17710 |
| Горяинов (46) | 17712 |
| Каратевский (42) | 17716 |
| Трофимова (44) | 17708 |
| Волкова (50) | 17714 |
| Шпырная (66) | 23130 |

Сводный отчёт: чат **22358** (`REPORT_CHAT_ID` в `src/graph.py`).
