# step-48-post-review-fixes.md

## Контекст

После реализации плана `fix-and-optimize-plan.md` проведено код-ревью. Найдено 5 недочётов, которые нужно исправить.

## Задача

Исправить 5 пунктов:

1. 🔴 Добавить `RotatingFileHandler` в `src/config.py`
2. 🔴 Вернуть потерянные цели в `Makefile` и `make.cmd`
3. 🟡 Удалить `setup.py` (дублируется с `pyproject.toml`)
4. 🟡 Синхронизировать `requirements-dev.txt` с `pyproject.toml`
5. 🟡 Добавить обработку ошибок JSON в `dept_chat_map` (src/config.py)

---

## Шаг 1 — RotatingFileHandler в `src/config.py`

### Что сделать

В функции `setup_logging()` заменить `logging.FileHandler` на `logging.handlers.RotatingFileHandler`.

### Где

Файл: `src/config.py`

### Как

1. Добавить импорт вверху файла:
```python
from logging.handlers import RotatingFileHandler
```

2. Найти строку (примерно строка 91):
```python
file_handler = logging.FileHandler(log_dir / "audit.log", encoding="utf-8")
```

3. Заменить на:
```python
file_handler = RotatingFileHandler(
    log_dir / "audit.log",
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
    encoding="utf-8",
)
```

4. Остальной код в `setup_logging` не трогать.

---

## Шаг 2 — Вернуть цели в Makefile и make.cmd

### Что сделать

Добавить цели `lint`, `dry-run`, `docker-build` в оба файла.

### Где

Файлы: `Makefile` (Linux/macOS) и `make.cmd` (Windows)

### Как — Makefile

Добавить после цели `test`:

```makefile
lint:
	$(PYTHON) -m flake8 src/ tests/

dry-run:
	DRY_RUN=true $(PYTHON) src/main.py

docker-build:
	docker build -t b24-ai-auditor:latest .
```

### Как — make.cmd

Добавить перед последней строкой `echo Usage:`:

```cmd
if "%1"=="lint" (
    %PYTHON% -m flake8 src\ tests\
    exit /b
)

if "%1"=="dry-run" (
    set DRY_RUN=true && %PYTHON% src\main.py
    exit /b
)

if "%1"=="docker-build" (
    docker build -t b24-ai-auditor:latest .
    exit /b
)
```

И обновить строку Usage:
```cmd
echo Usage: make.cmd [install^|run^|test^|lint^|dry-run^|docker-build]
```

---

## Шаг 3 — Удалить `setup.py`

### Что сделать

Удалить файл `setup.py` из корня проекта. Вся конфигурация пакета теперь в `pyproject.toml`.

### Где

Файл: `setup.py` (корень проекта)

### Как

Просто удалить файл. Никакие импорты в проекте на него не ссылаются. `pip install -e .[dev]` будет использовать `pyproject.toml`.

---

## Шаг 4 — Синхронизировать `requirements-dev.txt` с `pyproject.toml`

### Что сделать

Привести `requirements-dev.txt` в соответствие с `[project.optional-dependencies] dev` из `pyproject.toml`.

### Где

Файл: `requirements-dev.txt`

### Текущее содержимое

```
-r requirements.txt
flake8>=7.0.0
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

### Новое содержимое

Синхронизировать с `pyproject.toml` (там сейчас: `mypy>=1.0.0`, `black>=24.1.0`, `flake8>=7.0.0`, `pytest`):

```
-r requirements.txt
mypy>=1.0.0
black>=24.1.0
flake8>=7.0.0
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

**Важно:** `pytest-asyncio` оставить — он был в исходном файле и нужен для асинхронных тестов, даже если его нет в `pyproject.toml` (потом можно добавить и туда).

И заодно добавить `pytest-asyncio` в `pyproject.toml` в секцию `dev`:

```toml
dev = [
    "mypy>=1.0.0",
    "black>=24.1.0",
    "flake8>=7.0.0",
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0"
]
```

---

## Шаг 5 — Защитить `dept_chat_map` property от битого JSON

### Что сделать

Обернуть `json.loads()` в `try/except`, чтобы битый `DEPT_CHAT_MAP_JSON` в `.env` не ронял приложение.

### Где

Файл: `src/config.py`, property `dept_chat_map` (примерно строка 54-57)

### Текущий код

```python
@property
def dept_chat_map(self) -> dict[int, int]:
    """Return parsed DEPT_CHAT_MAP mapping from JSON string."""
    return {int(k): int(v) for k, v in json.loads(self.dept_chat_map_json).items()}
```

### Новый код

```python
@property
def dept_chat_map(self) -> dict[int, int]:
    """Return parsed DEPT_CHAT_MAP mapping from JSON string.

    Returns empty dict on malformed JSON — logged as warning.
    """
    try:
        return {
            int(k): int(v)
            for k, v in json.loads(self.dept_chat_map_json).items()
        }
    except (json.JSONDecodeError, ValueError, AttributeError) as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Invalid DEPT_CHAT_MAP_JSON: %s",
            exc,
        )
        return {}
```

---

## Проверка

После всех изменений:

1. `python -m flake8 src/ tests/` — не должно быть новых ошибок
2. `python -m pytest tests/ -v` — все тесты должны проходить
3. `python src/main.py` — при наличии `.env` должен запуститься без ошибок
4. Убедиться, что `setup.py` действительно удалён
5. Убедиться, что `make lint` / `make dry-run` работают (на Linux)
