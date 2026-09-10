"""План: период, состав, норма и темп.

План агентства задан не одной суммой на всех, а поимённо: на третий квартал
названы 29 человек с тремя ступенями нормы (3,5 / 4,5 / 5,5 млн комиссии), а
новички норму не несут вовсе. Поэтому норма здесь — не число в настройке, а
строка, привязанная к учётной записи, и план периода складывается снизу: из
людей в отделы, из отделов в компанию.

Отсюда три вещи, которые здесь решаются, и ни одна не сводится к арифметике.

**Кто вообще может нести план.** В портале 157 учётных записей, активных 65,
и лишь часть из них продаёт. Отделы продаж не угадываются по названию: их
список уже есть в настройках (``OWNER_SALES_DEPT_IDS_JSON``), им пользуется
отчёт по собственникам, и заводить рядом второй ответ на тот же вопрос значит
завести расхождение. РОП из состава вычитается — но это умолчание, а не
запрет: названный поимённо руководитель план несёт, и в списке третьего
квартала такой человек есть.

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
SCOPE_USER = "user"
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


def quarter_label(code: str) -> str:
    """«2026-Q3» → «3 кв. 2026». Код нужен адресу, человеку нужен квартал."""
    try:
        year, quarter = _parse_quarter(code)
    except ValueError:
        return code
    return f"{quarter} кв. {year}"


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
            "label": quarter_label(code), "declared": False}


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


def months_in(since: str, until: str) -> int:
    """Календарных месяцев в периоде [since, until).

    Расходы живут месяцами, план — кварталами, и переводить одно в другое
    делением на 30 дней нельзя: в квартале бывает 90 дней, бывает 92, а
    расходов всегда три месяца.
    """
    start, end = _day_of(since), _day_of(until)
    months = (end.year - start.year) * 12 + end.month - start.month
    return max(months, 1)


def breakeven(
    fact: float,
    projection: float | None,
    since: str,
    until: str,
) -> dict[str, Any] | None:
    """Рубеж безубыточности периода и расстояние до него.

    Зачем рядом с планом. План агентства — намеренная планка (решение от
    07.09): норма поставлена высоко, чтобы к ней тянулись, и выполнение по
    ней держится в диапазоне 0–15% весь квартал. Число, которое не меняет
    цвет от работы, перестают читать, а вместе с ним перестают читать и
    остальной экран.

    Рубеж отвечает на другой вопрос — не «к чему тянемся», а «доживём ли», —
    и он движется от каждой сделки. Одно не заменяет другое: планка
    остаётся целью для людей, рубеж нужен собственнику.

    None, если расходы или доля не заданы. Выдуманный порог хуже
    отсутствующего: по нему принимают решения о людях.
    """
    try:
        from config import get_settings

        settings = get_settings()
        monthly, share = float(settings.pulse_monthly_costs), float(settings.pulse_net_share)
    except Exception:  # pragma: no cover — конфиг недоступен в изолированных тестах
        return None
    if monthly <= 0 or not 0 < share <= 1:
        return None

    months = months_in(since, until)
    gross = monthly * months / share
    return {
        "gross": round(gross, 0),
        "share": round(100.0 * fact / gross, 1),
        "left": round(max(gross - fact, 0.0), 0),
        "projected_share": (
            round(100.0 * projection / gross, 1) if projection is not None else None
        ),
        "reaches": None if projection is None else projection >= gross,
        "gap": (
            None if projection is None else round(projection - gross, 0)
        ),
        "monthly_costs": monthly,
        "net_share": share,
        "months": months,
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


def plan_category_ids() -> tuple[int, ...]:
    """Воронки, сделки которых идут в план.

    Решение агентства от 07.09: план несёт только «Покупатели». Продавцы,
    новостройки и общая база в выполнение не входят — там другая работа и
    другой чек, и складывать их с покупателями значит сравнивать план с
    фактом, собранным по другому правилу.

    Список берётся из настройки и никогда не пуст: пустой означал бы «все
    воронки», то есть молчаливое расширение плана ровно в тот момент, когда
    кто-то ошибётся в переменной окружения.
    """
    try:
        from config import get_settings

        return tuple(int(value) for value in get_settings().pulse_category_ids)
    except Exception:  # pragma: no cover — конфиг недоступен в изолированных тестах
        return (18,)


def category_filter(
    alias: str, categories: Sequence[int] | None = None,
) -> tuple[str, dict[str, int]]:
    """Условие «сделка из воронки, несущей план», и параметры к нему.

    Живёт здесь, а не в двух запросах по месту: воронки плана — вопрос
    плана, и отвечать на него должен один модуль. Дайджест и страница уже
    один раз разошлись в том, кто такой РОП; повторять это на воронках
    незачем.

    Список склеивается через именованные параметры, а не подставляется
    числами в строку: он приходит из настройки, и f-string открыл бы дорогу
    значению, которого никто не проверял.
    """
    values = plan_category_ids() if categories is None else categories
    params = {f"cat{i}": int(value) for i, value in enumerate(values)}
    names = ", ".join(f":{key}" for key in params)
    return f"{alias}.category_id IN ({names})", params


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
        "FROM v_user",
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
            "rop_list": [], "overridden": 0, "members": [],
        })
        # Название отдела берётся у того, кто в нём действительно числится:
        # у перенесённого ростером РОПа в карточке стоит чужое подразделение.
        if not override.get("department_id") and user["department_name"]:
            bucket["name"] = user["department_name"]
        bucket["people"] += 1
        bucket["members"].append({
            "user_id": user["user_id"], "name": user["name"],
            "role": role, "overridden": bool(override),
        })
        if override:
            bucket["overridden"] += 1
        if role == ROLE_ROP:
            bucket["rops"] += 1
            # Поимённо, а не счётчиком: рассылке нужно, кому именно писать, и
            # брать это из другого источника нельзя. Портал считает РОПом
            # того, кто числится в отделе, ростер — того, кто за отдел
            # отвечает. Два ответа на один вопрос рано или поздно разойдутся,
            # и разойдутся молча: отдел просто перестанет получать отчёт.
            bucket["rop_list"].append({
                "user_id": user["user_id"],
                "name": user["name"],
                "surname": (user["last_name"] or "").strip().lower(),
            })
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
    # Именной режим. Как только у периода появилась хоть одна норма на
    # человека, план перестаёт вычисляться из штата: он и есть этот список.
    # Общая норма при этом не подставляется тем, кого в списке нет, — иначе
    # новичок, которому норму сознательно не ставили, молча получил бы её и
    # завысил план отдела. «Без нормы» тут решение агентства, а не пробел.
    named = any(kind == SCOPE_USER for kind, _ in norms)

    departments = []
    for row in staff["departments"]:
        members, amount, without = [], 0.0, 0
        for member in row["members"]:
            member_plan = _norm_for(
                member, row["department_id"], norms, default, named,
            )
            if member_plan is None:
                without += 1
            else:
                amount += member_plan
            members.append({**member, "plan": member_plan})
        on_plan = len(members) - without
        departments.append({
            **row,
            "members": members,
            "on_plan": on_plan,
            "without_norm": without,
            "plan": round(amount, 2) if on_plan else None,
            "basis": SCOPE_USER if named else (default or {}).get("basis"),
            "reason": None if on_plan else "норма не задана",
        })

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

def _norm_for(
    member: dict[str, Any],
    department_id: int,
    norms: dict[tuple[str, int], dict[str, Any]],
    default: dict[str, Any] | None,
    named: bool,
) -> float | None:
    """Норма конкретного человека. None — «нормы нет», а не «ноль».

    Порядок: именная норма, затем норма отдела, затем общая. РОП исключается
    из плана только тогда, когда его нет в именном списке: агентство вправе
    поставить план и руководителю, и в списке на третий квартал такой человек
    есть. Роль решает, кто вычитается по умолчанию, а не кто не может нести
    план вовсе.
    """
    personal = norms.get((SCOPE_USER, member["user_id"]))
    if personal is not None:
        return personal["amount"]
    if named:
        return None
    if member["role"] != ROLE_BROKER:
        return None
    norm = norms.get((SCOPE_DEPARTMENT, department_id)) or default
    if not norm:
        return None
    if norm["basis"] == BASIS_PER_BROKER:
        return norm["amount"]
    # Норма суммой на отдел на одного человека не раскладывается: делить её
    # поровну значило бы придумать распределение, которого никто не задавал.
    return None


def _rows(conn, sql: str, params: dict[str, Any] | Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(conn, sql: str, params: dict[str, Any] | Sequence[Any] = ()) -> dict[str, Any]:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}
