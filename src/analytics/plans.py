"""План: период, состав, норма и темп.

План агентства задан не суммой, а правилом — «4,5 млн комиссии на брокера за
квартал». Значит он ВЫЧИСЛЯЕТСЯ из штата и меняется вместе с ним: принятый в
середине квартала человек поднимает цель отдела, уволенный опускает. Хранить
посчитанную сумму нельзя — это второй источник правды, который разойдётся с
первым в первый же наём.

Отсюда три вещи, которые здесь решаются, и ни одна не сводится к арифметике.

**Кто несёт план.** В портале 157 учётных записей, активных 65, и лишь часть
из них продаёт. Отделы продаж не угадываются по названию: их список уже есть
в настройках (``OWNER_SALES_DEPT_IDS_JSON``), им пользуется отчёт по
собственникам, и заводить рядом второй ответ на тот же вопрос значит завести
расхождение. РОП вычитается из состава: он план не несёт.

**Почему без ручного ростера не обойтись.** Портал говорит неправду в двух
местах сразу, и оба измерены на боевых данных: РОП отдела «Волкова»
административно числится в служебном подразделении «Битрикс» — её отдел
остаётся без РОПа, а служебный получает лишнюю норму; учётка РОПа
«Каратевский» отключена при живом отделе. Чинить это правкой карточек в
Битриксе ради отчёта дороже, чем назвать исключение списком.

**Почему сравнение фамилий идёт в Python.** ``lower()`` в SQLite работает
только с латиницей: ``lower('Кретов')`` возвращает ``'Кретов'``, и запрос с
``IN ('кретов', ...)`` не находит никого — молча, без ошибки. Любое
регистронезависимое сравнение русских строк в этом проекте делается на
стороне Python.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import metrics

# Роли в плановом составе.
ROLE_BROKER = "broker"
ROLE_ROP = "rop"
ROLE_EXCLUDED = "excluded"

# Строка ростера, действующая во всех периодах: исключения вроде «РОП сидит
# не в своём отделе» переживают смену квартала, и переписывать их каждые три
# месяца — верный способ однажды забыть.
ANY_PERIOD = "*"

# Норма задаётся либо на брокера, либо суммой на весь охват.
BASIS_PER_BROKER = "per_broker"
BASIS_ABSOLUTE = "absolute"

SCOPE_COMPANY = "company"
SCOPE_DEPARTMENT = "department"
COMPANY_SCOPE_ID = 0

METRIC_COMMISSION = "commission"

# Календарь пока один — рабочая неделя пн–пт. Производственного календаря с
# праздниками у витрины нет, и выдавать одно за другое нельзя: январский темп,
# посчитанный без каникул, наврёт сильнее всего именно там, где решается
# годовой итог. Поэтому источник календаря называется в ответе, а не
# подразумевается.
CALENDAR_WEEKDAYS = "пн-пт"


# --------------------------------------------------------------------------
# период
# --------------------------------------------------------------------------

def quarter_code(day: date) -> str:
    """Код квартала, которому принадлежит день: 2026-Q3."""
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def quarter_bounds(code: str) -> tuple[str, str]:
    """Границы квартала [starts_at, ends_at) в UTC по московскому календарю.

    Считается здесь, а не берётся пресетом ``quarter`` из metrics: тот даёт
    «квартал по сегодняшний день», и планом быть не может — в первый день
    квартала выполнение вышло бы стопроцентным.
    """
    year, quarter = _parse_quarter(code)
    first_month = 3 * (quarter - 1) + 1
    starts = date(year, first_month, 1)
    ends = date(year + 1, 1, 1) if quarter == 4 else date(year, first_month + 3, 1)
    return _utc(starts), _utc(ends)


def period(conn, code: str) -> dict[str, Any]:
    """Границы планового периода.

    Объявленный период выигрывает у вычисленного: агентство вправе начать
    квартал не первого числа, и календарь об этом не знает.
    """
    row = _one(
        conn,
        "SELECT period_code, starts_at, ends_at, label FROM v_plan_period "
        "WHERE period_code = :code",
        {"code": code},
    )
    if row:
        row["declared"] = True
        return row
    starts, ends = quarter_bounds(code)
    return {"period_code": code, "starts_at": starts, "ends_at": ends,
            "label": code, "declared": False}


def _parse_quarter(code: str) -> tuple[int, int]:
    try:
        year_part, quarter_part = code.strip().upper().split("-Q")
        year, quarter = int(year_part), int(quarter_part)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"Код квартала должен выглядеть как 2026-Q3, получено {code!r}") from exc
    if not 1 <= quarter <= 4:
        raise ValueError(f"Квартал вне диапазона 1..4: {code!r}")
    return year, quarter


def _utc(day: date) -> str:
    return datetime(
        day.year, day.month, day.day, tzinfo=metrics.BUSINESS_TZ,
    ).astimezone(timezone.utc).isoformat()


def _day_of(moment: str) -> date:
    return datetime.fromisoformat(moment).astimezone(metrics.BUSINESS_TZ).date()


# --------------------------------------------------------------------------
# темп
# --------------------------------------------------------------------------

def working_days(since: str, until: str, holidays: Iterable[date] = ()) -> int:
    """Рабочих дней в [since, until) по московскому календарю.

    По календарным дням темп считать нельзя: в месяце с длинными выходными в
    начале любой отдел откроет период словом «отстаём» и закроет словом
    «нагнали», хотя работа шла ровно.
    """
    skip = set(holidays)
    start, end = _day_of(since), _day_of(until)
    days = 0
    current = start
    while current < end:
        if current.weekday() < 5 and current not in skip:
            days += 1
        current += timedelta(days=1)
    return days


def pace(
    fact: float,
    plan_amount: float | None,
    elapsed_days: int,
    total_days: int,
) -> dict[str, Any]:
    """Темп: доля плана против доли срока. Чистая функция, витрина не нужна.

    ``ratio`` больше единицы — идём с опережением. None означает «сказать
    нечего», и это не то же самое, что ноль: нулевой план и невыполненный
    план выглядят на экране одинаково, если оба показать нулём.
    """
    plan_share = (
        round(100.0 * fact / plan_amount, 1)
        if plan_amount else None
    )
    time_share = round(100.0 * elapsed_days / total_days, 1) if total_days else None
    ratio = (
        round(plan_share / time_share, 2)
        if plan_share is not None and time_share else None
    )
    return {
        "fact": fact,
        "plan": plan_amount,
        "plan_share": plan_share,
        "time_share": time_share,
        "elapsed_days": elapsed_days,
        "total_days": total_days,
        "ratio": ratio,
        "behind": None if ratio is None else ratio < 1,
        "calendar_source": CALENDAR_WEEKDAYS,
        "reason": None if plan_amount else "план не задан",
    }


# --------------------------------------------------------------------------
# состав
# --------------------------------------------------------------------------

def sales_department_ids() -> tuple[int, ...]:
    """Отделы, несущие план. Берутся из настройки, а не выводятся заново.

    Тот же список читает отчёт по собственникам. Второй ответ на вопрос «какие
    отделы продают» означал бы, что два отчёта одного агентства называют
    разное число брокеров в один день.
    """
    try:
        from config import get_settings

        return tuple(int(value) for value in get_settings().owner_sales_dept_ids)
    except Exception:  # pragma: no cover — конфиг недоступен в изолированных тестах
        return ()


def rop_surnames() -> frozenset[str]:
    """Фамилии РОПов в нижнем регистре. Источник — тот же, что у рассылки QC."""
    try:
        from qc_delivery import ROP_SURNAMES

        return frozenset(name.strip().lower() for name in ROP_SURNAMES)
    except Exception:  # pragma: no cover
        return frozenset()


def headcount(
    conn,
    period_code: str,
    department_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Кто несёт план: состав по отделам на момент запроса.

    Возвращает и сам состав, и всё, что делает его спорным: отделы без
    опознанного РОПа, людей, роль которых назначена вручную, и отделы, чей
    план завышен на одну норму, потому что вычитать некого. Неопределённость
    названа числом, а не спрятана и не превращена в отказ считать.
    """
    allowed = tuple(department_ids) if department_ids is not None else sales_department_ids()
    surnames = rop_surnames()
    overrides = _roster(conn, period_code)

    departments: dict[int, dict[str, Any]] = {}
    for user in _rows(
        conn,
        "SELECT user_id, name, last_name, department_id, department_name "
        "FROM v_user WHERE is_active = 1",
    ):
        override = overrides.get(user["user_id"], {})
        dept_id = override.get("department_id") or user["department_id"]
        if dept_id is None or (allowed and dept_id not in allowed):
            continue
        # Сравнение фамилии — в Python: lower() в SQLite не трогает кириллицу.
        surname = (user["last_name"] or "").strip().lower()
        role = override.get("plan_role") or (
            ROLE_ROP if surname and surname in surnames else ROLE_BROKER
        )
        bucket = departments.setdefault(dept_id, {
            "department_id": dept_id,
            "name": user["department_name"] or f"Отдел {dept_id}",
            "people": 0, "brokers": 0, "rops": 0, "excluded": 0,
            "rop_names": [], "overridden": 0,
        })
        # Название отдела берётся у того, кто в нём действительно числится:
        # у перенесённого ростером РОПа в карточке стоит чужое подразделение.
        if not override.get("department_id") and user["department_name"]:
            bucket["name"] = user["department_name"]
        bucket["people"] += 1
        if override:
            bucket["overridden"] += 1
        if role == ROLE_ROP:
            bucket["rops"] += 1
            bucket["rop_names"].append(user["name"])
        elif role == ROLE_EXCLUDED:
            bucket["excluded"] += 1
        else:
            bucket["brokers"] += 1

    rows = sorted(departments.values(), key=lambda row: -row["brokers"])
    for row in rows:
        row["rop_known"] = row["rops"] > 0
    missing = [row["name"] for row in rows if not row["rop_known"]]
    return {
        "period_code": period_code,
        "departments": rows,
        "brokers": sum(row["brokers"] for row in rows),
        "people": sum(row["people"] for row in rows),
        "departments_without_rop": missing,
        "sales_department_ids": allowed,
    }


def _roster(conn, period_code: str) -> dict[int, dict[str, Any]]:
    """Ручные исключения. Строка периода перекрывает строку «на все периоды»."""
    overrides: dict[int, dict[str, Any]] = {}
    for row in _rows(
        conn,
        "SELECT user_id, department_id, plan_role, note, period_code "
        "FROM v_plan_roster WHERE period_code IN (:code, :any) "
        "ORDER BY CASE WHEN period_code = :any THEN 0 ELSE 1 END",
        {"code": period_code, "any": ANY_PERIOD},
    ):
        overrides[row["user_id"]] = row
    return overrides


# --------------------------------------------------------------------------
# план
# --------------------------------------------------------------------------

def plan(conn, period_code: str, metric: str = METRIC_COMMISSION) -> dict[str, Any]:
    """Вычисленный план периода: по отделам и по компании.

    План компании по умолчанию — сумма отделов, снизу вверх. Объявленная
    норма компании его не заменяет молча: если она есть и не сходится с
    суммой, расхождение возвращается отдельным числом. Директор вправе
    поставить цель выше суммы отделов, и прятать этот зазор нельзя — как не
    прячется покрытие поля «Комиссия» рядом с суммой.
    """
    staff = headcount(conn, period_code)
    norms = {
        (row["scope_kind"], row["scope_id"]): row
        for row in _rows(
            conn,
            "SELECT scope_kind, scope_id, basis, amount, source FROM v_plan_norm "
            "WHERE period_code = :code AND metric = :metric",
            {"code": period_code, "metric": metric},
        )
    }
    default = norms.get((SCOPE_COMPANY, COMPANY_SCOPE_ID))

    departments = []
    for row in staff["departments"]:
        norm = norms.get((SCOPE_DEPARTMENT, row["department_id"])) or default
        amount, basis, reason = None, None, "норма не задана"
        if norm:
            basis = norm["basis"]
            if basis == BASIS_PER_BROKER:
                amount = norm["amount"] * row["brokers"]
                reason = None
            elif basis == BASIS_ABSOLUTE:
                amount = norm["amount"]
                reason = None
            else:
                reason = f"неизвестная база нормы: {basis}"
        departments.append({**row, "plan": amount, "basis": basis, "reason": reason})

    from_departments = sum(row["plan"] or 0 for row in departments)
    declared = None
    if default and default["basis"] == BASIS_ABSOLUTE:
        declared = default["amount"]

    return {
        "period_code": period_code,
        "metric": metric,
        "departments": departments,
        "brokers": staff["brokers"],
        "plan_from_departments": round(from_departments, 2),
        "plan_declared": declared,
        # Зазор между целью компании и суммой отделов — то самое число, о
        # котором руководитель спросит первым. Оно названо, а не подогнано.
        "unallocated": (
            round(declared - from_departments, 2) if declared is not None else None
        ),
        "plan": declared if declared is not None else round(from_departments, 2),
        "departments_without_rop": staff["departments_without_rop"],
        "norms_found": len(norms),
    }


# --------------------------------------------------------------------------
# служебное
# --------------------------------------------------------------------------

def _rows(conn, sql: str, params: dict[str, Any] | Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(conn, sql: str, params: dict[str, Any] | Sequence[Any] = ()) -> dict[str, Any]:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}
