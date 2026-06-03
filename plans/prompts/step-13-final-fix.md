# Промпт для Cursor — Этап 8: финальные правки (модели + деплой)

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `.env`, `Dockerfile`, `docker-compose.yml`, `crontab.txt`, `README.md`.

---

## Задача 1: Обе модели — v4-flash

В файле `.env` приведи обе модели к `deepseek/deepseek-v4-flash` (как в документации RouterAI):

Найди:
```ini
ROUTERAI_R1_MODEL=deepseek/deepseek-reasoner
ROUTERAI_V3_MODEL=deepseek/deepseek-chat
```

Замени на:
```ini
ROUTERAI_R1_MODEL=deepseek/deepseek-v4-flash
ROUTERAI_V3_MODEL=deepseek/deepseek-v4-flash
```

Также обнови `.env.example` — замени значения по умолчанию:
```ini
ROUTERAI_R1_MODEL=deepseek/deepseek-v4-flash
ROUTERAI_V3_MODEL=deepseek/deepseek-v4-flash
```

---

## Задача 2: Dockerfile — убрать COPY .env (секреты в образе)

В `Dockerfile` удали строку:
```dockerfile
COPY .env ./
```

И замени блок создания пользователя (строки 22-23):
```dockerfile
RUN useradd -m auditor && chown -R auditor:auditor /app
USER auditor
```
На:
```dockerfile
RUN useradd -m auditor && chown -R auditor:auditor /app && \
    mkdir -p /app/logs && chown auditor:auditor /app/logs
USER auditor
```

Итоговый Dockerfile должен выглядеть так:
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/

# Run as non-root user
RUN useradd -m auditor && chown -R auditor:auditor /app && \
    mkdir -p /app/logs && chown auditor:auditor /app/logs
USER auditor

CMD ["python", "src/main.py"]
```

Причина: `.env` с ключами НЕ должен попадать в слой Docker-образа. Переменные передаются через `docker run --env-file .env` или `env_file` в docker-compose.

---

## Задача 3: docker-compose.yml — убрать version, добавить command

Замени ВЕСЬ файл `docker-compose.yml` на:

```yaml
services:
  auditor:
    build: .
    container_name: b24-ai-auditor
    restart: "no"
    env_file:
      - .env
    volumes:
      - ./logs:/app/logs
    command: ["python", "src/main.py"]
```

Изменения:
- Убрана `version: "3.8"` — не нужна в Docker Compose v2+
- `restart: "no"` — контейнер для разового запуска
- Добавлен `command` — явная команда запуска

---

## Задача 4: crontab.txt — путь для хоста

Замени содержимое `crontab.txt` на:

```crontab
# b24-ai-auditor: запуск каждые 2 часа в рабочее время (пн-пт, 9:00-19:00)
# Вариант 1: запуск на хосте (без Docker)
# 0 9-19/2 * * 1-5 cd /opt/b24-ai-auditor && /opt/b24-ai-auditor/venv/bin/python src/main.py >> logs/cron.log 2>&1

# Вариант 2: запуск в Docker-контейнере
0 9-19/2 * * 1-5 cd /opt/b24-ai-auditor && docker run --rm --env-file .env -v $(pwd)/logs:/app/logs b24-ai-auditor:latest >> logs/cron.log 2>&1
```

---

## Задача 5: README.md — обновить секцию деплоя

В `README.md` в секции «Деплой»:

### Docker
Замени:
```bash
docker build -t b24-ai-auditor:latest .
docker run --rm --env-file .env -v $(pwd)/logs:/app/logs b24-ai-auditor:latest
```

На:
```bash
docker build -t b24-ai-auditor:latest .
docker run --rm --env-file .env -v $(pwd)/logs:/app/logs b24-ai-auditor:latest
```
(без изменений, просто проверь что строка корректна)

### Cron (Linux)
Замени блок на:
```bash
# Вариант 1: прямой запуск на хосте
crontab -l > my_crontab
echo "0 9-19/2 * * 1-5 cd /opt/b24-ai-auditor && venv/bin/python src/main.py >> logs/cron.log 2>&1" >> my_crontab
crontab my_crontab

# Вариант 2: запуск через Docker
echo "0 9-19/2 * * 1-5 cd /opt/b24-ai-auditor && docker run --rm --env-file .env -v \$(pwd)/logs:/app/logs b24-ai-auditor:latest >> logs/cron.log 2>&1" >> my_crontab
```

### Proxmox
Без изменений.

---

## Проверка

После внесения всех изменений:
1. `.\make.cmd lint` — должен пройти
2. `.\make.cmd dry-run` — полный пайплайн с v4-flash
3. `docker build -t b24-ai-auditor:latest .` — сборка без .env в образе (опционально)
