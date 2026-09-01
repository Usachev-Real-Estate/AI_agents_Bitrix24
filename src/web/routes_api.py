"""JSON-эндпоинты и выгрузка CSV.

Всё под /api защищено тем же middleware, что и страницы: анонимный запрос
получает 401 и пустое тело, а не данные.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

import metrics
from context import read_analytics, resolve_filters
from links import crm_link

router = APIRouter(prefix="/api")

CSV_MAX_ROWS = 10_000

CSV_COLUMNS = [
    ("id", "ID"),
    ("title", "Название"),
    ("pipeline_name", "Воронка"),
    ("stage_name", "Стадия"),
    ("assignee", "Ответственный"),
    ("department", "Отдел"),
    ("source_name", "Источник"),
    ("amount", "Сумма"),
    ("created", "Создано"),
    ("modified", "Изменено"),
    ("days_in_stage", "Дней на стадии"),
    ("link", "Ссылка"),
]


@router.get("/overview")
async def api_overview(request: Request) -> JSONResponse:
    filters = resolve_filters(request)
    with read_analytics(request) as conn:
        return JSONResponse({
            "period": filters["period"],
            "overview": metrics.overview(conn, filters["period"], filters["category_id"]),
            "pipelines": metrics.counts_by_pipeline(conn),
        })


@router.get("/funnel")
async def api_funnel(request: Request) -> JSONResponse:
    filters = resolve_filters(request)
    category_id = filters["category_id"]
    with read_analytics(request) as conn:
        if category_id is None:
            pipelines = metrics.pipelines(conn)
            category_id = pipelines[0]["category_id"] if pipelines else None
        if category_id is None:
            return JSONResponse({"error": "нет воронок в витрине"}, status_code=404)
        period = filters["period"]
        return JSONResponse({
            "category_id": category_id,
            "funnel": metrics.deal_funnel(conn, category_id, period["since"], period["until"]),
            "movement": metrics.stage_movement(
                conn, category_id, period["since"], period["until"],
                filters["department_id"],
            ),
        })


@router.get("/leads")
async def api_leads(request: Request) -> JSONResponse:
    period = resolve_filters(request)["period"]
    with read_analytics(request) as conn:
        return JSONResponse({
            "funnel": metrics.lead_funnel(conn, period["since"], period["until"]),
            "sources": metrics.lead_sources(conn, period["since"], period["until"]),
        })


@router.get("/quality")
async def api_quality(request: Request) -> JSONResponse:
    filters = resolve_filters(request)
    with read_analytics(request) as conn:
        return JSONResponse({
            "quality": metrics.data_quality(conn, filters["category_id"]),
            "etl": metrics.etl_status(conn),
        })


@router.get("/table")
async def api_table(request: Request) -> JSONResponse:
    with read_analytics(request) as conn:
        return JSONResponse(_table_from_request(request, conn))


@router.get("/export.csv")
async def api_export_csv(request: Request) -> StreamingResponse:
    """Выгрузка текущей выборки. Ограничена по числу строк.

    Без потолка одна вкладка могла бы вытянуть в память всю витрину и уронить
    сервис — это и отказ в обслуживании, и выгрузка всей базы одним запросом.
    """
    settings = request.app.state.settings
    params = dict(request.query_params)
    requested = _int_or_none(params.get("size")) or CSV_MAX_ROWS
    params["size"] = str(max(1, min(CSV_MAX_ROWS, requested)))
    params["page"] = "1"

    with read_analytics(request) as conn:
        result = _table_from_request(request, conn, overrides=params)

    buffer = io.StringIO()
    # utf-8-sig: без BOM Excel открывает кириллицу как «Ð¡Ð´ÐµÐ»ÐºÐ°».
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([title for _, title in CSV_COLUMNS])
    for row in result["rows"]:
        row["link"] = crm_link(result["entity"], row["id"], settings.b24_webhook_url)
        writer.writerow([_csv_cell(row.get(key)) for key, _ in CSV_COLUMNS])

    payload = "﻿" + buffer.getvalue()
    filename = f"{result['entity']}s.csv"
    return StreamingResponse(
        io.BytesIO(payload.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _table_from_request(
    request: Request,
    conn,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    params = overrides or dict(request.query_params)
    filters = resolve_filters(request)
    period = filters["period"]
    all_time = params.get("all_time") == "1"
    return metrics.entity_table(
        conn,
        entity=params.get("entity", "deal"),
        category_id=filters["category_id"],
        department_id=filters["department_id"],
        stage_id=params.get("stage") or None,
        assigned_by_id=_int_or_none(params.get("assignee")),
        source_id=params.get("source") or None,
        since=None if all_time else period["since"],
        until=None if all_time else period["until"],
        query=params.get("q") or None,
        only_open=params.get("open") == "1",
        sort=params.get("sort", "created"),
        direction=params.get("dir", "desc"),
        page=_int_or_none(params.get("page")) or 1,
        page_size=_int_or_none(params.get("size")) or 50,
    )


# Символы, с которых Excel и LibreOffice начинают разбирать ячейку как
# формулу. Заголовок сделки пишет человек со стороны — лид приходит с формы
# на сайте, — поэтому «=HYPERLINK(...)» в выгрузке исполнится у того, кто её
# откроет, и утащит соседние ячейки на чужой домен.
_FORMULA_STARTERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> str:
    """Значение для CSV. Текст обезвреживается, числа остаются числами."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        return f"{value:.2f}".replace(".", ",")
    if isinstance(value, int):
        return str(value)

    text = str(value)
    if text.startswith(_FORMULA_STARTERS):
        # Апостроф — штатный для табличных редакторов способ сказать «это
        # текст». Содержимое при этом сохраняется полностью.
        return "'" + text
    return text


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
