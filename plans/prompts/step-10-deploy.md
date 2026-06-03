# Промпт для Cursor — Этапы 4 и 5: логирование + деплой

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).  
> Создаёт/обновляет несколько файлов.

---

## Задача 1: Логирование в файл (Этап 4)

Обнови `src/config.py` — добавь в функцию `setup_logging` запись в файл `logs/audit.log` ДОПОЛНИТЕЛЬНО к выводу в консоль.

Текущая функция:
```python
def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
```

Нужно добавить:
- Создание директории `logs/` если её нет (используй `Path.mkdir(parents=True, exist_ok=True)`)
- `FileHandler` в `logs/audit.log` с тем же форматом
- Оставить вывод в консоль (stderr)

```python
def setup_logging(level: str) -> None:
    """Configure root logger: console + file."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    
    # Ensure logs directory exists
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # Console handler
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(logging.Formatter(fmt))
    root_logger.addHandler(console)
    
    # File handler
    file_handler = logging.FileHandler(log_dir / "audit.log", encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(fmt))
    root_logger.addHandler(file_handler)
```

Добавь импорт `from pathlib import Path` если его ещё нет.

---

## Задача 2: Dockerfile (Этап 5)

Создай `Dockerfile` в корне проекта:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (if needed by fastbitrix24)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY .env ./

# Create logs directory
RUN mkdir -p logs

# Run as non-root user
RUN useradd -m auditor && chown -R auditor:auditor /app
USER auditor

CMD ["python", "src/main.py"]
```

---

## Задача 3: crontab для периодического запуска (Этап 5)

Создай файл `crontab.txt` в корне проекта:

```crontab
# b24-ai-auditor: запуск каждые 2 часа в рабочее время (пн-пт, 9:00-19:00)
0 9-19/2 * * 1-5 cd /app && python src/main.py >> /app/logs/cron.log 2>&1
```

И создай `docker-compose.yml` в корне проекта (опционально, для удобства деплоя):

```yaml
version: "3.8"

services:
  auditor:
    build: .
    container_name: b24-ai-auditor
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./logs:/app/logs
    # Ручной запуск (cron настраивается на хосте или через отдельный сервис)
```

---

## Задача 4: Обновление `README.md`

Добавь в README секцию "Деплой" после секции "Быстрый старт":

```markdown
## Деплой

### Docker

```bash
docker build -t b24-ai-auditor:latest .
docker run --rm --env-file .env -v $(pwd)/logs:/app/logs b24-ai-auditor:latest
```

### Cron (Linux)

```bash
# Копировать crontab
crontab crontab.txt

# Или вручную: каждые 2 часа в рабочее время
# 0 9-19/2 * * 1-5 cd /path/to/b24-ai-auditor && python src/main.py >> logs/cron.log 2>&1
```

### Proxmox

1. Перенести проект на VM: `scp -r b24-ai-auditor/ user@vm:/opt/`
2. На VM: `cd /opt/b24-ai-auditor && docker build -t b24-ai-auditor .`
3. Настроить cron на хосте: `crontab /opt/b24-ai-auditor/crontab.txt`
4. Мониторинг логов: `tail -f /opt/b24-ai-auditor/logs/audit.log`
```

---

## Проверка

После создания всех файлов:
1. `.\make.cmd lint` — должен пройти
2. `.\make.cmd dry-run` — должен создать `logs/audit.log`
3. `docker build -t b24-ai-auditor:latest .` — проверить сборку образа (опционально)
