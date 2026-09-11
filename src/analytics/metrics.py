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
считаются по v_stage_event («достигал стадии»), а не по текущей STAGE_ID.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

ENTITY_DEAL = "deal"
ENTITY_LEAD = "lead"

# Пресеты периода. Значение — сколько дней назад начинается период.
PERIOD_PRESETS: dict[str, str] = {
    # «Вчера» — прошлый РАБОЧИЙ день, тот же, которым ведётся утренняя
    # рассылка и «План на день». Второе определение вчерашнего дня на
    # экране означало бы, что сводка и дашборд спорят о том, что случилось.
    # В понедельник окно накрывает выходные целиком, и подпись говорит об
    # этом датами, а не молчит.
    "yesterday": "Вчера",
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

# Витрина хранит время в UTC, а бизнес живёт по Москве. Границы периодов
# обязаны совпадать с календарём того, кто смотрит отчёт: пока сутки резались
# по UTC, «Текущий месяц» начинался в 03:00 МСК первого числа, и всё закрытое
# ночью уезжало в соседний период — ровно на стыке месяца, когда и считают
# отчётность. Даты на экране (format.py) переводятся в эту же зону.
BUSINESS_TZ = timezone(timedelta(hours=3))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _day_start(day: date) -> datetime:
    """Полночь этого дня по московскому календарю."""
    return datetime(day.year, day.month, day.day, tzinfo=BUSINESS_TZ)


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
    today = datetime.now(BUSINESS_TZ).date()

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

    if preset == "yesterday":
        window = yesterday_window()
        # Границы окна хранятся в UTC, а поля «с» и «по» — московские
        # календарные дни. Взять из строки первые десять символов значит
        # ошибиться на сутки: в UTC вчерашний день начинается позавчера в
        # 21:00, и форма показала бы дату на день раньше выбранной.
        since_day = datetime.fromisoformat(window["since"]).astimezone(
            BUSINESS_TZ).date()
        until_day = datetime.fromisoformat(window["until"]).astimezone(
            BUSINESS_TZ).date()
        return {
            "since": window["since"], "until": window["until"],
            # Подпись честная: в понедельник это не «вчера», а три дня, и
            # молчать об этом нельзя — по такому числу сверяют выручку.
            "label": ("Вчера" if window["label"] == "вчера"
                      else f"Вчера · {window['label']}"),
            "preset": preset,
            "start": since_day.isoformat(),
            "end": (until_day - timedelta(days=1)).isoformat(),
        }

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

def base_currency() -> str:
    """Валюта денежных метрик. По умолчанию рубль, меняется настройкой."""
    try:
        from config import get_settings

        return (get_settings().analytics_base_currency or "RUB").strip().upper()
    except Exception:  # pragma: no cover — конфиг недоступен в изолированных тестах
        return "RUB"


def _money_of(alias: str = "") -> str:
    """Условие «эта сумма выражена в валюте, которую можно складывать».

    Пустая валюта — это карточка, где поле не заполнено: портал в таких
    случаях подразумевает валюту портала, и выбрасывать их значит потерять
    почти всё. А вот сделку в долларах сложить с рублёвой нельзя: курса у
    витрины нет, и «1 910 000 ₽», где десять тысяч из них доллары, — это
    неверное число, а не приблизительное.
    """
    prefix = f"{alias}." if alias else ""
    return (f"({prefix}currency_id = '' OR {prefix}currency_id IS NULL"
            f" OR upper({prefix}currency_id) = :base)")


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


# Прогоны, по которым судят о свежести витрины. В etl_run пишут и задачи,
# которые витрину из Bitrix не обновляют, — например часовая синхронизация
# планов. Без этого отбора шапка каждой страницы показывала бы свежесть
# последней такой задачи, и остановившийся ETL выглядел бы живым: ровно та
# ошибка, от которой страница «Качество данных» и должна защищать.
MART_RUN_KINDS: tuple[str, ...] = ("incremental", "full", "backfill")
_MART_KIND_PARAMS = {f"kind{i}": kind for i, kind in enumerate(MART_RUN_KINDS)}
_MART_KIND_SQL = ", ".join(f":{name}" for name in _MART_KIND_PARAMS)


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


def yesterday_window(now: datetime | None = None) -> dict[str, Any]:
    """Прошлый РАБОЧИЙ день по московскому календарю.

    В понедельник «вчера» — это пятница: сообщение про воскресенье, в котором
    закономерно ничего не закрыто, обесценивает всю рассылку. Выходные при
    этом не теряются — в понедельник окно накрывает их целиком.

    Живёт здесь, а не в рассылке, потому что тем же окном пользуется
    страница «План на день». Второе определение прошлого рабочего дня
    однажды разошлось бы с первым, и утреннее сообщение спорило бы с
    экраном о том, что случилось.
    """
    now = now or datetime.now(BUSINESS_TZ)
    end = datetime(now.year, now.month, now.day, tzinfo=BUSINESS_TZ)
    start = end - timedelta(days=1)
    while start.weekday() >= 5:
        start -= timedelta(days=1)
    label = (
        "вчера" if (end - start).days == 1
        else f"{start:%d.%m}–{end - timedelta(days=1):%d.%m}"
    )
    return {"since": _utc_iso(start), "until": _utc_iso(end), "label": label}


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


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
        "SELECT user_id, name, department_id, department_name FROM v_user ORDER BY name",
    )


def department_clause(assignee: str) -> str:
    """«Карточка принадлежит выбранному отделу» — одним ответом на весь проект.

    Отдел берётся из user_home, то есть с учётом ростера, а не из карточки
    портала. Иначе фильтр спорит с областью видимости на одном экране:
    «Все отделы» показывают карточку человека, перенесённого ростером, а
    выбранный отдел того же человека — уже нет. Строка не исчезает с шумом,
    она просто перестаёт попадать в выборку, и объяснить это нельзя.

    Вынесено в функцию, а не повторено одиннадцать раз: одиннадцать копий
    условия однажды разойдутся, и разойдутся молча.

    Условие без параметра — «все отделы»: :dept IS NULL пропускает всё, в
    том числе карточки без ответственного, которых нет ни в одном отделе.
    """
    return (f"(:dept IS NULL OR {assignee} IN "
            f"(SELECT user_id FROM user_home WHERE department_id = :dept))")


# Отдел берётся у ТЕКУЩЕГО ответственного за сделку: истории назначений
# Bitrix через crm.stagehistory.list не отдаёт, и хранить её нам негде.
# Значит сделка, переданная в другой отдел, приносит туда всю свою историю
# движения. Для месячных срезов это редкость, но на дашборде об этом сказано
# прямо — иначе РОП увидит в своём отделе чужие переходы и не поймёт откуда.
def _department_filter(event_alias: str = "e") -> str:
    """Условие «сделка закреплена за отделом» для запросов по событиям стадий."""
    return f"""
          AND (:dept IS NULL OR EXISTS (
                SELECT 1 FROM v_deal fd
                WHERE fd.deal_id = {event_alias}.entity_id
                  AND fd.assigned_by_id IN
                      (SELECT user_id FROM user_home WHERE department_id = :dept)))
    """


def departments_options(conn) -> list[dict[str, Any]]:
    """Отделы для выпадающего списка — только те, за кем есть сделки."""
    return _rows(
        conn,
        """
        SELECT u.department_id, u.department_name AS name, COUNT(d.deal_id) AS deals
        FROM v_user u
        JOIN v_deal d ON d.assigned_by_id = u.user_id
        WHERE u.department_id IS NOT NULL AND u.department_name <> ''
        GROUP BY u.department_id
        HAVING deals > 0
        ORDER BY u.department_name
        """,
    )


def rop_by_department(conn) -> dict[int, dict[str, Any]]:
    """Отдел → РОП, по списку фамилий из qc_delivery.

    Список фамилий — решение агентства от 31.08, и берётся он оттуда же,
    откуда его берёт рассылка QC: двум ответам на вопрос «чей это отдел»
    расходиться нельзя. Угадывать по должности здесь тем более нельзя —
    ошибка означает, что РОП увидит на дашборде чужой отдел.

    Однофамильцев не разрешаем догадкой, как и рассылка: если под фамилию
    подходят двое, не берётся ни один, и отдел остаётся без РОПа. Пустой
    ответ честнее неверного.

    Отдел берётся из user_home, то есть с учётом ростера, — тем же ответом,
    которым сужается видимость и считается план. По карточке портала РОП
    сплошь и рядом числится не там, где работает: руководитель отдела продаж
    сидит в служебном подразделении «Битрикс». Спросив карточку, эта функция
    сказала бы, что «Битрикс» возглавляет РОП, а её настоящий отдел остался
    без руководителя.

    Косметикой это не является: по этому ответу заводят учётку
    (`manage.py adduser --rop ФАМИЛИЯ`). Учётка получила бы служебное
    подразделение, РОП открыл бы дашборд и увидел пустой экран — и решил бы,
    что сломан дашборд, а не его доступ.
    """
    from qc_delivery import ROP_SURNAMES

    by_surname: dict[str, list[dict[str, Any]]] = {}
    for row in _rows(
        conn,
        "SELECT u.user_id, u.name, u.last_name, h.department_id "
        "FROM v_user u JOIN user_home h ON h.user_id = u.user_id "
        "WHERE h.department_id IS NOT NULL AND u.last_name <> ''",
    ):
        surname = str(row["last_name"]).strip().lower()
        if surname in ROP_SURNAMES:
            by_surname.setdefault(surname, []).append(row)

    directory: dict[int, dict[str, Any]] = {}
    for surname, rows in by_surname.items():
        if len(rows) != 1:
            continue
        row = rows[0]
        directory[int(row["department_id"])] = {
            "user_id": int(row["user_id"]),
            "name": row["name"],
            "surname": surname,
        }
    return directory


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
        "SELECT COUNT(*) AS n FROM v_deal "
        "WHERE category_id = :cat "
        "AND date_create >= :since AND date_create < :until",
        {"cat": category_id, "since": since, "until": until},
    ).get("n", 0)

    rows = _rows(
        conn,
        """
        SELECT s.stage_id, s.name, s.sort, s.semantic,
          (SELECT COUNT(*) FROM v_deal d
            WHERE d.category_id = s.category_id
              AND d.stage_id = s.stage_id) AS count_now,
          (SELECT COALESCE(SUM(d.opportunity), 0) FROM v_deal d
            WHERE d.category_id = s.category_id
              AND d.stage_id = s.stage_id AND d.is_closed = 0) AS amount_open,
          (SELECT COUNT(DISTINCT e.entity_id)
             FROM v_stage_event e
             JOIN v_deal d ON d.deal_id = e.entity_id
            WHERE e.entity_type = 'deal' AND e.stage_id = s.stage_id
              AND d.category_id = s.category_id
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


def funnel_by_department(
    conn, category_id: int, since: str, until: str,
) -> dict[str, Any]:
    """Та же воронка, что и deal_funnel, но отделы стоят рядом.

    Отвечает на вопрос, который одним числом выполнения не задать: отдел с
    13% — это пятеро, доводящих сделки до конца понемногу, или один, доводящий
    все, при четверых, теряющих клиентов на первом показе. Лечение у этих
    случаев разное, а на экране плана они неразличимы.

    Правила счёта взяты у deal_funnel целиком, а не выведены заново: когорта —
    сделки, СОЗДАННЫЕ в периоде; ``reached`` — сколько из них когда-либо
    доходило до стадии; шаговая конверсия осмысленна только внутри цепочки
    «в работе» и для выигрыша. Две страницы, называющие разную конверсию
    одной воронки, разошлись бы молча.

    Отдел берётся из user_home, то есть с учётом ростера: тот же ответ на
    вопрос «чей человек», что и в «Пульсе». Медиана дней на стадии считается
    по завершённым интервалам той же когорты — стадия, с которой ещё никто не
    ушёл, времени не имеет, и ноль там соврал бы.
    """
    names = {
        row["department_id"]: row["name"]
        for row in _rows(
            conn,
            "SELECT DISTINCT department_id, department_name AS name FROM v_user_all "
            "WHERE department_id IS NOT NULL AND department_name IS NOT NULL",
        )
    }
    stages = _rows(
        conn,
        "SELECT stage_id, name, sort, semantic FROM dim_stage "
        "WHERE category_id = :cat ORDER BY sort, name",
        {"cat": category_id},
    )
    cohort = _rows(
        conn,
        """
        SELECT h.department_id AS department_id, COUNT(*) AS n
        FROM v_deal d
        JOIN user_home h ON h.user_id = d.assigned_by_id
        WHERE d.category_id = :cat
          AND d.date_create >= :since AND d.date_create < :until
        GROUP BY h.department_id
        """,
        {"cat": category_id, "since": since, "until": until},
    )
    reached = _rows(
        conn,
        """
        SELECT h.department_id AS department_id, e.stage_id AS stage_id,
               COUNT(DISTINCT e.entity_id) AS reached
        FROM v_stage_event e
        JOIN v_deal d ON d.deal_id = e.entity_id
        JOIN user_home h ON h.user_id = d.assigned_by_id
        WHERE e.entity_type = 'deal' AND d.category_id = :cat
          AND d.date_create >= :since AND d.date_create < :until
        GROUP BY h.department_id, e.stage_id
        """,
        {"cat": category_id, "since": since, "until": until},
    )
    spent = _rows(
        conn,
        """
        SELECT h.department_id AS department_id, e.stage_id AS stage_id,
               e.duration_sec / 86400.0 AS days
        FROM v_stage_event e
        JOIN v_deal d ON d.deal_id = e.entity_id
        JOIN user_home h ON h.user_id = d.assigned_by_id
        WHERE e.entity_type = 'deal' AND d.category_id = :cat
          AND e.duration_sec IS NOT NULL AND e.duration_sec >= 0
          AND d.date_create >= :since AND d.date_create < :until
        """,
        {"cat": category_id, "since": since, "until": until},
    )

    hit = {(row["department_id"], row["stage_id"]): row["reached"] for row in reached}
    days: dict[tuple[int, str], list[float]] = {}
    for row in spent:
        days.setdefault((row["department_id"], row["stage_id"]), []).append(row["days"])

    departments = []
    for row in sorted(cohort, key=lambda item: -item["n"]):
        dept_id = row["department_id"]
        cells, previous = [], None
        for stage in stages:
            count = hit.get((dept_id, stage["stage_id"]), 0)
            values = days.get((dept_id, stage["stage_id"]), [])
            step = None
            if stage["semantic"] in ("in_progress", "won"):
                step = _share(count, previous) if previous else None
            if stage["semantic"] == "in_progress":
                previous = count
            cells.append({
                "stage_id": stage["stage_id"],
                "reached": count,
                "conversion_from_start": _share(count, row["n"]),
                "conversion_step": step,
                "median_days": round(percentile(values, 0.5) or 0, 1) if values else None,
            })
        departments.append({
            "department_id": dept_id,
            "name": names.get(dept_id) or f"Отдел {dept_id}",
            "cohort_size": row["n"],
            # Словарём, а не списком: шаблон обходит стадии внешним циклом, а
            # отделы внутренним, и достать клетку по порядковому номеру там
            # нельзя — во вложенном цикле счётчик принадлежит внутреннему.
            # На экране это выглядело как одинаковые числа во всех строках:
            # таблица отрисовалась, ошиблась и ничем себя не выдала.
            "stages": {cell["stage_id"]: cell for cell in cells},
        })
    return {"stages": stages, "departments": departments}


def win_rate(conn, category_id: int, since: str, until: str) -> dict[str, Any]:
    """Доля выигранных среди закрытых за период (по дате закрытия).

    Знаменатель — только закрытые сделки. Считать от всех, включая открытые,
    значит занижать конверсию тем сильнее, чем больше сделок в работе.

    Штуки считаются по всем сделкам, деньги — только по базовой валюте
    (см. _money_of). Выигранные сделки в другой валюте видны отдельным
    счётчиком ``won_foreign``: молча сложить их с рублями значит напечатать
    неверное число со знаком ₽.
    """
    money_ok = _money_of()
    row = _one(
        conn,
        f"""
        SELECT
            SUM(is_won) AS won,
            SUM(is_lost) AS lost,
            SUM(CASE WHEN is_won = 1 AND opportunity > 0 AND {money_ok}
                     THEN 1 ELSE 0 END) AS won_filled,
            COALESCE(SUM(CASE WHEN is_won = 1 AND {money_ok}
                              THEN opportunity ELSE 0 END), 0) AS won_amount,
            SUM(CASE WHEN is_won = 1 AND NOT {money_ok} THEN 1 ELSE 0 END) AS won_foreign
        FROM v_deal
        WHERE is_closed = 1
          AND closedate >= :since AND closedate < :until
          AND (:cat IS NULL OR category_id = :cat)
        """,
        {"cat": category_id, "since": since, "until": until, "base": base_currency()},
    )
    won = int(row.get("won") or 0)
    lost = int(row.get("lost") or 0)
    won_filled = int(row.get("won_filled") or 0)
    won_amount = float(row.get("won_amount") or 0)
    won_foreign = int(row.get("won_foreign") or 0)
    closed = won + lost
    # Средний чек делится на число сделок С ЗАПОЛНЕННОЙ суммой, а не на все
    # выигранные: сделка с пустой комиссией входила в делитель штукой, а в
    # делимое нулём и занижала чек тем сильнее, чем хуже заполнены карточки.
    # Сколько сделок легло в основание, отдаём рядом — подпись обязана его
    # называть, иначе «средний чек» опять читается как «по всем выигранным».
    return {
        "won": won, "lost": lost, "closed": closed,
        "win_rate": _share(won, closed),
        "won_amount": won_amount,
        "won_filled": won_filled,
        "won_coverage": _share(won_filled, won - won_foreign),
        "won_foreign": won_foreign,
        "currency": base_currency(),
        "avg_check": round(won_amount / won_filled, 2) if won_filled else 0.0,
        "avg_check_base": won_filled,
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
            FROM v_deal
            WHERE is_closed = 1 AND closedate IS NOT NULL
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

def stage_movement(
    conn,
    category_id: int,
    since: str,
    until: str,
    department_id: int | None = None,
) -> list[dict[str, Any]]:
    """Движение за период: было / вошло / вышло / осталось.

    Полноценный учёт потока, а не разность срезов: сделка, зашедшая и вышедшая
    внутри периода, в разности срезов не видна вовсе, хотя работа по ней шла.

    Четыре числа связаны тождеством, которое можно проверить глазами прямо в
    таблице::

        осталось = было + вошло − вышло

    ``opening`` («было») считается тем же запросом, что и ``remaining``, только
    на момент начала периода. Без него «осталось» не с чем сверить, и читателю
    остаётся верить на слово — на странице, весь смысл которой в сходимости
    потока.

    ``entered`` и ``left_count`` считают ПЕРЕХОДЫ, а не разные сделки: сделка,
    вернувшаяся на стадию дважды, входила дважды. Иначе тождество выше не
    сошлось бы.
    """
    return _rows(
        conn,
        f"""
        SELECT s.stage_id, s.name, s.sort, s.semantic,
          (SELECT COUNT(*) FROM v_stage_event e
            WHERE e.entity_type = 'deal' AND e.stage_id = s.stage_id
              AND e.category_id = s.category_id
              AND e.entered_at < :since
              AND (e.left_at IS NULL OR e.left_at >= :since)
              {_department_filter()}) AS opening,
          (SELECT COUNT(*) FROM v_stage_event e
            WHERE e.entity_type = 'deal' AND e.stage_id = s.stage_id
              AND e.category_id = s.category_id
              AND e.entered_at >= :since AND e.entered_at < :until
              {_department_filter()}) AS entered,
          (SELECT COUNT(*) FROM v_stage_event e
            WHERE e.entity_type = 'deal' AND e.stage_id = s.stage_id
              AND e.category_id = s.category_id
              AND e.left_at IS NOT NULL
              AND e.left_at >= :since AND e.left_at < :until
              {_department_filter()}) AS left_count,
          (SELECT COUNT(*) FROM v_stage_event e
            WHERE e.entity_type = 'deal' AND e.stage_id = s.stage_id
              AND e.category_id = s.category_id
              AND e.entered_at < :until
              AND (e.left_at IS NULL OR e.left_at >= :until)
              {_department_filter()}) AS remaining
        FROM dim_stage s
        WHERE s.category_id = :cat
        ORDER BY s.sort, s.name
        """,
        {"cat": category_id, "since": since, "until": until, "dept": department_id},
    )


def stage_transitions(
    conn,
    category_id: int,
    since: str,
    until: str,
    department_id: int | None = None,
) -> dict[str, Any]:
    """Переходы между стадиями за период — данные для тепловой карты.

    ``backwards`` — переходы назад по порядку стадий. Это не мелочь: возврат
    сделки на предыдущий шаг обычно означает, что квалификация на входе была
    неверной, и такие случаи стоит смотреть поимённо.
    """
    rows = _rows(
        conn,
        f"""
        SELECT e1.stage_id AS from_stage, e2.stage_id AS to_stage,
               COUNT(*) AS moves,
               COALESCE(sf.name, e1.stage_id) AS from_name,
               COALESCE(st.name, e2.stage_id) AS to_name,
               COALESCE(sf.sort, 0) AS from_sort,
               COALESCE(st.sort, 0) AS to_sort
        FROM v_stage_event e1
        JOIN v_stage_event e2
          ON e2.entity_type = e1.entity_type AND e2.entity_id = e1.entity_id
         AND e2.seq = e1.seq + 1
        LEFT JOIN dim_stage sf ON sf.stage_id = e1.stage_id AND sf.category_id = :cat
        LEFT JOIN dim_stage st ON st.stage_id = e2.stage_id AND st.category_id = :cat
        WHERE e1.entity_type = 'deal' AND e1.category_id = :cat
          AND e2.entered_at >= :since AND e2.entered_at < :until
          {_department_filter('e1')}
        GROUP BY e1.stage_id, e2.stage_id
        ORDER BY moves DESC
        """,
        {"cat": category_id, "since": since, "until": until, "dept": department_id},
    )
    backwards = [r for r in rows if r["to_sort"] < r["from_sort"]]
    return {
        "transitions": rows,
        "backwards": backwards,
        "backwards_total": sum(r["moves"] for r in backwards),
    }


# Сколько переходов показывать поимённо. Больше двухсот строк никто не
# читает, а страница на живом месяце их набирает под тысячу.
MOVES_SHOWN = 200


def stage_moves(
    conn,
    category_id: int,
    since: str,
    until: str,
    department_id: int | None = None,
    limit: int = MOVES_SHOWN,
) -> dict[str, Any]:
    """Переходы карточек поимённо: куда сдвинули и что после этого сделали.

    stage_transitions() отвечает на тот же вопрос счётчиками — сколько раз
    из «Подбора» ушли в «Показ». Здесь нужны сами карточки: сводное число
    говорит, что движение есть, но не даёт задать ни одного вопроса
    конкретному человеку.

    Главное здесь — последняя колонка. Перевод карточки на следующую стадию
    сам по себе не работа: в Битриксе это один клик, и стадию двигают, когда
    просят «подтянуть воронку». Работа — то, что после клика: запись о
    разговоре или поставленное дело. Переход без того и другого — ровно та
    строка, ради которой блок и заводится, и она подсвечена.

    «Что написал» ищется НЕ «после перехода вообще», а внутри стояния на
    новой стадии: от входа до выхода (left_at). Иначе запись, сделанная
    через три стадии и два месяца, оправдывала бы давно забытый переход, и
    красных строк на экране не осталось бы вовсе.

    Кто двинул — вопрос, на который витрина честно ответить не может:
    crm.stagehistory.list автора перехода не отдаёт, его нет и в самом
    Битриксе. Поэтому здесь ТЕКУЩИЙ ответственный за карточку, и страница
    называет колонку его именем, а не «кто двинул». Автор записи при этом
    настоящий — у комментария автор есть.
    """
    rows = _rows(
        conn,
        """
        SELECT d.deal_id, d.title, d.opportunity, d.currency_id,
               e2.entered_at AS moved_at,
               COALESCE(sf.name, e1.stage_id) AS from_name,
               COALESCE(st.name, e2.stage_id) AS to_name,
               COALESCE(sf.sort, 0) AS from_sort,
               COALESCE(st.sort, 0) AS to_sort,
               d.assigned_by_id AS user_id,
               COALESCE(u.name, '') AS assignee,
               COALESCE(u.department_name, '') AS department,
               (SELECT c.body FROM v_comment c
                 WHERE c.entity_id = d.deal_id AND c.is_auto = 0
                   AND c.created_at >= e2.entered_at
                   AND (e2.left_at IS NULL OR c.created_at < e2.left_at)
                 ORDER BY c.created_at LIMIT 1) AS note,
               (SELECT COALESCE(au.name, '') FROM v_comment c
                  LEFT JOIN v_user_all au ON au.user_id = c.author_id
                 WHERE c.entity_id = d.deal_id AND c.is_auto = 0
                   AND c.created_at >= e2.entered_at
                   AND (e2.left_at IS NULL OR c.created_at < e2.left_at)
                 ORDER BY c.created_at LIMIT 1) AS note_author,
               (SELECT a.subject FROM v_activity a
                 WHERE ((a.owner_type_id = 2 AND a.owner_id = d.deal_id)
                        OR (a.owner_type_id = 3 AND d.contact_id IS NOT NULL
                            AND d.contact_id > 0 AND a.owner_id = d.contact_id))
                   AND a.created_at >= e2.entered_at
                   AND (e2.left_at IS NULL OR a.created_at < e2.left_at)
                 ORDER BY a.created_at LIMIT 1) AS task
        FROM v_stage_event e1
        JOIN v_stage_event e2
          ON e2.entity_type = e1.entity_type AND e2.entity_id = e1.entity_id
         AND e2.seq = e1.seq + 1
        JOIN v_deal d ON d.deal_id = e1.entity_id
        LEFT JOIN dim_stage sf ON sf.stage_id = e1.stage_id AND sf.category_id = :cat
        LEFT JOIN dim_stage st ON st.stage_id = e2.stage_id AND st.category_id = :cat
        LEFT JOIN v_user_all u ON u.user_id = d.assigned_by_id
        WHERE e1.entity_type = 'deal' AND d.category_id = :cat
          AND e2.entered_at >= :since AND e2.entered_at < :until
          AND (:dept IS NULL OR d.assigned_by_id IN
               (SELECT user_id FROM user_home WHERE department_id = :dept))
        """,
        {"cat": category_id, "since": since, "until": until, "dept": department_id},
    )
    for row in rows:
        row["note"] = (row["note"] or "").strip()
        row["task"] = (row["task"] or "").strip()
        row["backwards"] = row["to_sort"] < row["from_sort"]
        # Ни записи, ни дела: клик был, работы не видно.
        row["silent"] = not row["note"] and not row["task"]
    # Молчаливые наверх, внутри — по деньгам: разговор начинают с самого
    # дорогого следа, который никто не оставил.
    rows.sort(key=lambda row: (not row["silent"], -(row["opportunity"] or 0)))
    return {
        "rows": rows[:limit],
        "total": len(rows),
        "silent": sum(1 for row in rows if row["silent"]),
        "shown": min(len(rows), limit),
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
        FROM v_stage_event
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
        FROM v_stage_event
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


def stage_norms(conn, category_id: int) -> dict[str, float]:
    """Норма времени на стадии по всей воронке: 75-й перцентиль, в днях.

    Читает v_stage_norm — единственное представление, не суженное по отделу
    (см. scope.py: в нём нет ни идентификаторов, ни названий, ни сумм, ни
    ответственных). Норма обязана быть одинаковой для всех, иначе «зависшая
    сделка» значит разное для РОПа и для директора.
    """
    durations: dict[str, list[float]] = {}
    for row in _rows(
        conn,
        "SELECT stage_id, duration_sec / 86400.0 AS days FROM v_stage_norm "
        "WHERE category_id = :cat",
        {"cat": category_id},
    ):
        if row["days"] is not None and row["days"] >= 0:
            durations.setdefault(row["stage_id"], []).append(row["days"])
    return {
        stage_id: percentile(values, 0.75) or 0.0
        for stage_id, values in durations.items()
    }


def funnel_norm_days(conn, category_id: int) -> float:
    """Запасная норма по всей воронке: p75 длительности любой её стадии.

    Нужна стадиям, с которых ещё никто не уходил. Своих завершённых интервалов
    у такой стадии нет, и пока порог брался только из них, карточки на ней
    выпадали из списка целиком — включая те, что стоят там дольше всех. Молчать
    о самых давних простоях хуже, чем сравнить их с общей нормой воронки.
    """
    values = [
        row["days"] for row in _rows(
            conn,
            "SELECT duration_sec / 86400.0 AS days FROM v_stage_norm "
            "WHERE category_id = :cat AND duration_sec >= 0",
            {"cat": category_id},
        ) if row["days"] is not None
    ]
    return percentile(values, 0.75) or 0.0


def stuck_deals(
    conn,
    category_id: int,
    limit: int = 50,
    department_id: int | None = None,
) -> list[dict[str, Any]]:
    """Список зависших сделок для показа — самые долгие сверху, не длиннее limit.

    Считать по этому списку итоги нельзя: он обрезан. Сумма денег на зависших
    сделках — stuck_money(), она идёт по всем найденным, а не по показанным.
    """
    return _stuck_rows(conn, category_id, department_id)[:limit]


def stuck_money(
    conn,
    category_id: int | None = None,
    department_id: int | None = None,
) -> dict[str, Any]:
    """Сколько денег стоит на зависших сделках — по ВСЕМ, а не по показанным.

    Отдельная функция, а не сумма по stuck_deals(): тот список обрезан по
    limit ради страницы, и сложение его строк давало бы деньги пятидесяти
    самых долгих сделок под подписью «деньги на зависших». Число выглядело бы
    правдоподобно и было бы занижено ровно настолько, насколько зависших
    больше полусотни.

    Валюта учитывается так же, как в money(): сделки не в базовой валюте в
    сумму не входят и считаются отдельно. Покрытие рядом с суммой обязательно
    — зависшая сделка с незаполненной комиссией это не ноль рублей риска.

    category_id=None — по всем воронкам: норма стадии у каждой воронки своя,
    поэтому считается пововоронночно и складывается, а не одним запросом.
    """
    categories = (
        [category_id] if category_id is not None
        else [row["category_id"] for row in pipelines(conn)]
    )
    base = base_currency()
    amount, deals, filled, foreign = 0.0, 0, 0, 0
    for cat in categories:
        for row in _stuck_rows(conn, cat, department_id):
            deals += 1
            currency = (row.get("currency_id") or "").strip().upper()
            if currency and currency != base:
                foreign += 1
                continue
            value = float(row.get("opportunity") or 0)
            if value > 0:
                filled += 1
            amount += value
    return {
        "amount": round(amount, 0),
        "deals": deals,
        "filled": filled,
        "coverage": _share(filled, deals - foreign),
        "foreign": foreign,
        "currency": base,
    }


def _stuck_exempt(category_id: int) -> set[str]:
    """Стадии, на которых простой не считается простоем.

    Норма стадии — перцентиль ЗАВЕРШЁННЫХ интервалов, то есть время тех
    карточек, которые со стадии ушли. Там, где уходят первыми самые быстрые,
    норма считается по ним и выходит короткой: у продавцов на «Поиске
    клиента» объект в рекламе живёт месяцами, и всё честно рекламируемое
    оказалось бы «зависшим».

    Это отбор выживших, а не свойство стадии, и лечится он не порогом.
    Отличить работу от забвения на такой стадии можно только по действиям —
    звонкам, показам. Теперь они в витрине есть, и на этот вопрос отвечает
    work.card_work(): из 285 карточек на «Поиске клиента» 216 не имеют ни
    одного разговора. Исключение остаётся: время на стадии по-прежнему
    ничего не говорит о работе, а говорят о ней звонки.
    """
    try:
        from config import get_settings

        return get_settings().analytics_stuck_exclude_stages.get(int(category_id), set())
    except Exception:  # pragma: no cover — конфиг недоступен в изолированных тестах
        return set()


def _stuck_rows(
    conn,
    category_id: int,
    department_id: int | None = None,
) -> list[dict[str, Any]]:
    """Сделки, стоящие на стадии дольше, чем p75 этой же стадии.

    Порог берётся из собственных данных воронки, а не из выдуманного числа
    дней: у «Подбора» и «Офера» нормальный срок разный, и единый порог либо
    завалит список шумом, либо пропустит реальные простои. У стадии без
    завершённых интервалов своей нормы нет — тогда берётся общая по воронке, и
    строка честно говорит, откуда её порог (``threshold_source``).

    Порог общий по воронке независимо от того, кто смотрит: и при фильтре по
    отделу, и у РОПа, видящего только свой отдел. Иначе медленный отдел
    сравнивался бы сам с собой и никогда не выглядел медленным, а одна и та же
    карточка была бы «зависшей» для директора и нормальной для РОПа.

    Интервал берётся ровно один — последний незакрытый вход в ТЕКУЩУЮ стадию
    карточки. Соединение со всеми незакрытыми интервалами задваивало карточку,
    у которой в истории осталось два открытых входа, и показывало её дни от
    чужой стадии.
    """
    thresholds = stage_norms(conn, category_id)
    fallback = funnel_norm_days(conn, category_id)
    skip = _stuck_exempt(category_id)
    rows = _rows(
        conn,
        """
        SELECT d.deal_id, d.title, d.stage_id, d.opportunity, d.currency_id,
               d.assigned_by_id, d.source_id, d.date_create,
               COALESCE(s.name, d.stage_id) AS stage_name,
               COALESCE(u.name, '') AS assignee,
               COALESCE(u.department_name, '') AS department,
               (julianday('now') - julianday(e.entered_at)) AS days_in_stage
        FROM v_deal d
        JOIN v_stage_event e
          ON e.entity_type = 'deal' AND e.entity_id = d.deal_id
         AND e.stage_id = d.stage_id AND e.left_at IS NULL
         AND e.seq = (SELECT MAX(last.seq) FROM v_stage_event last
                      WHERE last.entity_type = 'deal' AND last.entity_id = d.deal_id
                        AND last.stage_id = d.stage_id AND last.left_at IS NULL)
        LEFT JOIN dim_stage s ON s.stage_id = d.stage_id AND s.category_id = d.category_id
        LEFT JOIN v_user_all u ON u.user_id = d.assigned_by_id
        WHERE d.category_id = :cat AND d.is_closed = 0
          AND (:dept IS NULL OR d.assigned_by_id IN
               (SELECT user_id FROM user_home WHERE department_id = :dept))
        ORDER BY days_in_stage DESC
        """,
        {"cat": category_id, "dept": department_id},
    )
    stuck = []
    for row in rows:
        if row["stage_id"] in skip:
            continue
        days = row["days_in_stage"]
        own = thresholds.get(row["stage_id"], 0) or 0
        threshold = own or fallback
        if days is None or threshold <= 0 or days <= threshold:
            continue
        row["days_in_stage"] = round(days, 1)
        row["threshold_days"] = round(threshold, 1)
        row["threshold_source"] = "стадия" if own else "воронка"
        stuck.append(row)
    return stuck


def money_funnel(category_id: int | None) -> bool:
    """Считаются ли в этой воронке деньги.

    Тот же список, что несёт план: решение «где деньги» принимается один раз и
    живёт в plans. У продавцов комиссии в карточке нет вовсе — там держат
    объект и проверяют работу с собственником, — и колонка сумм показывала бы
    нули, которые читаются как «канал не принёс ничего».

    Импорт локальный: plans импортирует metrics, и на уровне модуля это цикл.
    """
    if category_id is None:
        return False
    import plans

    return int(category_id) in plans.plan_category_ids()


def _window_days(since: str, until: str) -> float:
    start, end = _parse_day(since), _parse_day(until)
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).days)


def _stalled_by_source(
    conn, category_id: int, since: str, until: str,
) -> dict[str, int]:
    """Сколько сделок когорты стоят на стадии дольше нормы — по источникам.

    Считается через _stuck_rows, а не своим запросом: «зависла» определено
    один раз, вместе с нормой стадии и списком стадий-исключений. Второе
    определение разошлось бы с первым молча — и разошлось бы именно в тот
    день, когда кто-нибудь поправит норму в одном месте из двух.
    """
    counts: dict[str, int] = {}
    for row in _stuck_rows(conn, category_id):
        created = row.get("date_create")
        if not created or created < since or created >= until:
            continue
        key = row.get("source_id") or ""
        counts[key] = counts.get(key, 0) + 1
    return counts


def deal_sources(conn, category_id: int, since: str, until: str) -> dict[str, Any]:
    """Разрез сделок по источнику: что канал привёл и чем это кончилось.

    Когорта — сделки, СОЗДАННЫЕ в периоде, а не закрытые в нём. Иначе каналы
    сравнивались бы на разном сроке дозревания: включённый в марте успел
    довести сделки до конца, включённый в августе — нет, и второй выглядел бы
    хуже при том же качестве трафика.

    Плата за это — молодая когорта недосчитывает выигранных: сделка живёт
    дольше окна, за которое на неё смотрят. Поэтому рядом возвращается
    медианный цикл воронки и признак ``matured``. Без них окно в 30 дней
    показало бы ноль выигранных у всех каналов сразу — и было бы право.

    «Зависло» считается по той же когорте, а не по всем открытым сделкам
    источника: строка обязана описывать одну совокупность. Снимок всех
    открытых рядом с когортной конверсией — это два отчёта в одной таблице,
    и разойтись они успевают уже на втором взгляде.
    """
    base = base_currency()
    with_money = money_funnel(category_id)
    rows = _rows(
        conn,
        f"""
        SELECT d.source_id,
               COALESCE(s.name, NULLIF(d.source_id, ''), 'Не указан') AS name,
               COUNT(*) AS deals,
               SUM(CASE WHEN d.is_closed = 0 THEN 1 ELSE 0 END) AS open_deals,
               SUM(CASE WHEN d.is_won = 1 THEN 1 ELSE 0 END) AS won,
               SUM(CASE WHEN d.is_lost = 1 THEN 1 ELSE 0 END) AS lost,
               COALESCE(SUM(CASE WHEN d.is_won = 1 AND {_money_of('d')}
                                 THEN d.opportunity ELSE 0 END), 0) AS won_amount,
               SUM(CASE WHEN d.is_won = 1 AND {_money_of('d')} AND d.opportunity > 0
                        THEN 1 ELSE 0 END) AS won_filled,
               SUM(CASE WHEN d.is_won = 1 AND NOT {_money_of('d')}
                        THEN 1 ELSE 0 END) AS won_foreign
        FROM v_deal d
        LEFT JOIN dim_source s ON s.source_id = d.source_id
        WHERE d.category_id = :cat
          AND d.date_create >= :since AND d.date_create < :until
        GROUP BY d.source_id
        """,
        {"cat": category_id, "since": since, "until": until, "base": base},
    )

    stalled = _stalled_by_source(conn, category_id, since, until)
    total: dict[str, Any] = {
        "source_id": None, "name": "Итого", "deals": 0, "open_deals": 0,
        "won": 0, "lost": 0, "won_amount": 0.0, "won_filled": 0,
        "won_foreign": 0, "stalled": 0,
    }
    for row in rows:
        row["stalled"] = stalled.get(row["source_id"] or "", 0)
        for key in ("deals", "open_deals", "won", "lost",
                    "won_filled", "won_foreign", "stalled"):
            total[key] += row[key] or 0
        total["won_amount"] += row["won_amount"] or 0

    for row in (*rows, total):
        row["conversion"] = _share(row["won"], row["deals"])
        row["stalled_share"] = _share(row["stalled"], row["open_deals"])
        row["share"] = _share(row["deals"], total["deals"])
        if not with_money:
            for key in ("won_amount", "won_filled", "won_foreign"):
                row.pop(key, None)
            continue
        row["won_amount"] = round(row["won_amount"], 0)
        # Деньги на одну ПРИВЕДЁННУЮ сделку, а не на выигранную: с ценой
        # канала сравнивают именно её. Средний чек выигранной у канала с
        # одной сделкой из ста выглядит прекрасно и не значит ничего.
        row["amount_per_deal"] = (
            round(row["won_amount"] / row["deals"], 0) if row["deals"] else 0
        )
        row["coverage"] = _share(row["won_filled"], row["won"] - row["won_foreign"])

    # Крупные каналы вверх: решение принимают по ним, а хвост из одной сделки
    # читают редко и никогда первым.
    rows.sort(key=lambda item: (item["deals"], item["won"]), reverse=True)
    cycle = deal_cycle_days(conn, category_id, since, until)
    median = cycle["median"] if cycle["count"] else None
    return {
        "rows": rows,
        "total": total,
        "with_money": with_money,
        "currency": base,
        # Медиана цикла — мерка зрелости окна. Короче цикла — выигранных в
        # когорте почти нет, и сравнивать каналы по ним нельзя.
        "cycle_days": median,
        "cycle_base": cycle["count"],
        "matured": median is not None and _window_days(since, until) >= median,
    }


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
        "SELECT COUNT(*) AS n FROM v_lead "
        "WHERE date_create >= :since AND date_create < :until",
        {"since": since, "until": until},
    ).get("n", 0)

    by_status = _rows(
        conn,
        """
        SELECT l.status_id, COALESCE(s.name, l.status_id) AS name,
               COALESCE(s.sort, 999) AS sort, COALESCE(s.semantic, 'in_progress') AS semantic,
               COUNT(*) AS count
        FROM v_lead l
        LEFT JOIN dim_lead_status s ON s.status_id = l.status_id
        WHERE l.date_create >= :since AND l.date_create < :until
        GROUP BY l.status_id
        ORDER BY sort, name
        """,
        {"since": since, "until": until},
    )
    for row in by_status:
        row["share"] = _share(row["count"], total)

    converted = _one(
        conn,
        "SELECT COUNT(*) AS n FROM v_lead "
        "WHERE converted_deal_id IS NOT NULL "
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
        FROM v_lead l
        LEFT JOIN dim_source s ON s.source_id = l.source_id
        LEFT JOIN v_deal d ON d.deal_id = l.converted_deal_id
        WHERE l.date_create >= :since AND l.date_create < :until
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

    Необработанный лид считается по времени ожидания «до сих пор», а не
    выбрасывается. Пока в расчёт шли только лиды с закрытым первым интервалом,
    метрика отвечала на вопрос «как быстро обрабатывают ТЕХ, КОГО обработали»,
    и была тем лучше, чем больше лидов не тронули вовсе: девять лежащих месяц
    карточек не мешали показать медиану в два часа по единственной десятой.

    ``waiting`` — сколько лидов когорты ещё ждут первой обработки. Их время
    измерено снизу: оно продолжает расти, поэтому медиана с ними — тоже оценка
    снизу, и подпись обязана это называть.

    Доступна, только если портал ведёт историю статусов лидов
    (см. etl._lead_history_supported). ``supported=False`` означает
    «не измеряем», а не «ноль».
    """
    supported = _one(
        conn, "SELECT value FROM analytics_meta WHERE key = 'lead_history_supported'",
    ).get("value") == "1"
    if not supported:
        return {"supported": False, "median": None, "p90": None,
                "count": 0, "waiting": 0}

    rows = _rows(
        conn,
        """
        SELECT CASE WHEN e.duration_sec IS NOT NULL THEN e.duration_sec / 86400.0
                    ELSE julianday('now') - julianday(e.entered_at) END AS days,
               CASE WHEN e.duration_sec IS NULL THEN 1 ELSE 0 END AS waiting
        FROM v_stage_event e
        JOIN v_lead l ON l.lead_id = e.entity_id
        WHERE e.entity_type = 'lead' AND e.seq = 0
          AND l.date_create >= :since AND l.date_create < :until
        """,
        {"since": since, "until": until},
    )
    values = [max(0.0, row["days"]) for row in rows if row["days"] is not None]
    waiting = sum(int(row["waiting"] or 0) for row in rows if row["days"] is not None)
    return {
        "supported": True,
        "median": round(percentile(values, 0.5) or 0, 2),
        "p90": round(percentile(values, 0.9) or 0, 2),
        "count": len(values),
        "waiting": waiting,
    }


def money(conn, category_id: int | None, since: str, until: str) -> dict[str, Any]:
    """Деньги + ПОКРЫТИЕ поля суммы.

    Покрытие обязательно рядом с каждой суммой. Поле «Комиссия» заполнено не
    везде — в проекте есть отдельная задача, которая ежечасно напоминает
    брокерам его заполнить. «12,4 млн ₽» без приписки «заполнено у 68% сделок»
    вводит в заблуждение ровно там, где решается вопрос о деньгах.
    """
    money_ok = _money_of()
    open_row = _one(
        conn,
        f"""
        SELECT COUNT(*) AS deals,
               SUM(CASE WHEN opportunity > 0 AND {money_ok} THEN 1 ELSE 0 END) AS filled,
               COALESCE(SUM(CASE WHEN {money_ok} THEN opportunity ELSE 0 END), 0) AS amount,
               SUM(CASE WHEN NOT {money_ok} THEN 1 ELSE 0 END) AS foreign_deals
        FROM v_deal
        WHERE is_closed = 0
          AND (:cat IS NULL OR category_id = :cat)
        """,
        {"cat": category_id, "base": base_currency()},
    )
    # Покрытие берётся из того же расчёта, что и сама сумма. Отдельный запрос
    # без is_closed давал числитель больше знаменателя: выигранная, но ещё не
    # закрытая сделка попадала только в «заполнено», и подпись под суммой
    # печатала «заполнено у 4 из 3 сделок».
    won = win_rate(conn, category_id, since, until)
    return {
        "open_amount": float(open_row.get("amount") or 0),
        "open_deals": int(open_row.get("deals") or 0),
        "open_filled": int(open_row.get("filled") or 0),
        "open_coverage": _share(
            open_row.get("filled") or 0,
            (open_row.get("deals") or 0) - (open_row.get("foreign_deals") or 0),
        ),
        "open_foreign": int(open_row.get("foreign_deals") or 0),
        "won_foreign": won["won_foreign"],
        "currency": won["currency"],
        "won_amount": won["won_amount"],
        "won_deals": won["won"],
        "won_filled": won["won_filled"],
        "won_coverage": won["won_coverage"],
        "avg_check": won["avg_check"],
        "avg_check_base": won["avg_check_base"],
    }


# Сколько закрытых сделок должна накопить стадия, чтобы доля выигранных на ней
# считалась вероятностью. Ниже этого числа один исход двигает результат на
# десятки процентов: «50%» из двух сделок выглядит на экране так же
# убедительно, как «50%» из двухсот.
FORECAST_MIN_CLOSED = 10


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
        FROM v_stage_event e
        JOIN v_deal d ON d.deal_id = e.entity_id
        WHERE e.entity_type = 'deal' AND e.category_id = :cat
        GROUP BY e.stage_id
        """,
        {"cat": category_id},
    )
    # Стадии, по которым закрытых сделок меньше порога, в ответ не попадают:
    # у них вероятности НЕТ, и это не то же самое, что «ноль». Доля выигранных
    # из двух закрытых сделок — случайность, а не история, и показывать по ней
    # деньги нельзя. Отсутствие ключа прогноз обрабатывает отдельно.
    return {
        row["stage_id"]: row["won"] / row["closed"]
        for row in rows
        if (row["closed"] or 0) >= FORECAST_MIN_CLOSED
    }


def weighted_forecast(conn, category_id: int) -> dict[str, Any]:
    """Взвешенный прогноз: сумма открытых сделок × вероятность их стадии.

    Рядом с прогнозом обязаны стоять два разных покрытия, иначе он читается
    как полная картина:

    * ``coverage`` — доля открытых сделок, у которых вообще заполнена сумма.
      Прогноз по половине заполненных сделок это половина прогноза.
    * ``priced_share`` — доля суммы, для которой у стадии есть накопленная
      вероятность. Остаток лежит в ``unpriced_amount``: по этим стадиям
      закрытых сделок меньше FORECAST_MIN_CLOSED, и оценивать их нечем.
    """
    probabilities = stage_win_probability(conn, category_id)
    rows = _rows(
        conn,
        """
        SELECT stage_id, COUNT(*) AS deals,
               COALESCE(SUM(opportunity), 0) AS amount,
               SUM(CASE WHEN opportunity > 0 THEN 1 ELSE 0 END) AS filled
        FROM v_deal
        WHERE category_id = :cat AND is_closed = 0
        GROUP BY stage_id
        """,
        {"cat": category_id},
    )
    stage_names = {s["stage_id"]: s["name"] for s in stages(conn, category_id)}
    total, deals, filled = 0.0, 0, 0
    priced_amount, unpriced_amount, unpriced_deals = 0.0, 0.0, 0
    detail = []
    for row in rows:
        probability = probabilities.get(row["stage_id"])
        deals += row["deals"]
        filled += row["filled"] or 0
        if probability is None:
            # Стадия без накопленной истории не обнуляет свои деньги: раньше
            # она получала вероятность 0, и полтора миллиона в работе
            # показывались как ожидаемый ноль. Теперь её сумма выносится из
            # прогноза отдельной строкой «не оценено».
            unpriced_amount += row["amount"]
            unpriced_deals += row["deals"]
            expected = None
        else:
            expected = row["amount"] * probability
            total += expected
            priced_amount += row["amount"]
        detail.append({
            "stage_id": row["stage_id"],
            "name": stage_names.get(row["stage_id"], row["stage_id"]),
            "deals": row["deals"], "amount": row["amount"],
            "probability": round(100 * probability, 1) if probability is not None else None,
            "expected": round(expected, 0) if expected is not None else None,
        })
    detail.sort(key=lambda r: (r["expected"] is None, -(r["expected"] or 0)))
    open_amount = priced_amount + unpriced_amount
    return {
        "expected": round(total, 0),
        "open_deals": deals,
        "open_amount": round(open_amount, 0),
        "filled_deals": filled,
        "coverage": _share(filled, deals),
        "priced_share": _share(priced_amount, open_amount),
        "unpriced_deals": unpriced_deals,
        "unpriced_amount": round(unpriced_amount, 0),
        "min_closed": FORECAST_MIN_CLOSED,
        "by_stage": detail,
    }


# --------------------------------------------------------------------------
# люди и динамика
# --------------------------------------------------------------------------

def people(conn, since: str, until: str, category_id: int | None = None) -> list[dict[str, Any]]:
    """Срез по ответственным: нагрузка когорты и закрытые за период деньги.

    «Выиграно» и «Выиграно денег» считаются ровно тем же определением, что на
    «Сделках»: закрытые в периоде, по дате закрытия. Раньше здесь
    брались сделки, СОЗДАННЫЕ в периоде, и без требования быть закрытой —
    достаточно было стоять на успешной стадии. Одна и та же подпись давала на
    двух страницах разные числа, и сумма по людям не сходилась с итогом
    компании.

    «Сделок создано» и «Открыто из них» остаются когортой по дате создания:
    это вопрос нагрузки, а не денег. Два окна в одной таблице — сознательный
    выбор, поэтому каждая колонка названа своим окном в подписи под таблицей.
    """
    money_ok = _money_of("d")
    rows = _rows(
        conn,
        f"""
        SELECT d.assigned_by_id AS user_id,
               COALESCE(u.name, 'ID ' || CAST(d.assigned_by_id AS TEXT),
                        'Без ответственного') AS name,
               COALESCE(u.department_name, '') AS department,
               SUM(CASE WHEN d.date_create >= :since AND d.date_create < :until
                        THEN 1 ELSE 0 END) AS deals_created,
               SUM(CASE WHEN d.date_create >= :since AND d.date_create < :until
                         AND d.is_closed = 0 THEN 1 ELSE 0 END) AS deals_open,
               SUM(CASE WHEN d.is_closed = 1 AND d.is_won = 1
                         AND d.closedate >= :since AND d.closedate < :until
                        THEN 1 ELSE 0 END) AS won,
               SUM(CASE WHEN d.is_closed = 1 AND d.is_lost = 1
                         AND d.closedate >= :since AND d.closedate < :until
                        THEN 1 ELSE 0 END) AS lost,
               COALESCE(SUM(CASE WHEN d.is_closed = 1 AND d.is_won = 1
                                  AND d.closedate >= :since AND d.closedate < :until
                                  AND {money_ok}
                                 THEN d.opportunity ELSE 0 END), 0) AS won_amount
        FROM v_deal d
        LEFT JOIN v_user_all u ON u.user_id = d.assigned_by_id
        -- Справочник полный, а лишнее убирает условие, и разница тут
        -- смысловая. Строк без человека две: сделка без ответственного и
        -- сделка на чужом id, которого в портале уже нет. Обе — находка, и
        -- страница их называет, а не прячет за пустой ячейкой. Обычное
        -- соединение убрало бы вместе с уволенным и эти две.
        --
        -- COALESCE(..., 1): нет строки в справочнике — значит и увольнять
        -- было некого, карточка остаётся. Есть строка и в ней 0 — человек
        -- ушёл, и отчитываться о нём отдельной строкой не о чем.
        WHERE COALESCE(u.is_active, 1) = 1
          AND (:cat IS NULL OR d.category_id = :cat)
          AND ((d.date_create >= :since AND d.date_create < :until)
               OR (d.is_closed = 1
                   AND d.closedate >= :since AND d.closedate < :until))
        GROUP BY d.assigned_by_id
        ORDER BY won_amount DESC, deals_created DESC
        """,
        {"since": since, "until": until, "cat": category_id, "base": base_currency()},
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


# Корзины ряда нарезаются по тем же московским суткам, что и границы периода
# (см. BUSINESS_TZ): иначе точка «1 сентября» на графике означала бы не тот
# день, что подпись периода над ним.
_GRAIN_SQL = {
    "day": "substr(datetime({col}, '+3 hours'), 1, 10)",
    "week": "strftime('%Y-W%W', datetime({col}, '+3 hours'))",
    "month": "substr(datetime({col}, '+3 hours'), 1, 7)",
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
            f"SELECT {bucket_created} AS bucket, COUNT(*) AS n FROM v_lead "
            "WHERE date_create >= :since AND date_create < :until "
            "GROUP BY bucket",
            {"since": since, "until": until},
        )
    }
    deals = {
        row["bucket"]: row for row in _rows(
            conn,
            f"SELECT {bucket_created} AS bucket, COUNT(*) AS n FROM v_deal "
            "WHERE date_create >= :since AND date_create < :until "
            "AND (:cat IS NULL OR category_id = :cat) GROUP BY bucket",
            {"since": since, "until": until, "cat": category_id},
        )
    }
    won = {
        row["bucket"]: row for row in _rows(
            conn,
            f"SELECT {bucket_closed} AS bucket, COUNT(*) AS n, "
            "COALESCE(SUM(opportunity), 0) AS amount FROM v_deal "
            "WHERE is_won = 1 AND closedate IS NOT NULL "
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
            "SELECT COUNT(*) AS n FROM v_deal "
            "WHERE date_create >= :since AND date_create < :until "
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
# Потолок выгрузки. Страница остаётся лёгкой (500 строк), а CSV отдаёт то, что
# обещает подпись под таблицей: выгрузка на 500 строк, названная десятью
# тысячами, — это молча обрезанный итог в чьём-то Excel.
MAX_EXPORT_ROWS = 10_000


def entity_table(
    conn,
    *,
    entity: str = "deal",
    category_id: int | None = None,
    department_id: int | None = None,
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
    max_rows: int = MAX_PAGE_SIZE,
) -> dict[str, Any]:
    """Строки, из которых сложились цифры дашборда.

    Сортировка выбирается из белого списка колонок, а не подставляется из
    запроса: имя колонки нельзя параметризовать, и приём пользовательской
    строки прямо в ORDER BY — это SQL-инъекция.

    ``department_id`` здесь — фильтр отображения, а не защита: данные уже
    ограничены на уровне соединения. Он нужен, чтобы разложение сходилось с
    числом, по которому кликнули: администратор, отфильтровавший «Движение»
    по отделу, должен увидеть в таблице тот же отдел, а не всю компанию.
    """
    entity = entity if entity in ("deal", "lead") else "deal"
    sort_column = _TABLE_SORTS[entity].get(sort, _TABLE_SORTS[entity]["created"])
    direction_sql = "ASC" if str(direction).lower() == "asc" else "DESC"
    page = max(1, int(page))
    page_size = max(1, min(int(max_rows), int(page_size)))

    conditions: list[str] = []
    params: dict[str, Any] = {}
    if entity == "deal":
        base = """
        FROM v_deal d
        LEFT JOIN dim_stage s ON s.stage_id = d.stage_id AND s.category_id = d.category_id
        LEFT JOIN v_user_all u ON u.user_id = d.assigned_by_id
        LEFT JOIN dim_source src ON src.source_id = d.source_id
        LEFT JOIN v_stage_event e
               ON e.entity_type = 'deal' AND e.entity_id = d.deal_id AND e.left_at IS NULL

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
        if department_id is not None:
            conditions.append(department_clause("d.assigned_by_id"))
            params["dept"] = department_id
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
        FROM v_lead l
        LEFT JOIN dim_lead_status st ON st.status_id = l.status_id
        LEFT JOIN v_user_all u ON u.user_id = l.assigned_by_id
        LEFT JOIN dim_source src ON src.source_id = l.source_id
        LEFT JOIN v_stage_event e
               ON e.entity_type = 'lead' AND e.entity_id = l.lead_id AND e.left_at IS NULL

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
        if department_id is not None:
            conditions.append(department_clause("l.assigned_by_id"))
            params["dept"] = department_id
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
        if only_open:
            # «Открытый лид» — тот, по которому ещё нет сделки и чей статус не
            # закрывает разговор (спам, нецелевой, отказ). Раньше чип на этой
            # вкладке подсвечивался и не фильтровал ничего: условие было
            # написано только для сделок, а параметр молча принимался.
            conditions.append(
                "l.converted_deal_id IS NULL"
                " AND COALESCE(st.semantic, 'in_progress') = 'in_progress'"
            )
        if query:
            conditions.append("(l.title LIKE :q OR CAST(l.lead_id AS TEXT) LIKE :q)")
            params["q"] = f"%{query}%"

    # WHERE собирается здесь и только здесь. Приклеивать условия как « AND …»
    # к заготовке, в которой WHERE уже есть, опасно: стоит этому WHERE
    # исчезнуть — и условия прицепятся к ON последнего LEFT JOIN. Запрос
    # останется валидным, но фильтровать перестанет: LEFT JOIN на непопадание
    # отдаёт NULL-ы, а не отбрасывает строку. Такое молчит и не падает.
    where = base + ("WHERE " + " AND ".join(conditions) if conditions else "")
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
    money_ok = _money_of()
    deals = _one(
        conn,
        f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN NOT {money_ok} THEN 1 ELSE 0 END) AS foreign_currency,
               SUM(CASE WHEN opportunity <= 0 THEN 1 ELSE 0 END) AS no_amount,
               SUM(CASE WHEN source_id = '' THEN 1 ELSE 0 END) AS no_source,
               SUM(CASE WHEN assigned_by_id IS NULL THEN 1 ELSE 0 END) AS no_assignee,
               SUM(CASE WHEN is_closed = 1 AND closedate IS NULL THEN 1 ELSE 0 END)
                   AS closed_no_date,
               SUM(CASE WHEN is_won = 1 AND opportunity <= 0 THEN 1 ELSE 0 END)
                   AS won_no_amount
        FROM v_deal
        WHERE (:cat IS NULL OR category_id = :cat)
        """,
        {"cat": category_id, "base": base_currency()},
    )
    leads = _one(
        conn,
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN source_id = '' THEN 1 ELSE 0 END) AS no_source,
               SUM(CASE WHEN assigned_by_id IS NULL THEN 1 ELSE 0 END) AS no_assignee
        FROM v_lead
        """,
    )
    orphan_stages = _one(
        conn,
        """
        SELECT COUNT(*) AS n FROM v_deal d
        WHERE NOT EXISTS (
            SELECT 1 FROM dim_stage s
            WHERE s.stage_id = d.stage_id AND s.category_id = d.category_id)
        """,
    ).get("n", 0)
    # Дата входа в стадию из будущего — почти всегда сбитые часы на портале
    # или ошибка приведения таймзоны. Молча обрезать её нельзя: она портит
    # время на стадии и списки зависших карточек.
    future_stages = _one(
        conn,
        "SELECT COUNT(*) AS n FROM v_stage_event WHERE entered_at > :now",
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
        "FROM etl_run ORDER BY id DESC LIMIT 40",
    )
    # Последний успешный прогон ищется запросом, а не перебором показанных
    # строк: список выше обрезан по LIMIT ради страницы, и при частых чужих
    # прогонах настоящий ETL из него просто выпал бы. Тогда шапка сказала бы
    # «данные не загружались» при живой витрине.
    last_ok = _one(
        conn,
        "SELECT kind, entity, started_at, finished_at, status, rows_upserted, error "
        f"FROM etl_run WHERE status = 'ok' AND kind IN ({_MART_KIND_SQL}) "
        "ORDER BY id DESC LIMIT 1",
        _MART_KIND_PARAMS,
    ) or None
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
            "SELECT (SELECT COUNT(*) FROM v_deal) AS deals, "
            "(SELECT COUNT(*) FROM v_lead) AS leads, "
            "(SELECT COUNT(*) FROM v_stage_event) AS stage_events",
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
        LEFT JOIN v_deal d ON d.category_id = p.category_id
        GROUP BY p.category_id
        ORDER BY p.sort, p.name
        """,
    )
