# Рейтинг брокеров по ведению CRM

Единый рейтинг **0–100%** по четырём компонентам + бонус за чистые дни.

## Формула

```
rating = min(100,
  40% × CRM + 15% × Portfolio + 25% × Tasks + 20% × Engagement + clean_day_bonus
)
```

| Компонент | Штрафы / бонусы |
|-----------|-----------------|
| **CRM (40%)** | 14 правил аудита: лиды, покупатели, продавцы (`violations`, routine) |
| **Portfolio (15%)** | Лид → «Общие лиды» (−12), сделка → «Общая база» (−18) |
| **Tasks (25%)** | Просрочки; +20 если все задачи с дедлайном закрыты в срок |
| **Engagement (20%)** | CRM-визиты + прочтение важных постов ленты |
| **Чистые дни** | +0.5% за рабочий день без нарушений CRM (max +5%) |

## Severity штрафы (CRM)

| Severity | Штраф |
|----------|-------|
| medium | −4 |
| high | −6 |
| very high | −10 |

## Светофор

| Балл | Уровень |
|------|---------|
| ≥ 80% | 🟢 ОТЛИЧНО |
| 50–79% | 🟡 СРЕДНЕ |
| < 50% | 🔴 ПЛОХО |

## Модули

| Файл | Роль |
|------|------|
| `src/broker_rating.py` | Формула, leaderboard, scorecard |
| `src/broker_rating_collectors.py` | Ежедневный сбор метрик из Bitrix |
| `src/weekly_report.py` | Секция рейтинга + snapshot по пятницам |
| `src/broker_score.py` | Персональный отчёт `!score <ID>` |

## Cron

| Job | Расписание | Команда |
|-----|-----------|---------|
| Дневные метрики | пн–пт 19:00 МСК | `python src/broker_rating_collectors.py --daily` |
| Weekly + рейтинг | пт 18:00 МСК | `python src/weekly_report.py` |

## Chat-команды

- `!rating`, `/rating`, `рейтинг` — общий рейтинг
- `!rating <отдел>` — рейтинг по отделу
- `!score <ID>` — персональный scorecard

## Env

```env
BROKER_RATING_PERIOD_DAYS=30
BROKER_RATING_TOP_N=10
BROKER_RATING_NEWS_TAG=
BROKER_RATING_WEIGHTS_JSON={}
```

## БД (`data/violations.db`)

- `broker_ratings` — еженедельные snapshots
- `broker_daily_metrics` — CRM-визиты, чистые дни, лента
- `broker_shared_leads` — лиды, ушедшие в «Общие лиды»
