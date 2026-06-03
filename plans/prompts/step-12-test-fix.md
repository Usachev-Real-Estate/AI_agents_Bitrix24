# Промпт для Cursor — Этап 7: микроправки перед тестированием

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).
> Обновляет: `make.cmd`, `.env`, `README.md`.

---

## Задача 1: Добавить цель `test-b24` в `make.cmd`

В файле `make.cmd` добавь новую цель `test-b24` между строками `:test` и `:run`:

Текущий блок (строки 28–30):
```cmd
:test
call venv\Scripts\pytest tests/ -v
exit /b %errorlevel%
```

Добавь ПОСЛЕ блока `:test` и ПЕРЕД `:run`:

```cmd
:test-b24
call venv\Scripts\python src/test_b24.py
exit /b %errorlevel%
```

Также добавь `test-b24` в usage-строку (строка 14):

Было:
```cmd
echo Usage: make.cmd ^<install^|lint^|test^|run^|dry-run^|docker-build^>
```

Замени на:
```cmd
echo Usage: make.cmd ^<install^|lint^|test^|test-b24^|run^|dry-run^|docker-build^>
```

И добавь новый переход в блоке `if` (после `if /i "%~1"=="test" goto test`):
```cmd
if /i "%~1"=="test-b24" goto testb24
```

---

## Задача 2: Исправить `ROUTERAI_R1_MODEL` в `.env`

В файле `.env` найди строку:
```
ROUTERAI_R1_MODEL=deepseek/deepseek-v4-flash
```

Замени на:
```
ROUTERAI_R1_MODEL=deepseek/deepseek-reasoner
```

Причина: `deepseek-v4-flash` — быстрая модель без reasoning. Для Analyst-нода (поиск нарушений по строгим правилам) нужна модель с reasoning (`deepseek-reasoner`), иначе structured JSON-ответ может быть нестабильным.

---

## Задача 3: Исправить `MANAGEMENT_CHAT_ID` в `.env`

В файле `.env` найди строку:
```
MANAGEMENT_CHAT_ID=chat12345
```

Замени на:
```
MANAGEMENT_CHAT_ID=
```

Причина: `chat12345` — плейсхолдер, несуществующий чат. При реальном запуске `send_management_report` попытается отправить сообщение в несуществующий чат и вернёт ошибку от API Битрикс24. Dispatcher в новой версии (step-11) умеет обрабатывать пустой `MANAGEMENT_CHAT_ID` — просто пропускает отправку отчёта.

Когда появится реальный ID чата руководителей — впишите его сюда.

---

## Задача 4 (бонус): Обновить README — добавить `test-b24`

В файле `README.md` в таблице «Makefile команды» добавь строку:

```
| `.\make.cmd test-b24` | Проверить подключение к Битрикс24 |
```

После строки `| .\make.cmd test | Запустить тесты |`.

---

## Проверка

После внесения изменений:
1. `.\make.cmd test-b24` — должен запустить проверку подключения к Битрикс24
2. `.\make.cmd lint` — должен пройти
3. `.\make.cmd dry-run` — должен отработать полный пайплайн (Auditor → Analyst → Dispatcher)
