"""Общий контекст страниц: соединение с витриной, фильтры, навигация."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from fastapi import Request

import metrics
from links import crm_link
from schema import analytics_session

NAV = [
    ("", "Обзор"),
    ("leads", "Лиды"),
    ("deals", "Сделки"),
    ("movement", "Движение"),
    ("people", "Люди"),
    ("table", "Таблица"),
    ("quality", "Качество данных"),
]


@contextmanager
def read_analytics() -> Iterator[Any]:
    """Витрина — строго на чтение.

    Единственный писатель витрины — ETL. Веб открывает её в режиме ro, и это
    не декларация: SQLite откажет в записи на уровне драйвера.
    """
    with analytics_session(readonly=True) as conn:
        yield conn


def resolve_filters(request: Request) -> dict[str, Any]:
    """Разобрать общие параметры строки запроса: период и воронка."""
    params = request.query_params
    period = metrics.resolve_period(
        params.get("period"), params.get("start"), params.get("end"),
    )
    category = params.get("category")
    category_id: int | None = None
    if category not in (None, "", "all"):
        try:
            category_id = int(category)
        except ValueError:
            category_id = None
    return {"period": period, "category_id": category_id}


def base_context(request: Request, active: str = "") -> dict[str, Any]:
    """Контекст, нужный каждой странице: пользователь, период, список воронок."""
    config = request.app.state.config
    settings = request.app.state.settings
    filters = resolve_filters(request)

    with read_analytics() as conn:
        pipelines = metrics.pipelines(conn)
        status = metrics.etl_status(conn)

    return {
        "request": request,
        "user": getattr(request.state, "user", None),
        "base_path": config.base_path,
        "nav": NAV,
        "active": active,
        "period": filters["period"],
        "period_presets": metrics.PERIOD_PRESETS,
        "category_id": filters["category_id"],
        "pipelines": pipelines,
        "etl_lag_minutes": status["lag_minutes"],
        "etl_window_since": status["window_since"],
        "crm_link": lambda entity, entity_id: crm_link(
            entity, entity_id, settings.b24_webhook_url,
        ),
    }


def query_string(request: Request, **overrides: Any) -> str:
    """Собрать строку запроса, сохранив текущие фильтры.

    Нужна для ссылок «разложить до карточек»: переход в таблицу обязан
    сохранить период и воронку, иначе пользователь увидит другие числа, чем
    те, по которым кликнул.
    """
    params = dict(request.query_params)
    for key, value in overrides.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = str(value)
    from urllib.parse import urlencode

    return urlencode(params)
