"""HTML-страницы дашборда.

Данные отдаются шаблону сразу, а графики получают их островками
<script type="application/json">. Так строгий CSP обходится без
'unsafe-inline', а страница не мигает спиннерами при каждом открытии.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

import chartdata
import metrics
import objects
import plans
import pulse as pulse_metrics
from context import base_context, read_analytics

router = APIRouter()


def _render(request: Request, template: str, context: dict) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, template, context)


def _default_category(context: dict) -> int | None:
    """Воронка по умолчанию — первая заведённая, если явно не выбрана."""
    if context["category_id"] is not None:
        return context["category_id"]
    pipelines = context["pipelines"]
    return pipelines[0]["category_id"] if pipelines else None


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    context = base_context(request, active="")
    period, category_id = context["period"], context["category_id"]
    grain = _grain_for(period)
    with read_analytics(request) as conn:
        series = metrics.timeseries(
            conn, period["since"], period["until"], category_id, grain=grain,
        )
        context.update({
            "overview": metrics.overview(conn, period, category_id),
            "pipeline_counts": metrics.counts_by_pipeline(conn),
            "leads": metrics.lead_funnel(conn, period["since"], period["until"]),
            "charts": {"timeseries": chartdata.timeseries_chart(series, grain)},
        })
    return _render(request, "overview.html", context)


@router.get("/pulse", response_class=HTMLResponse)
async def pulse(request: Request) -> HTMLResponse:
    """План, факт и темп квартала — первый экран руководителя.

    Период здесь свой и общему фильтру не подчиняется: план живёт на
    квартале, и «текущий месяц» из шапки означал бы план квартала против
    факта месяца. Такое число выглядит совершенно нормальным, и это худший
    вид ошибки — заметить её можно только сверкой вручную.
    """
    context = base_context(request, active="pulse")
    period_code = _quarter_code(request.query_params.get("period_code"))
    with read_analytics(request) as conn:
        context.update({
            "pulse": pulse_metrics.pulse(conn, period_code, with_stuck=True),
            "period_code": period_code,
            "quarters": _quarter_choices(),
        })
    return _render(request, "pulse.html", context)


@router.get("/leads", response_class=HTMLResponse)
async def leads(request: Request) -> HTMLResponse:
    context = base_context(request, active="leads")
    period = context["period"]
    with read_analytics(request) as conn:
        funnel = metrics.lead_funnel(conn, period["since"], period["until"])
        context.update({
            "funnel": funnel,
            "sources": metrics.lead_sources(conn, period["since"], period["until"]),
            "first_move": metrics.lead_first_move_days(
                conn, period["since"], period["until"],
            ),
            "charts": {"funnel": chartdata.lead_funnel_chart(funnel)},
        })
    return _render(request, "leads.html", context)


@router.get("/deals", response_class=HTMLResponse)
async def deals(request: Request) -> HTMLResponse:
    context = base_context(request, active="deals")
    period = context["period"]
    category_id = _default_category(context)
    context["category_id"] = category_id
    with read_analytics(request) as conn:
        if category_id is None:
            context.update({"funnel": None, "charts": {}})
            return _render(request, "deals.html", context)
        funnel = metrics.deal_funnel(
            conn, category_id, period["since"], period["until"],
        )
        durations = metrics.stage_durations(
            conn, category_id, period["since"], period["until"],
        )
        context.update({
            "funnel": funnel,
            "durations": durations,
            "wins": metrics.win_rate(conn, category_id, period["since"], period["until"]),
            "cycle": metrics.deal_cycle_days(
                conn, category_id, period["since"], period["until"],
            ),
            "money": metrics.money(conn, category_id, period["since"], period["until"]),
            "forecast": metrics.weighted_forecast(conn, category_id),
            # Разрез по источнику живёт здесь, а не на «Лидах»: лид до сделки
            # доходит не всегда, и канал, который приводит сразу сделку, в
            # отчёте по лидам не виден вовсе.
            "sources": metrics.deal_sources(
                conn, category_id, period["since"], period["until"],
            ),
            # Воронка продавцов комиссию не ведёт: там держат объект и
            # проверяют работу. Денежные блоки страницы показывали бы нули,
            # а ноль рублей и «поле не заполняют» — разные утверждения.
            "with_money": metrics.money_funnel(category_id),
            "charts": {
                "funnel": chartdata.funnel_chart(funnel),
                "durations": chartdata.durations_chart(durations),
            },
        })
    return _render(request, "deals.html", context)


@router.get("/movement", response_class=HTMLResponse)
async def movement(request: Request) -> HTMLResponse:
    context = base_context(request, active="movement")
    period = context["period"]
    category_id = _default_category(context)
    context["category_id"] = category_id
    with read_analytics(request) as conn:
        if category_id is None:
            context.update({
                "movement": [], "transitions": None, "stuck": [],
                "charts": {}, "matrix": None,
            })
            return _render(request, "movement.html", context)
        department_id = context["department_id"]
        movement_rows = metrics.stage_movement(
            conn, category_id, period["since"], period["until"], department_id,
        )
        transitions = metrics.stage_transitions(
            conn, category_id, period["since"], period["until"], department_id,
        )
        context.update({
            "movement": movement_rows,
            "transitions": transitions,
            "stuck": metrics.stuck_deals(conn, category_id, department_id=department_id),
            # Сравнение отделов рядом — вопрос, который фильтром «один отдел»
            # не задать: где именно каждый теряет клиентов и где тормозит.
            "by_department": metrics.funnel_by_department(
                conn, category_id, period["since"], period["until"],
            ),
            "charts": {"netflow": chartdata.net_flow_chart(movement_rows)},
            "matrix": chartdata.transitions_matrix(
                transitions, metrics.stages(conn, category_id),
            ),
        })
    return _render(request, "movement.html", context)


@router.get("/people", response_class=HTMLResponse)
async def people(request: Request) -> HTMLResponse:
    context = base_context(request, active="people")
    period, category_id = context["period"], context["category_id"]
    with read_analytics(request) as conn:
        rows = metrics.people(conn, period["since"], period["until"], category_id)
        context.update({
            "people": rows,
            "departments": metrics.departments(
                conn, period["since"], period["until"], category_id,
            ),
            "charts": {"people": chartdata.people_chart(rows)},
        })
    return _render(request, "people.html", context)


@router.get("/table", response_class=HTMLResponse)
async def table(request: Request) -> HTMLResponse:
    context = base_context(request, active="table")
    params = request.query_params
    period = context["period"]
    entity = params.get("entity", "deal")
    with read_analytics(request) as conn:
        context.update({
            "table": metrics.entity_table(
                conn,
                entity=entity,
                category_id=context["category_id"],
                department_id=context["department_id"],
                stage_id=params.get("stage") or None,
                assigned_by_id=_int_or_none(params.get("assignee")),
                source_id=params.get("source") or None,
                since=period["since"] if params.get("all_time") != "1" else None,
                until=period["until"] if params.get("all_time") != "1" else None,
                query=params.get("q") or None,
                only_open=params.get("open") == "1",
                sort=params.get("sort", "created"),
                direction=params.get("dir", "desc"),
                page=_int_or_none(params.get("page")) or 1,
                page_size=_int_or_none(params.get("size")) or 50,
            ),
            "entity": entity,
            "stages": (
                metrics.stages(conn, context["category_id"])
                if context["category_id"] is not None else []
            ),
            "users": metrics.users(conn),
            "sources": metrics.sources(conn),
            "selected": {
                "stage": params.get("stage", ""),
                "assignee": params.get("assignee", ""),
                "source": params.get("source", ""),
                "q": params.get("q", ""),
                "open": params.get("open", ""),
                "all_time": params.get("all_time", ""),
                "sort": params.get("sort", "created"),
                "dir": params.get("dir", "desc"),
            },
        })
    return _render(request, "table.html", context)


# Обработчик синхронный намеренно, в отличие от соседних async def. Запрос к
# Афине — это блокирующий httpx внутри, и в async-обработчике он занимал бы
# цикл событий на всё время ожидания: медленная Афина подвешивала бы весь
# дашборд, включая /healthz. Starlette уносит обычный def в пул потоков.
@router.get("/objects", response_class=HTMLResponse)
def objects_page(request: Request) -> HTMLResponse:
    """Объекты Афины: счётчики, реклама, сайт и снятия.

    Единственная страница дашборда, данные для которой приходят не из
    витрины, а по HTTP из другой системы. Отсюда и два отличия: область
    видимости к ним неприменима (см. objects.py), а отказ источника — штатное
    состояние страницы, а не пятисотая.
    """
    context = base_context(request, active=objects.SLUG)
    settings = request.app.state.settings
    # None — администратор, видит всё. Кортеж — разрешённые отделы Афины;
    # пустой означает «ничего», и это не полдоступа, а отказ.
    allowed = objects.allowed_departments(
        getattr(request.state, "user", None), settings.afina_department_map)
    selected = objects.resolve(request.query_params, allowed)
    period = objects.clamp_period(context["period"])
    context.update({
        "selected": selected,
        "period": period,
        # Раздел показывает свой набор окон, а не общий для дашборда: на
        # длинных периодах снимок и события расходятся (см. objects.py).
        "period_presets": objects.PERIOD_PRESETS,
        "views": objects.views_for(allowed),
        "listing_filters": objects.filters_for(allowed),
        "configured": objects.is_configured(settings),
        "tiles": [],
        "scoped": False,
        "table": None,
        "card": None,
        "breakdown": None,
        "departments": [],
        "objects": None,
        "truncated": False,
        "error": None,
    })
    if allowed is not None and not allowed:
        return _forbidden(request, context)
    if context["configured"]:
        context.update(objects.load(
            objects.client_for(settings), selected, period,
            settings.afina_api_page_size,
            previous=metrics.previous_period(period),
            allowed=allowed,
        ))
    return _render(request, "objects.html", context)


@router.get("/quality", response_class=HTMLResponse)
async def quality(request: Request) -> HTMLResponse:
    context = base_context(request, active="quality")
    with read_analytics(request) as conn:
        context.update({
            "quality": metrics.data_quality(conn, context["category_id"]),
            "status": metrics.etl_status(conn),
            "pipeline_counts": metrics.counts_by_pipeline(conn),
        })
    return _render(request, "quality.html", context)


def _quarter_code(requested: str | None) -> str:
    """Код квартала из адреса. Мусор и будущее молча падают в текущий.

    Подрезка нужна не ради безопасности — данных за будущий квартал просто
    нет, — а чтобы опечатка в адресе давала понятный экран, а не пустой.
    """
    from datetime import datetime

    current = plans.quarter_code(datetime.now(metrics.BUSINESS_TZ).date())
    if not requested:
        return current
    try:
        plans.quarter_bounds(requested)
    except ValueError:
        return current
    return min(requested.strip().upper(), current)


def _quarter_choices(depth: int = 4) -> list[tuple[str, str]]:
    """Текущий квартал и предыдущие — для переключателя над экраном."""
    from datetime import datetime

    today = datetime.now(metrics.BUSINESS_TZ).date()
    year, quarter = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(depth):
        out.append((f"{year}-Q{quarter}", f"{quarter} кв. {year}"))
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
    return out


def _forbidden(request: Request, context: dict) -> HTMLResponse:
    """Отказ в доступе к разделу. Без подробностей: чего нет, того не видно."""
    return request.app.state.templates.TemplateResponse(
        request, "error.html",
        {"code": 403, "message": "Раздел доступен только администратору",
         "base_path": context["base_path"], "user": context["user"]},
        status_code=403,
    )


def _grain_for(period: dict) -> str:
    """Дробность динамики под длину периода: 90 точек по дням ещё читаются, 365 — нет."""
    from datetime import datetime

    span_days = (
        datetime.fromisoformat(period["until"]) - datetime.fromisoformat(period["since"])
    ).days
    if span_days <= 62:
        return "day"
    if span_days <= 200:
        return "week"
    return "month"


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
