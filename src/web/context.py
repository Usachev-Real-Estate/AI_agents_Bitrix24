"""Общий контекст страниц: соединение с витриной, фильтры, навигация."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from fastapi import Request

import metrics
import objects
from links import crm_link
from scope import ROLE_ADMIN, Scope, scoped_session

# Страницы, умеющие фильтровать по отделу. Остальным параметр не передаётся:
# иначе он молча остаётся в адресе, страница его игнорирует, и человек видит
# «Отдел Волковой» в ссылке при данных по всей компании.
DEPARTMENT_AWARE_PAGES = frozenset({"movement", "table"})

# Разделы, которые видит только администратор.
#
# Решение агентства от 10.09. Ограничение здесь не про секретность — данные
# РОПу и так сужены до его отдела на самом соединении, — а про то, чем этот
# экран является. «Таблица» и «Качество данных» — инструменты сопровождения
# витрины: пустые поля, дубли, карточки без ответственного, выгрузка всего
# подряд. «Люди» — сравнение сотрудников между собой, и разговор этот
# ведётся не на уровне отдела.
#
# Множество ОДНО и на меню, и на маршруты. Спрятать пункт меню — не защита:
# адрес набирается руками. Закрыть маршрут, но оставить пункт, — приглашение
# на отказ. Два списка однажды разойдутся, и разойдутся молча; проверяется
# это тестом, перебирающим маршруты.
ADMIN_ONLY_PAGES = frozenset({"people", "table", "quality", "users"})

NAV = [
    # «План на день» стоит первым: с этим вопросом дашборд и открывают.
    # Сводные экраны отвечают на другой — «как дела», — и ждут своей очереди.
    ("today", "План на день"),
    ("pulse", "Пульс"),
    ("leads", "Лиды"),
    ("deals", "Сделки"),
    ("movement", "Движение"),
    ("clients", "Клиенты"),
    # «Исключения» сразу за «Клиентами»: это тот же портфель, разрезанный
    # по претензиям, и ходят между ними подряд.
    ("exceptions", "Исключения"),
    ("people", "Люди"),
    (objects.SLUG, "Объекты"),
    ("table", "Таблица"),
    ("quality", "Качество данных"),
    ("users", "Доступы"),
]


@contextmanager
def read_analytics(request: Request) -> Iterator[Any]:
    """Витрина, суженная до того, что разрешено видеть этому пользователю.

    Единственный способ читать данные из веба. Область видимости берётся из
    сессии и задаётся на самом соединении, а не подставляется в запросы:
    метрики обращаются только к представлениям v_deal / v_lead /
    v_stage_event / v_user, которых на неограниченном соединении просто нет.
    Забыть ограничение в новом запросе невозможно — забывать нечего.

    Витрина при этом открыта строго на чтение: единственный её писатель — ETL.
    """
    with scoped_session(scope_for(request)) as conn:
        yield conn


def scope_for(request: Request) -> Scope:
    """Область видимости по пользователю сессии."""
    return Scope.for_user(getattr(request.state, "user", None))


def visible_department_ids(request: Request) -> tuple[int, ...] | None:
    """Отделы, доступные пользователю. None — все (администратор)."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == ROLE_ADMIN:
        return None
    return tuple(user.get("department_ids") or ())


def resolve_filters(request: Request) -> dict[str, Any]:
    """Разобрать общие параметры строки запроса: период, воронка, отдел."""
    params = request.query_params
    period = metrics.resolve_period(
        params.get("period"), params.get("start"), params.get("end"),
    )
    requested_department = _optional_int(params.get("department"))
    return {
        "period": period,
        "category_id": _optional_int(params.get("category")),
        "department_id": _clamp_department(request, requested_department),
    }


def _clamp_department(request: Request, requested: int | None) -> int | None:
    """Подрезать выбранный отдел по правам пользователя.

    Само по себе это не защита — данные уже ограничены на уровне соединения,
    и чужой отдел в адресе просто дал бы пустую страницу. Подрезка нужна,
    чтобы РОП не увидел в фильтре чужое название отдела и не решил, что
    смотрит его данные.
    """
    allowed = visible_department_ids(request)
    if allowed is None or requested is None:
        return requested
    return requested if requested in allowed else None


def _optional_int(value: str | None) -> int | None:
    """None для «все» и для мусора в строке запроса."""
    if value in (None, "", "all"):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def visible_nav(request: Request, settings: Any, is_admin: bool,
                active: str, department_id: int | None) -> list[dict[str, Any]]:
    """Навигация под конкретного пользователя: что видно и куда ведёт.

    «Объекты» появляются, когда связь с Афиной настроена и пользователю есть
    что там увидеть: администратору — всё, РОПу — свой отдел, и только если
    этот отдел сопоставлен с отделом Афины. Пункт меню, ведущий на отказ или
    на плашку «не настроено», хуже отсутствующего: по нему нельзя понять,
    поломка это или так и задумано.

    По той же причине из меню РОПа убраны административные разделы: пункт,
    который всегда отвечает отказом, читается как поломка дашборда.
    """
    allowed = objects.allowed_departments(
        getattr(request.state, "user", None),
        getattr(settings, "afina_department_map", {}) or {},
    )
    has_objects = objects.is_configured(settings) and (allowed is None or allowed)
    shown = [
        item for item in NAV
        if (has_objects or item[0] != objects.SLUG)
        and (is_admin or item[0] not in ADMIN_ONLY_PAGES)
    ]
    return [
        {"slug": slug, "label": label,
         "query": _nav_query(request, slug, active, department_id)}
        for slug, label in shown
    ]


# Параметры, которые принадлежат конкретному разделу, а не всему дашборду.
# Период, воронка и отдел общие — их переход между разделами сохраняет.
SECTION_PARAMS = (
    "q", "page", "id", "view", "filter", "entity", "stage", "assignee",
    "source", "open", "all_time", "sort", "dir",
)


def _nav_query(request: Request, slug: str, active: str,
               department_id: int | None) -> str:
    """Строка запроса для пункта меню.

    Уходя из раздела, его собственные параметры надо оставить: «Объекты»
    получали из «Таблицы» чужие page=3 и q=Иванов и открывались на пустой
    третьей странице с непонятно откуда взявшимся поиском. Внутри своего
    раздела параметры сохраняются — иначе клик по текущему пункту молча
    сбрасывал бы уже настроенные фильтры.
    """
    overrides: dict[str, Any] = {
        "department": department_id if slug in DEPARTMENT_AWARE_PAGES else None,
    }
    if slug != active:
        overrides.update({name: None for name in SECTION_PARAMS})
    return query_string(request, **overrides)


def base_context(request: Request, active: str = "") -> dict[str, Any]:
    """Контекст, нужный каждой странице: пользователь, период, список воронок."""
    config = request.app.state.config
    settings = request.app.state.settings
    filters = resolve_filters(request)

    user = getattr(request.state, "user", None)
    is_admin = (user or {}).get("role") == ROLE_ADMIN
    with read_analytics(request) as conn:
        pipelines = metrics.pipelines(conn)
        departments = metrics.departments_options(conn)
        status = metrics.etl_status(conn)

    return {
        "request": request,
        "user": user,
        "is_admin": is_admin,
        "scope_label": scope_for(request).describe(),
        "base_path": config.base_path,
        "nav": visible_nav(
            request, settings, is_admin, active, filters["department_id"],
        ),
        "active": active,
        "period": filters["period"],
        "period_presets": metrics.PERIOD_PRESETS,
        "category_id": filters["category_id"],
        "department_id": filters["department_id"],
        "pipelines": pipelines,
        "departments": departments,
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
