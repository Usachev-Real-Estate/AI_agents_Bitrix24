"""Метрики витрины.

Единственное место, где определены цифры дашборда. На них принимаются решения
о деньгах, поэтому у каждой метрики есть письменное определение (docstring →
подсказка в интерфейсе) и разложение до карточек Bitrix через entity_table().
Число, которое нельзя разложить до строк, — это число, которому не поверят.

Ключевое различие, которое нельзя смешивать:

* **Срез** — «сколько сделок стоит на стадии сейчас». Вопрос загрузки: где
  затор, кому разгребать.
* **Когорта** — «из сделок, созданных в июле, сколько КОГДА-ЛИБО дошли до
  Договора». Вопрос денег: какой источник окупается, какой отдел конвертирует.

Свежая когорта всегда выглядит хуже зрелой, поэтому подменять одно другим —
готовый способ принять дорогое неправильное решение. Когортные метрики
считаются по fact_stage_event («достигал стадии»), а не по текущей STAGE_ID.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

ENTITY_DEAL = "deal"
ENTITY_LEAD = "lead"

# Пресеты периода. Значение — сколько дней назад начинается период.
PERIOD_PRESETS: dict[str, str] = {
    "today": "Сегодня",
    "7d": "7 дней",
    "30d": "30 дней",
    "90d": "90 дней",
    "month": "Текущий месяц",
    "prev_month": "Прошлый месяц",
    "quarter": "Текущий квартал",
    "12m": "12 месяцев",
}
DEFAULT_PERIOD = "30d"


# --------------------------------------------------------------------------
# период
# --------------------------------------------------------------------------

def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def resolve_period(
    preset: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, str]:
    """Границы периода: [since, until). Правая граница исключающая.

    Включающая правая граница — источник вечных расхождений на единицу:
    «по 31 августа» либо теряет весь последний день, либо задваивает его при
    сравнении с соседним периодом.
    """
    today = datetime.now(timezone.utc).date()

    if start or end:
        since_day = _parse_day(start) or (today - timedelta(days=30))
        until_day = _parse_day(end) or today
        since = _day_start(since_day)
        until = _day_start(until_day) + timedelta(days=1)
        label = f"{since_day.isoformat()} — {until_day.isoformat()}"
        return {"since": _iso(since), "until": _iso(until), "label": label,
                "preset": "custom", "start": since_day.isoformat(),
                "end": until_day.isoformat()}

    preset = preset if preset in PERIOD_PRESETS else DEFAULT_PERIOD
    until = _day_start(today) + timedelta(days=1)

    if preset == "today":
        since = _day_start(today)
    elif preset == "month":
        since = _day_start(today.replace(day=1))
    elif preset == "prev_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        since = _day_start(last_prev.replace(day=1))
        until = _day_start(first_this)
    elif preset == "quarter":
        quarter_first_month = 3 * ((today.month - 1) // 3) + 1
        since = _day_start(today.replace(month=quarter_first_month, day=1))
    elif preset == "12m":
        since = _day_start(_shift_months(today, -12))
    else:
        days = {"7d": 7, "30d": 30, "90d": 90}[preset]
        since = _day_start(today - timedelta(days=days - 1))

    return {
        "since": _iso(since), "until": _iso(until),
        "label": PERIOD_PRESETS[preset], "preset": preset,
        "start": since.date().isoformat(),
        "end": (until - timedelta(days=1)).date().isoformat(),
    }


def previous_period(period: dict[str, str]) -> dict[str, str]:
    """Предыдущий период той же длины — база для сравнения «к прошлому»."""
    since = datetime.fromisoformat(period["since"])
    until = datetime.fromisoformat(period["until"])
    span = until - since
    return {"since": _iso(since - span), "until": _iso(since), "label": "предыдущий период"}


def _shift_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# служебное
# --------------------------------------------------------------------------

def _rows(conn, sql: str, params: dict[str, Any] | Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(conn, sql: str, params: dict[str, Any] | Sequence[Any] = ()) -> dict[str, Any]:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def percentile(values: Sequence[float], share: float) -> float | None:
    """Перцентиль методом ближайшего ранга.

    Медиана, а не среднее: одна зависшая на год сделка сдвигает среднее так,
    что им нельзя пользоваться. В SQLite перцентилей нет, считаем в Python.
    """
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    index = max(0, min(len(data) - 1, int(round(share * (len(data) - 1)))))
    return float(data[index])


def _share(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _delta(current: float, previous: float) -> float | None:
    """Изменение в процентах. None, когда сравнивать не с чем."""
    if not previous:
        return None
    return round(100.0 * (current - previous) / previous, 1)


# --------------------------------------------------------------------------
# измерения
# --------------------------------------------------------------------------

def pipelines(conn) -> list[dict[str, Any]]:
    """Список воронок сделок, как они заведены в Bitrix."""
    return _rows(conn, "SELECT category_id, name, sort FROM dim_pipeline ORDER BY sort, name")


def stages(conn, category_id: int) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT stage_id, name, sort, semantic FROM dim_stage "
        "WHERE category_id = :cat ORDER BY sort, name",
        {"cat": category_id},
    )


def users(conn) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT user_id, name, department_id, department_name FROM dim_user ORDER BY name",
    )


def sources(conn) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT source_id, name FROM dim_source ORDER BY name")


# --------------------------------------------------------------------------
# воронка сделок
# --------------------------------------------------------------------------

def deal_funnel(conn, category_id: int, since: str, until: str) -> dict[str, Any]:
    """Воронка сделок: срез и когортная конверсия рядом.

    * ``count_now`` — сколько сделок стоит на стадии сейчас (срез).
    * ``reached`` — сколько сделок КОГОРТЫ (созданных в периоде) когда-либо
      доходили до стадии. Именно это, а не срез, отвечает на вопрос
      «доходят ли сделки до договора».
    * ``conversion_from_start`` — доля когорты, дошедшая до стадии.
    * ``conversion_step`` — конверсия из предыдущей стадии воронки.
    """
    cohort_size = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_deal "
        "WHERE category_id = :cat AND is_deleted = 0 "
        "AND date_create >= :since AND date_create < :until",
        {"cat": category_id, "since": since, "until": until},
    ).get("n", 0)

    rows = _rows(
        conn,
        """
        SELECT s.stage_id, s.name, s.sort, s.semantic,
          (SELECT COUNT(*) FROM fact_deal d
            WHERE d.category_id = s.category_id AND d.is_deleted = 0
              AND d.stage_id = s.stage_id) AS count_now,
          (SELECT COALESCE(SUM(d.opportunity), 0) FROM fact_deal d
            WHERE d.category_id = s.category_id AND d.is_deleted = 0
              AND d.stage_id = s.stage_id AND d.is_closed = 0) AS amount_open,
          (SELECT COUNT(DISTINCT e.entity_id)
             FROM fact_stage_event e
             JOIN fact_deal d ON d.deal_id = e.entity_id
            WHERE e.entity_type = 'deal' AND e.stage_id = s.stage_id
              AND d.category_id = s.category_id AND d.is_deleted = 0
              AND d.date_create >= :since AND d.date_create < :until) AS reached
        FROM dim_stage s
        WHERE s.category_id = :cat
        ORDER BY s.sort, s.name
        """,
        {"cat": category_id, "since": since, "until": until},
    )

    previous_reached: int | None = None
    for row in rows:
        row["conversion_from_start"] = _share(row["reached"], cohort_size)
        # Шаговая конверсия осмысленна только внутри цепочки «в работе».
        # Проиграть сделку можно с любой стадии, поэтому «доля от предыдущей»
        # для проигрыша — не конверсия, а бессмыслица: делённое на последнюю
        # рабочую стадию, оно легко даёт 400%.
        if row["semantic"] == "in_progress":
            row["conversion_step"] = (
                _share(row["reached"], previous_reached) if previous_reached else None
            )
            previous_reached = row["reached"]
        elif row["semantic"] == "won":
            # Выигрыш — законный конец цепочки: доля дошедших до последней
            # рабочей стадии, которые её закрыли.
            row["conversion_step"] = (
                _share(row["reached"], previous_reached) if previous_reached else None
            )
        else:
            row["conversion_step"] = None

    return {"cohort_size": cohort_size, "stages": rows}


def win_rate(conn, category_id: int, since: str, until: str) -> dict[str, Any]:
    """Доля выигранных среди закрытых за период (по дате закрытия).

    Знаменатель — только закрытые сделки. Считать от всех, включая открытые,
    значит занижать конверсию тем сильнее, чем больше сделок в работе.
    """
    row = _one(
        conn,
        """
        SELECT
            SUM(is_won) AS won,
            SUM(is_lost) AS lost,
            COALESCE(SUM(CASE WHEN is_won = 1 THEN opportunity ELSE 0 END), 0) AS won_amount
        FROM fact_deal
        WHERE is_deleted = 0 AND is_closed = 1
          AND closedate >= :since AND closedate < :until
          AND (:cat IS NULL OR category_id = :cat)
        """,
        {"cat": category_id, "since": since, "until": until},
    )
    won = int(row.get("won") or 0)
    lost = int(row.get("lost") or 0)
    closed = won + lost
    return {
        "won": won, "lost": lost, "closed": closed,
        "win_rate": _share(won, closed),
        "won_amount": float(row.get("won_amount") or 0),
        "avg_check": round(float(row.get("won_amount") or 0) / won, 2) if won else 0.0,
    }


def deal_cycle_days(conn, category_id: int | None, since: str, until: str) -> dict[str, Any]:
    """Цикл сделки: от создания до закрытия, по закрытым за период.

    Медиана и p75, а не среднее — см. percentile().
    """
    values = [
        row["days"] for row in _rows(
            conn,
            """
            SELECT (julianday(closedate) - julianday(date_create)) AS days
            FROM fact_deal
            WHERE is_deleted = 0 AND is_closed = 1 AND closedate IS NOT NULL
              AND closedate >= :since AND closedate < :until
              AND (:cat IS NULL OR category_id = :cat)
            """,
            {"cat": category_id, "since": since, "until": until},
        ) if row["days"] is not None and row["days"] >= 0
    ]
    return {
        "median": round(percentile(values, 0.5) or 0, 1),
        "p75": round(percentile(values, 0.75) or 0, 1),
        "count": len(values),
    }


# --------------------------------------------------------------------------
# движение
# --------------------------------------------------------------------------

def stage_movement(conn, category_id: int, since: str, until: str) -> list[dict[str, Any]]:
    """Движение за период: вошло / вышло / осталось на конец.

    Полноценный учёт потока, а не разность срезов: сделка, зашедшая и вышедшая
    внутри периода, в разности срезов не видна вовсе, хотя работа по ней шла.
    """
    return _rows(
        conn,
        """
        SELECT s.stage_id, s.name, s.sort, s.semantic,
          (SELECT COUNT(*) FROM fact_stage_event e
            WHERE e.entity_type = 'deal' AND e.stage_id = s.stage_id
              AND e.category_id = s.category_id
              AND e.entered_at >= :since AND e.entered_at < :until) AS entered,
          (SELECT COUNT(*) FROM fact_stage_event e
            WHERE e.entity_type = 'deal' AND e.stage_id = s.stage_id
              AND e.category_id = s.category_id
              AND e.left_at IS NOT NULL
              AND e.left_at >= :since AND e.left_at < :until) AS left_count,
          (SELECT COUNT(*) FROM fact_stage_event e
            WHERE e.entity_type = 'deal' AND e.stage_id = s.stage_id
              AND e.category_id = s.category_id
              AND e.entered_at < :until
              AND (e.left_at IS NULL OR e.left_at >= :until)) AS remaining
        FROM dim_stage s
        WHERE s.category_id = :cat
        ORDER BY s.sort, s.name
        """,
        {"cat": category_id, "since": since, "until": until},
    )


def stage_transitions(conn, category_id: int, since: str, until: str) -> dict[str, Any]:
    """Переходы между стадиями за период — данные для диаграммы потоков.

    ``backwards`` — переходы назад по порядку стадий. Это не мелочь: возврат
    сделки на предыдущий шаг обычно означает, что квалификация на входе была
    неверной, и такие случаи стоит смотреть поимённо.
    """
    rows = _rows(
        conn,
        """
        SELECT e1.stage_id AS from_stage, e2.stage_id AS to_stage,
               COUNT(*) AS moves,
               COALESCE(sf.name, e1.stage_id) AS from_name,
               COALESCE(st.name, e2.stage_id) AS to_name,
               COALESCE(sf.sort, 0) AS from_sort,
               COALESCE(st.sort, 0) AS to_sort
        FROM fact_stage_event e1
        JOIN fact_stage_event e2
          ON e2.entity_type = e1.entity_type AND e2.entity_id = e1.entity_id
         AND e2.seq = e1.seq + 1
        LEFT JOIN dim_stage sf ON sf.stage_id = e1.stage_id AND sf.category_id = :cat
        LEFT JOIN dim_stage st ON st.stage_id = e2.stage_id AND st.category_id = :cat
        WHERE e1.entity_type = 'deal' AND e1.category_id = :cat
          AND e2.entered_at >= :since AND e2.entered_at < :until
        GROUP BY e1.stage_id, e2.stage_id
        ORDER BY moves DESC
        """,
        {"cat": category_id, "since": since, "until": until},
    )
    backwards = [r for r in rows if r["to_sort"] < r["from_sort"]]
    return {
        "transitions": rows,
        "backwards": backwards,
        "backwards_total": sum(r["moves"] for r in backwards),
    }


def stage_durations(conn, category_id: int, since: str, until: str) -> list[dict[str, Any]]:
    """Сколько времени сделки проводят на каждой стадии.

    ``median_days`` / ``p75_days`` — по интервалам, ЗАВЕРШЁННЫМ в периоде.
    ``open_median_days`` — текущий возраст тех, кто стоит на стадии сейчас;
    считается на момент запроса, потому что записанное значение протухло бы
    к следующему просмотру.
    """
    completed: dict[str, list[float]] = {}
    for row in _rows(
        conn,
        """
        SELECT stage_id, duration_sec / 86400.0 AS days
        FROM fact_stage_event
        WHERE entity_type = 'deal' AND category_id = :cat
          AND duration_sec IS NOT NULL
          AND left_at >= :since AND left_at < :until
        """,
        {"cat": category_id, "since": since, "until": until},
    ):
        completed.setdefault(row["stage_id"], []).append(row["days"])

    open_ages: dict[str, list[float]] = {}
    for row in _rows(
        conn,
        """
        SELECT stage_id, (julianday('now') - julianday(entered_at)) AS days
        FROM fact_stage_event
        WHERE entity_type = 'deal' AND category_id = :cat AND left_at IS NULL
        """,
        {"cat": category_id},
    ):
        if row["days"] is not None and row["days"] >= 0:
            open_ages.setdefault(row["stage_id"], []).append(row["days"])

    out = []
    for stage in stages(conn, category_id):
        done = completed.get(stage["stage_id"], [])
        live = open_ages.get(stage["stage_id"], [])
        out.append({
            **stage,
            "median_days": round(percentile(done, 0.5) or 0, 1),
            "p75_days": round(percentile(done, 0.75) or 0, 1),
            "completed_count": len(done),
            "open_count": len(live),
            "open_median_days": round(percentile(live, 0.5) or 0, 1),
        })
    return out


def stuck_deals(conn, category_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Сделки, стоящие на стадии дольше, чем p75 этой же стадии.

    Порог берётся из собственных данных воронки, а не из выдуманного числа
    дней: у «Подбора» и «Офера» нормальный срок разный, и единый порог либо
    завалит список шумом, либо пропустит реальные простои.
    """
    thresholds = {
        row["stage_id"]: row["p75_days"] or 0
        for row in stage_durations(conn, category_id, "0000", "9999")
    }
    rows = _rows(
        conn,
        """
        SELECT d.deal_id, d.title, d.stage_id, d.opportunity, d.assigned_by_id,
               COALESCE(s.name, d.stage_id) AS stage_name,
               COALESCE(u.name, '') AS assignee,
               COALESCE(u.department_name, '') AS department,
               (julianday('now') - julianday(e.entered_at)) AS days_in_stage
        FROM fact_deal d
        JOIN fact_stage_event e
          ON e.entity_type = 'deal' AND e.entity_id = d.deal_id AND e.left_at IS NULL
        LEFT JOIN dim_stage s ON s.stage_id = d.stage_id AND s.category_id = d.category_id
        LEFT JOIN dim_user u ON u.user_id = d.assigned_by_id
        WHERE d.category_id = :cat AND d.is_deleted = 0 AND d.is_closed = 0
        ORDER BY days_in_stage DESC
        """,
        {"cat": category_id},
    )
    stuck = [
        row for row in rows
        if row["days_in_stage"] is not None
        and thresholds.get(row["stage_id"], 0) > 0
        and row["days_in_stage"] > thresholds[row["stage_id"]]
    ]
    for row in stuck:
        row["days_in_stage"] = round(row["days_in_stage"], 1)
        row["threshold_days"] = round(thresholds.get(row["stage_id"], 0), 1)
    return stuck[:limit]


# --------------------------------------------------------------------------
# лиды
# --------------------------------------------------------------------------

def lead_funnel(conn, since: str, until: str) -> dict[str, Any]:
    """Лиды когорты по статусам + конверсия в сделку.

    ``converted`` считается по факту существования сделки с этим LEAD_ID, а не
    по одному лишь статусу «Квалифицирован»: статус проставляется руками и
    расходится с реальностью.
    """
    total = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_lead WHERE is_deleted = 0 "
        "AND date_create >= :since AND date_create < :until",
        {"since": since, "until": until},
    ).get("n", 0)

    by_status = _rows(
        conn,
        """
        SELECT l.status_id, COALESCE(s.name, l.status_id) AS name,
               COALESCE(s.sort, 999) AS sort, COALESCE(s.semantic, 'in_progress') AS semantic,
               COUNT(*) AS count
        FROM fact_lead l
        LEFT JOIN dim_lead_status s ON s.status_id = l.status_id
        WHERE l.is_deleted = 0 AND l.date_create >= :since AND l.date_create < :until
        GROUP BY l.status_id
        ORDER BY sort, name
        """,
        {"since": since, "until": until},
    )
    for row in by_status:
        row["share"] = _share(row["count"], total)

    converted = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_lead WHERE is_deleted = 0 "
        "AND converted_deal_id IS NOT NULL "
        "AND date_create >= :since AND date_create < :until",
        {"since": since, "until": until},
    ).get("n", 0)

    return {
        "total": total,
        "by_status": by_status,
        "converted": converted,
        "conversion": _share(converted, total),
    }


def lead_sources(conn, since: str, until: str) -> list[dict[str, Any]]:
    """Когортная конверсия по источникам — основа решений о каналах.

    Считается только по лидам когорты (созданным в периоде), чтобы источники
    сравнивались на одинаковом сроке дозревания.
    """
    rows = _rows(
        conn,
        """
        SELECT l.source_id, COALESCE(s.name, NULLIF(l.source_id, ''), 'Не указан') AS name,
               COUNT(*) AS leads,
               SUM(CASE WHEN l.converted_deal_id IS NOT NULL THEN 1 ELSE 0 END) AS converted,
               SUM(CASE WHEN l.status_id IN ('JUNK', 'UC_A7I8DK') THEN 1 ELSE 0 END) AS junk,
               COALESCE(SUM(CASE WHEN d.is_won = 1 THEN d.opportunity ELSE 0 END), 0) AS won_amount,
               SUM(CASE WHEN d.is_won = 1 THEN 1 ELSE 0 END) AS won_deals
        FROM fact_lead l
        LEFT JOIN dim_source s ON s.source_id = l.source_id
        LEFT JOIN fact_deal d ON d.deal_id = l.converted_deal_id AND d.is_deleted = 0
        WHERE l.is_deleted = 0 AND l.date_create >= :since AND l.date_create < :until
        GROUP BY l.source_id
        ORDER BY leads DESC
        """,
        {"since": since, "until": until},
    )
    total_leads = sum(row["leads"] for row in rows)
    total_junk = sum(row["junk"] or 0 for row in rows)
    average_junk = _share(total_junk, total_leads)

    for row in rows:
        row["conversion"] = _share(row["converted"], row["leads"])
        row["junk_share"] = _share(row["junk"], row["leads"])
        row["amount_per_lead"] = round(row["won_amount"] / row["leads"], 0) if row["leads"] else 0
        # Порог — средняя доля мусора по всем источникам, а не выдуманное
        # число: «плохо» здесь значит «хуже остальных каналов», и по такому
        # сравнению уже можно принимать решение о канале.
        row["junk_above_average"] = row["junk_share"] > average_junk
    return rows


def lead_first_move_days(conn, since: str, until: str) -> dict[str, Any]:
    """Сколько лид лежит до первой смены статуса.

    Прокси скорости первого касания: считается по истории стадий лидов и
    доступен только если портал её ведёт (см. etl._lead_history_supported).
    ``supported=False`` означает «не измеряем», а не «ноль».
    """
    supported = _one(
        conn, "SELECT value FROM analytics_meta WHERE key = 'lead_history_supported'",
    ).get("value") == "1"
    if not supported:
        return {"supported": False, "median": None, "p90": None, "count": 0}

    values = [
        row["days"] for row in _rows(
            conn,
            """
            SELECT MIN(e.duration_sec) / 86400.0 AS days
            FROM fact_stage_event e
            JOIN fact_lead l ON l.lead_id = e.entity_id
            WHERE e.entity_type = 'lead' AND e.seq = 0 AND e.duration_sec IS NOT NULL
              AND l.is_deleted = 0
              AND l.date_create >= :since AND l.date_create < :until
            GROUP BY e.entity_id
            """,
            {"since": since, "until": until},
        ) if row["days"] is not None
    ]
    return {
        "supported": True,
        "median": round(percentile(values, 0.5) or 0, 2),
        "p90": round(percentile(values, 0.9) or 0, 2),
        "count": len(values),
    }


# --------------------------------------------------------------------------
# деньги
# --------------------------------------------------------------------------

def money(conn, category_id: int | None, since: str, until: str) -> dict[str, Any]:
    """Деньги + ПОКРЫТИЕ поля суммы.

    Покрытие обязательно рядом с каждой суммой. Поле «Комиссия» заполнено не
    везде — в проекте есть отдельная задача, которая ежечасно напоминает
    брокерам его заполнить. «12,4 млн ₽» без приписки «заполнено у 68% сделок»
    вводит в заблуждение ровно там, где решается вопрос о деньгах.
    """
    open_row = _one(
        conn,
        """
        SELECT COUNT(*) AS deals,
               SUM(CASE WHEN opportunity > 0 THEN 1 ELSE 0 END) AS filled,
               COALESCE(SUM(opportunity), 0) AS amount
        FROM fact_deal
        WHERE is_deleted = 0 AND is_closed = 0
          AND (:cat IS NULL OR category_id = :cat)
        """,
        {"cat": category_id},
    )
    won = win_rate(conn, category_id, since, until)
    won_coverage = _one(
        conn,
        """
        SELECT COUNT(*) AS deals,
               SUM(CASE WHEN opportunity > 0 THEN 1 ELSE 0 END) AS filled
        FROM fact_deal
        WHERE is_deleted = 0 AND is_won = 1
          AND closedate >= :since AND closedate < :until
          AND (:cat IS NULL OR category_id = :cat)
        """,
        {"cat": category_id, "since": since, "until": until},
    )
    return {
        "open_amount": float(open_row.get("amount") or 0),
        "open_deals": int(open_row.get("deals") or 0),
        "open_filled": int(open_row.get("filled") or 0),
        "open_coverage": _share(open_row.get("filled") or 0, open_row.get("deals") or 0),
        "won_amount": won["won_amount"],
        "won_deals": won["won"],
        "won_filled": int(won_coverage.get("filled") or 0),
        "won_coverage": _share(won_coverage.get("filled") or 0, won_coverage.get("deals") or 0),
        "avg_check": won["avg_check"],
    }


def stage_win_probability(conn, category_id: int) -> dict[str, float]:
    """Историческая вероятность выигрыша по стадии, достигнутой сделкой.

    Берётся из собственных закрытых сделок за всё окно витрины, а не из
    процентов, проставленных в карточке воронки руками: проценты в Bitrix
    ставятся один раз при настройке и почти никогда не пересматриваются.
    """
    rows = _rows(
        conn,
        """
        SELECT e.stage_id,
               COUNT(DISTINCT CASE WHEN d.is_closed = 1 THEN d.deal_id END) AS closed,
               COUNT(DISTINCT CASE WHEN d.is_won = 1 THEN d.deal_id END) AS won
        FROM fact_stage_event e
        JOIN fact_deal d ON d.deal_id = e.entity_id AND d.is_deleted = 0
        WHERE e.entity_type = 'deal' AND e.category_id = :cat
        GROUP BY e.stage_id
        """,
        {"cat": category_id},
    )
    return {
        row["stage_id"]: (row["won"] / row["closed"]) if row["closed"] else 0.0
        for row in rows
    }


def weighted_forecast(conn, category_id: int) -> dict[str, Any]:
    """Взвешенный прогноз: сумма открытых сделок × вероятность их стадии.

    ``coverage`` показывает, какая доля открытых сделок вообще имеет сумму —
    прогноз по половине заполненных сделок это половина прогноза.
    """
    probabilities = stage_win_probability(conn, category_id)
    rows = _rows(
        conn,
        """
        SELECT stage_id, COUNT(*) AS deals,
               COALESCE(SUM(opportunity), 0) AS amount,
               SUM(CASE WHEN opportunity > 0 THEN 1 ELSE 0 END) AS filled
        FROM fact_deal
        WHERE category_id = :cat AND is_deleted = 0 AND is_closed = 0
        GROUP BY stage_id
        """,
        {"cat": category_id},
    )
    stage_names = {s["stage_id"]: s["name"] for s in stages(conn, category_id)}
    total, deals, filled = 0.0, 0, 0
    detail = []
    for row in rows:
        probability = probabilities.get(row["stage_id"], 0.0)
        expected = row["amount"] * probability
        total += expected
        deals += row["deals"]
        filled += row["filled"] or 0
        detail.append({
            "stage_id": row["stage_id"],
            "name": stage_names.get(row["stage_id"], row["stage_id"]),
            "deals": row["deals"], "amount": row["amount"],
            "probability": round(100 * probability, 1),
            "expected": round(expected, 0),
        })
    detail.sort(key=lambda r: r["expected"], reverse=True)
    return {
        "expected": round(total, 0),
        "open_deals": deals,
        "coverage": _share(filled, deals),
        "by_stage": detail,
    }


# --------------------------------------------------------------------------
# люди и динамика
# --------------------------------------------------------------------------

def people(conn, since: str, until: str, category_id: int | None = None) -> list[dict[str, Any]]:
    """Срез по ответственным: нагрузка, конверсия, выигранные деньги."""
    rows = _rows(
        conn,
        """
        SELECT d.assigned_by_id AS user_id,
               COALESCE(u.name, 'ID ' || d.assigned_by_id) AS name,
               COALESCE(u.department_name, '') AS department,
               COUNT(*) AS deals_created,
               SUM(CASE WHEN d.is_closed = 0 THEN 1 ELSE 0 END) AS deals_open,
               SUM(d.is_won) AS won,
               SUM(d.is_lost) AS lost,
               COALESCE(SUM(CASE WHEN d.is_won = 1 THEN d.opportunity ELSE 0 END), 0) AS won_amount
        FROM fact_deal d
        LEFT JOIN dim_user u ON u.user_id = d.assigned_by_id
        WHERE d.is_deleted = 0
          AND d.date_create >= :since AND d.date_create < :until
          AND (:cat IS NULL OR d.category_id = :cat)
        GROUP BY d.assigned_by_id
        ORDER BY won_amount DESC, deals_created DESC
        """,
        {"since": since, "until": until, "cat": category_id},
    )
    for row in rows:
        closed = (row["won"] or 0) + (row["lost"] or 0)
        row["win_rate"] = _share(row["won"] or 0, closed)
    return rows


def departments(conn, since: str, until: str, category_id: int | None = None):
    """Те же цифры, свёрнутые по отделам."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in people(conn, since, until, category_id):
        key = row["department"] or "Без отдела"
        bucket = grouped.setdefault(key, {
            "department": key, "people": 0, "deals_created": 0,
            "deals_open": 0, "won": 0, "lost": 0, "won_amount": 0.0,
        })
        bucket["people"] += 1
        for field in ("deals_created", "deals_open", "won", "lost", "won_amount"):
            bucket[field] += row[field] or 0
    out = list(grouped.values())
    for row in out:
        row["win_rate"] = _share(row["won"], row["won"] + row["lost"])
    out.sort(key=lambda r: r["won_amount"], reverse=True)
    return out


_GRAIN_SQL = {
    "day": "substr({col}, 1, 10)",
    "week": "strftime('%Y-W%W', {col})",
    "month": "substr({col}, 1, 7)",
}


def timeseries(
    conn,
    since: str,
    until: str,
    category_id: int | None = None,
    grain: str = "day",
) -> list[dict[str, Any]]:
    """Динамика: созданные лиды и сделки, выигранные сделки и суммы по периодам."""
    grain = grain if grain in _GRAIN_SQL else "day"
    bucket_created = _GRAIN_SQL[grain].format(col="date_create")
    bucket_closed = _GRAIN_SQL[grain].format(col="closedate")

    leads = {
        row["bucket"]: row["n"] for row in _rows(
            conn,
            f"SELECT {bucket_created} AS bucket, COUNT(*) AS n FROM fact_lead "
            "WHERE is_deleted = 0 AND date_create >= :since AND date_create < :until "
            "GROUP BY bucket",
            {"since": since, "until": until},
        )
    }
    deals = {
        row["bucket"]: row for row in _rows(
            conn,
            f"SELECT {bucket_created} AS bucket, COUNT(*) AS n FROM fact_deal "
            "WHERE is_deleted = 0 AND date_create >= :since AND date_create < :until "
            "AND (:cat IS NULL OR category_id = :cat) GROUP BY bucket",
            {"since": since, "until": until, "cat": category_id},
        )
    }
    won = {
        row["bucket"]: row for row in _rows(
            conn,
            f"SELECT {bucket_closed} AS bucket, COUNT(*) AS n, "
            "COALESCE(SUM(opportunity), 0) AS amount FROM fact_deal "
            "WHERE is_deleted = 0 AND is_won = 1 AND closedate IS NOT NULL "
            "AND closedate >= :since AND closedate < :until "
            "AND (:cat IS NULL OR category_id = :cat) GROUP BY bucket",
            {"since": since, "until": until, "cat": category_id},
        )
    }

    buckets = sorted(set(leads) | set(deals) | set(won))
    return [{
        "bucket": bucket,
        "leads": leads.get(bucket, 0),
        "deals": (deals.get(bucket) or {}).get("n", 0),
        "won": (won.get(bucket) or {}).get("n", 0),
        "won_amount": (won.get(bucket) or {}).get("amount", 0),
    } for bucket in buckets]


# --------------------------------------------------------------------------
# сводка
# --------------------------------------------------------------------------

def overview(conn, period: dict[str, str], category_id: int | None = None) -> dict[str, Any]:
    """KPI-строка обзорной страницы со сравнением к прошлому периоду той же длины."""
    since, until = period["since"], period["until"]
    prev = previous_period(period)

    def snapshot(a: str, b: str) -> dict[str, Any]:
        leads = lead_funnel(conn, a, b)
        deals_created = _one(
            conn,
            "SELECT COUNT(*) AS n FROM fact_deal WHERE is_deleted = 0 "
            "AND date_create >= :since AND date_create < :until "
            "AND (:cat IS NULL OR category_id = :cat)",
            {"since": a, "until": b, "cat": category_id},
        ).get("n", 0)
        wins = win_rate(conn, category_id, a, b)
        return {
            "leads": leads["total"],
            "lead_conversion": leads["conversion"],
            "deals_created": deals_created,
            "won": wins["won"],
            "win_rate": wins["win_rate"],
            "won_amount": wins["won_amount"],
            "avg_check": wins["avg_check"],
        }

    current = snapshot(since, until)
    previous = snapshot(prev["since"], prev["until"])
    cycle = deal_cycle_days(conn, category_id, since, until)
    cash = money(conn, category_id, since, until)

    return {
        "current": current,
        "previous": previous,
        "delta": {key: _delta(current[key], previous[key]) for key in current},
        "cycle": cycle,
        "money": cash,
    }


# --------------------------------------------------------------------------
# разложение до карточек
# --------------------------------------------------------------------------

_TABLE_SORTS = {
    "deal": {
        "id": "d.deal_id", "title": "d.title", "stage": "s.sort",
        "amount": "d.opportunity", "created": "d.date_create",
        "modified": "d.date_modify", "assignee": "u.name",
        "days_in_stage": "days_in_stage",
    },
    "lead": {
        "id": "l.lead_id", "title": "l.title", "stage": "st.sort",
        "amount": "l.opportunity", "created": "l.date_create",
        "modified": "l.date_modify", "assignee": "u.name",
        "days_in_stage": "days_in_stage",
    },
}
MAX_PAGE_SIZE = 500


def entity_table(
    conn,
    *,
    entity: str = "deal",
    category_id: int | None = None,
    stage_id: str | None = None,
    assigned_by_id: int | None = None,
    source_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    query: str | None = None,
    only_open: bool = False,
    sort: str = "created",
    direction: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Строки, из которых сложились цифры дашборда.

    Сортировка выбирается из белого списка колонок, а не подставляется из
    запроса: имя колонки нельзя параметризовать, и приём пользовательской
    строки прямо в ORDER BY — это SQL-инъекция.
    """
    entity = entity if entity in ("deal", "lead") else "deal"
    sort_column = _TABLE_SORTS[entity].get(sort, _TABLE_SORTS[entity]["created"])
    direction_sql = "ASC" if str(direction).lower() == "asc" else "DESC"
    page = max(1, int(page))
    page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))

    conditions: list[str] = []
    params: dict[str, Any] = {}
    if entity == "deal":
        base = """
        FROM fact_deal d
        LEFT JOIN dim_stage s ON s.stage_id = d.stage_id AND s.category_id = d.category_id
        LEFT JOIN dim_user u ON u.user_id = d.assigned_by_id
        LEFT JOIN dim_source src ON src.source_id = d.source_id
        LEFT JOIN fact_stage_event e
               ON e.entity_type = 'deal' AND e.entity_id = d.deal_id AND e.left_at IS NULL
        WHERE d.is_deleted = 0
        """
        select = """
        SELECT d.deal_id AS id, d.title, d.stage_id, d.category_id,
               COALESCE(s.name, d.stage_id) AS stage_name,
               COALESCE(p.name, '') AS pipeline_name,
               d.opportunity AS amount, d.currency_id,
               d.assigned_by_id, COALESCE(u.name, '') AS assignee,
               COALESCE(u.department_name, '') AS department,
               COALESCE(src.name, NULLIF(d.source_id, ''), '') AS source_name,
               d.date_create AS created, d.date_modify AS modified,
               d.closedate, d.is_closed, d.is_won, d.is_lost,
               (julianday('now') - julianday(e.entered_at)) AS days_in_stage
        """
        base = base.replace(
            "LEFT JOIN dim_source src",
            "LEFT JOIN dim_pipeline p ON p.category_id = d.category_id\n"
            "        LEFT JOIN dim_source src",
        )
        if category_id is not None:
            conditions.append("d.category_id = :cat")
            params["cat"] = category_id
        if stage_id:
            conditions.append("d.stage_id = :stage")
            params["stage"] = stage_id
        if assigned_by_id:
            conditions.append("d.assigned_by_id = :assignee")
            params["assignee"] = assigned_by_id
        if source_id:
            conditions.append("d.source_id = :source")
            params["source"] = source_id
        if since:
            conditions.append("d.date_create >= :since")
            params["since"] = since
        if until:
            conditions.append("d.date_create < :until")
            params["until"] = until
        if only_open:
            conditions.append("d.is_closed = 0")
        if query:
            conditions.append("(d.title LIKE :q OR CAST(d.deal_id AS TEXT) LIKE :q)")
            params["q"] = f"%{query}%"
    else:
        base = """
        FROM fact_lead l
        LEFT JOIN dim_lead_status st ON st.status_id = l.status_id
        LEFT JOIN dim_user u ON u.user_id = l.assigned_by_id
        LEFT JOIN dim_source src ON src.source_id = l.source_id
        LEFT JOIN fact_stage_event e
               ON e.entity_type = 'lead' AND e.entity_id = l.lead_id AND e.left_at IS NULL
        WHERE l.is_deleted = 0
        """
        select = """
        SELECT l.lead_id AS id, l.title, l.status_id AS stage_id, 0 AS category_id,
               COALESCE(st.name, l.status_id) AS stage_name,
               'Лиды' AS pipeline_name,
               l.opportunity AS amount, l.currency_id,
               l.assigned_by_id, COALESCE(u.name, '') AS assignee,
               COALESCE(u.department_name, '') AS department,
               COALESCE(src.name, NULLIF(l.source_id, ''), '') AS source_name,
               l.date_create AS created, l.date_modify AS modified,
               l.date_closed AS closedate, 0 AS is_closed,
               l.is_converted AS is_won, 0 AS is_lost,
               l.converted_deal_id,
               (julianday('now') - julianday(e.entered_at)) AS days_in_stage
        """
        if stage_id:
            conditions.append("l.status_id = :stage")
            params["stage"] = stage_id
        if assigned_by_id:
            conditions.append("l.assigned_by_id = :assignee")
            params["assignee"] = assigned_by_id
        if source_id:
            conditions.append("l.source_id = :source")
            params["source"] = source_id
        if since:
            conditions.append("l.date_create >= :since")
            params["since"] = since
        if until:
            conditions.append("l.date_create < :until")
            params["until"] = until
        if query:
            conditions.append("(l.title LIKE :q OR CAST(l.lead_id AS TEXT) LIKE :q)")
            params["q"] = f"%{query}%"

    where = base + ("".join(f" AND {c}" for c in conditions))
    total = _one(conn, f"SELECT COUNT(*) AS n {where}", params).get("n", 0)
    rows = _rows(
        conn,
        f"{select} {where} ORDER BY {sort_column} {direction_sql} "
        "LIMIT :limit OFFSET :offset",
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    )
    for row in rows:
        days = row.get("days_in_stage")
        if days is None:
            continue
        # Отрицательное время на стадии означает дату входа из будущего — это
        # аномалия данных, а не «−482 часа». В таблице показываем ноль, а сам
        # факт выносим на страницу качества, где ему и место.
        row["days_in_stage"] = round(max(0.0, days), 1)
    return {
        "rows": rows, "total": total, "page": page, "page_size": page_size,
        "pages": max(1, -(-total // page_size)), "entity": entity,
    }


# --------------------------------------------------------------------------
# качество данных и состояние ETL
# --------------------------------------------------------------------------

def data_quality(conn, category_id: int | None = None) -> dict[str, Any]:
    """Насколько данным вообще можно верить.

    Если у 40% сделок пуста сумма, «сумма в работе» — не цифра, а половина
    цифры. Эта страница ставит границу доверия к остальным.
    """
    deals = _one(
        conn,
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN opportunity <= 0 THEN 1 ELSE 0 END) AS no_amount,
               SUM(CASE WHEN source_id = '' THEN 1 ELSE 0 END) AS no_source,
               SUM(CASE WHEN assigned_by_id IS NULL THEN 1 ELSE 0 END) AS no_assignee,
               SUM(CASE WHEN is_closed = 1 AND closedate IS NULL THEN 1 ELSE 0 END)
                   AS closed_no_date,
               SUM(CASE WHEN is_won = 1 AND opportunity <= 0 THEN 1 ELSE 0 END)
                   AS won_no_amount
        FROM fact_deal
        WHERE is_deleted = 0 AND (:cat IS NULL OR category_id = :cat)
        """,
        {"cat": category_id},
    )
    leads = _one(
        conn,
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN source_id = '' THEN 1 ELSE 0 END) AS no_source,
               SUM(CASE WHEN assigned_by_id IS NULL THEN 1 ELSE 0 END) AS no_assignee
        FROM fact_lead WHERE is_deleted = 0
        """,
    )
    orphan_stages = _one(
        conn,
        """
        SELECT COUNT(*) AS n FROM fact_deal d
        WHERE d.is_deleted = 0 AND NOT EXISTS (
            SELECT 1 FROM dim_stage s
            WHERE s.stage_id = d.stage_id AND s.category_id = d.category_id)
        """,
    ).get("n", 0)
    # Дата входа в стадию из будущего — почти всегда сбитые часы на портале
    # или ошибка приведения таймзоны. Молча обрезать её нельзя: она портит
    # время на стадии и списки зависших карточек.
    future_stages = _one(
        conn,
        "SELECT COUNT(*) AS n FROM fact_stage_event WHERE entered_at > :now",
        {"now": datetime.now(timezone.utc).isoformat()},
    ).get("n", 0)

    total_deals = int(deals.get("total") or 0)
    total_leads = int(leads.get("total") or 0)
    return {
        "deals": {
            **{k: int(v or 0) for k, v in deals.items()},
            "amount_coverage": _share(total_deals - int(deals.get("no_amount") or 0), total_deals),
            "source_coverage": _share(total_deals - int(deals.get("no_source") or 0), total_deals),
        },
        "leads": {
            **{k: int(v or 0) for k, v in leads.items()},
            "source_coverage": _share(total_leads - int(leads.get("no_source") or 0), total_leads),
        },
        "orphan_stage_deals": orphan_stages,
        "future_stage_events": future_stages,
    }


def etl_status(conn) -> dict[str, Any]:
    """Свежесть витрины. Устаревшие данные опаснее отсутствующих: они выглядят живыми."""
    runs = _rows(
        conn,
        "SELECT kind, entity, started_at, finished_at, status, rows_upserted, error "
        "FROM etl_run ORDER BY id DESC LIMIT 20",
    )
    last_ok = next((r for r in runs if r["status"] == "ok"), None)
    lag_minutes = None
    if last_ok and last_ok["finished_at"]:
        try:
            finished = datetime.fromisoformat(last_ok["finished_at"])
            lag_minutes = round(
                (datetime.now(timezone.utc) - finished).total_seconds() / 60, 1,
            )
        except ValueError:
            lag_minutes = None
    window = _one(
        conn, "SELECT value FROM analytics_meta WHERE key = 'window_since'",
    ).get("value")
    return {
        "runs": runs,
        "last_ok": last_ok,
        "lag_minutes": lag_minutes,
        "window_since": window,
        "counts": _one(
            conn,
            "SELECT (SELECT COUNT(*) FROM fact_deal WHERE is_deleted = 0) AS deals, "
            "(SELECT COUNT(*) FROM fact_lead WHERE is_deleted = 0) AS leads, "
            "(SELECT COUNT(*) FROM fact_stage_event) AS stage_events",
        ),
    }


def counts_by_pipeline(conn) -> list[dict[str, Any]]:
    """Сколько сделок в каждой воронке — для проверки схождения с Bitrix."""
    return _rows(
        conn,
        """
        SELECT p.category_id, p.name,
               COUNT(d.deal_id) AS deals,
               SUM(CASE WHEN d.is_closed = 0 THEN 1 ELSE 0 END) AS open_deals,
               COALESCE(SUM(CASE WHEN d.is_closed = 0 THEN d.opportunity ELSE 0 END), 0)
                   AS open_amount
        FROM dim_pipeline p
        LEFT JOIN fact_deal d ON d.category_id = p.category_id AND d.is_deleted = 0
        GROUP BY p.category_id
        ORDER BY p.sort, p.name
        """,
    )
