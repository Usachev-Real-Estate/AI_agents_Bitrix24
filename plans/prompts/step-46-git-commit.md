# Промпт для Cursor — Подготовка к коммиту и пушу на GitHub

> Скопируйте содержимое ниже и отправьте в Cursor (Agent mode).

---

## Шаг 1 — Проверить `.gitignore`

Добавить `.cursor/` в `.gitignore` (если ещё нет):

```
# Cursor IDE
.cursor/
```

Проверить, что `.gitignore` содержит все нужные правила:
- `.env` ✅
- `logs/` ✅
- `venv/` ✅
- `__pycache__/` ✅
- `.vscode/` ✅
- `.idea/` ✅
- `*.egg-info/` ✅

---

## Шаг 2 — Проверить, нет ли секретов в файлах

```bash
git status
```

Убедиться, что:
- `.env` **не** в списке отслеживаемых/изменённых файлов
- `logs/audit.log` **не** в списке
- `__pycache__/` **не** в списке

Если `.env` или `logs/` уже отслеживаются — удалить из индекса:
```bash
git rm --cached .env
git rm --cached -r logs/
```

---

## Шаг 3 — Добавить файлы и закоммитить

```bash
git add .
git status
```

Проверить, что в staged:
- `src/graph.py` — ✅
- `src/tools.py` — ✅
- `crontab.txt` — ✅
- `plans/prompts/step-*.md` — ✅
- `.gitignore` (если меняли) — ✅
- `.env` — ❌ НЕ должно быть
- `logs/` — ❌ НЕ должно быть
- `__pycache__/` — ❌ НЕ должно быть

Коммит:
```bash
git commit -m "v2 stable: ROP routing, call classification, cron 10:00/17:00 MSK

- graph.py: seller_collector → merge edge restored
- graph.py: DEPT_CHAT_MAP → ROP chat routing (5 departments)
- graph.py: removed call stats and звонки blocks (temporary)
- tools.py: _fetch_user_calls_for_audit — 3-API approach (voximplant → activity.list)
- tools.py: incoming classification: 'пропущен' text → missed, otherwise other
- .env: REPORT_SINCE=2026-06-02
- crontab.txt: 0 7,14 * * 1-5 (10:00 and 17:00 MSK)
- plans: steps 43-45 documentation"
```

---

## Шаг 4 — Пуш

```bash
git push origin main
```

Или если ветка другая:
```bash
git push origin <branch-name>
```

---

## Проверка после пуша

1. Зайти на GitHub — убедиться, что `.env` и `logs/` не попали в репозиторий
2. Проверить, что все изменённые файлы на месте
3. Убедиться, что `ROUTERAI_API_KEY` и `B24_WEBHOOK_URL` не видны в коде
