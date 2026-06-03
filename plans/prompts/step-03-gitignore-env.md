# Промпт для Cursor — Создание `.gitignore` и `.env.example`

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode). Создаёт сразу 2 файла.

---

Создай два файла в корне проекта:

## Файл 1: `.gitignore`

Стандартный `.gitignore` для Python-проекта на Windows + Linux:

```gitignore
# Secrets
.env

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
*.egg-info/
dist/
build/
eggs/
*.egg

# Virtual Environment
venv/
.venv/
env/

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# Logs
logs/
*.log

# Testing
.pytest_cache/
.coverage
htmlcov/

# Linting
.flake8_cache
.mypy_cache/
.ruff_cache/

# OS
Thumbs.db
.DS_Store
Desktop.ini

# Docker
.docker/

# Jupyter
.ipynb_checkpoints/
```

## Файл 2: `.env.example`

Шаблон переменных окружения (без реальных ключей):

```ini
# ============================================
# Bitrix24
# ============================================
B24_WEBHOOK_URL=https://your-domain.bitrix24.ru/rest/1/your-webhook-code/
B24_USER_ID=1

# ============================================
# RouterAI (российский шлюз к DeepSeek)
# ============================================
ROUTERAI_API_KEY=sk-your-routerai-key
ROUTERAI_BASE_URL=https://routerai.ru/api/v1
ROUTERAI_R1_MODEL=deepseek/deepseek-reasoner
ROUTERAI_V3_MODEL=deepseek/deepseek-chat

# ============================================
# Режим работы
# ============================================
# DRY_RUN=true — только чтение, без мутаций в Битрикс24
DRY_RUN=true
LOG_LEVEL=INFO

# ============================================
# Уведомления
# ============================================
MANAGEMENT_CHAT_ID=chat12345
ADMIN_USER_ID=1
```

**Требования:**
- Оба файла в кодировке UTF-8
- Переносы строк: LF
- Никаких реальных ключей — только плейсхолдеры
