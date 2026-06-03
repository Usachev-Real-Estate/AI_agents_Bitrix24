# Промпт для Cursor — Создание `setup.py`, `requirements.txt`, `requirements-dev.txt`

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode). Создаёт 3 файла в корне проекта.

---

Создай три файла в корне проекта для управления зависимостями Python-проекта `b24-ai-auditor`.

## Файл 1: `setup.py`

```python
"""Setup configuration for b24-ai-auditor package."""

from setuptools import setup, find_packages

setup(
    name="b24-ai-auditor",
    version="0.1.0",
    description="AI-аудитор для Битрикс24 на LangGraph + DeepSeek (через RouterAI)",
    author="Your Name",
    python_requires=">=3.11",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "langgraph>=0.2.0",
        "langchain-openai>=0.2.0",
        "fastbitrix24>=0.3.0",
        "python-dotenv>=1.0.0",
        "pydantic-settings>=2.0.0",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
```

## Файл 2: `requirements.txt`

```txt
langgraph>=0.2.0
langchain-openai>=0.2.0
fastbitrix24>=0.3.0
python-dotenv>=1.0.0
pydantic-settings>=2.0.0
```

## Файл 3: `requirements-dev.txt`

```txt
-r requirements.txt
flake8>=7.0.0
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

**Требования:**
- Все 3 файла в кодировке UTF-8
- Переносы строк: LF
- `requirements-dev.txt` начинается с `-r requirements.txt` (наследование)
