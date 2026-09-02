"""Раздел «Объекты»: витрина Афины, приведённая к виду страницы.

Здесь всё, что нужно знать шаблону, и ничего про транспорт — за него
отвечает afina.py. Разделение нужно ради тестов: подменить клиент в одном
месте проще, чем поднимать HTTP.

Данные этого раздела не проходят через область видимости витрины
(`analytics/scope.py`): та ограничивает отделы SQL-представлениями над
fact_*/dim_*, а объекты приходят по HTTP и ни в какие представления не
попадают. Отдел брокера Афина называет своим справочником, сопоставить его
с отделами Битрикса нечем. Поэтому раздел открыт только администратору —
конструкция закрывается при сбое, а не открывается. Чтобы открыть его РОПам,
достаточно снять проверку `is_admin` в routes_pages.objects и routes_api,
но тогда РОП увидит брокеров и отделы всей компании.
"""

from __future__ import annotations

from typing import Any

from afina import AfinaClient, AfinaError

SLUG = "objects"

VIEW_LISTINGS = "listings"
VIEW_REMOVALS = "removals"
VIEWS = ((VIEW_LISTINGS, "Объекты"), (VIEW_REMOVALS, "Снятия за период"))

# Статус, при котором причина снятия ещё описывает происходящее. Афина не
# чистит причину при возврате в рекламу, поэтому у объекта «В рекламе»
# может лежать прошлогоднее «Продано другими» — показывать его нельзя.
STATUS_REMOVED = "Снят с рекламы"

# Чипсы над таблицей. Ключ уходит в адрес, kwargs — в запрос к Афине.
LISTING_FILTERS: dict[str, tuple[str, dict[str, bool]]] = {
    "all": ("Все", {}),
    "in_ad": ("Сейчас в рекламе", {"in_ad": True}),
    "published": ("Опубликованы на сайте", {"is_published": True}),
    "removed": ("Сняты с рекламы", {"removed_from_ad": True}),
}
DEFAULT_FILTER = "all"

# Плитки счётчиков: ключ ответа Афины → подпись и пояснение.
SUMMARY_TILES = (
    ("total", "Всего объектов", "Все карточки Афины, включая черновики и копии"),
    ("in_ad", "В рекламе", "Статус «В рекламе» прямо сейчас"),
    ("is_published", "На сайте", "Опубликованы на сайте прямо сейчас"),
    ("removed_from_ad", "Сняты с рекламы", "Статус «Снят с рекламы» прямо сейчас"),
)


def is_configured(settings: Any) -> bool:
    """Настроена ли связь с Афиной.

    Без ключа раздела нет вовсе — ни страницы, ни пункта меню. Пустая
    страница с ошибкой хуже, чем отсутствие пункта: по ней не понять, это
    поломка или так задумано.
    """
    return bool(
        (getattr(settings, "afina_api_key", "") or "").strip()
        and (getattr(settings, "afina_api_base_url", "") or "").strip()
    )


def client_for(settings: Any) -> AfinaClient:
    """Клиент Афины по настройкам.

    Отдельная функция, а не конструктор в маршруте: тест подменяет её и
    получает свой объект, не трогая сеть.
    """
    return AfinaClient(
        settings.afina_api_base_url,
        settings.afina_api_key,
        settings.afina_api_timeout_seconds,
    )


def resolve(params: Any) -> dict[str, Any]:
    """Разобрать параметры адреса раздела."""
    view = params.get("view")
    requested = params.get("filter")
    return {
        "view": view if view in dict(VIEWS) else VIEW_LISTINGS,
        "filter": requested if requested in LISTING_FILTERS else DEFAULT_FILTER,
        "q": (params.get("q") or "").strip(),
        "page": _page(params.get("page")),
        "object_id": _optional_int(params.get("id")),
    }


def load(client: AfinaClient, selected: dict[str, Any], period: dict[str, str],
         page_size: int) -> dict[str, Any]:
    """Собрать данные раздела. Отказ Афины — не исключение, а состояние.

    Страница обязана отрисоваться и когда Афина молчит: человеку нужнее
    понять, что сломалось, чем увидеть пятисотую.
    """
    data: dict[str, Any] = {
        "summary": None, "table": None, "card": None, "error": None,
        # Два разных несчастья: источник отказал или объекта просто нет.
        # Для страницы это одна плашка, а для JSON — 502 против 404, и
        # различать их надо здесь, пока известно, что именно случилось.
        "failed": False, "not_found": False,
    }
    try:
        data["summary"] = client.summary()
        if selected["object_id"] is not None:
            found = client.listing(selected["object_id"])
            # Карточку готовим так же, как строку таблицы: иначе период
            # размещения и причина снятия молча пропадут именно там, где
            # человек и открыл объект, чтобы их прочитать.
            data["card"] = _listing_row(found) if found else None
            if data["card"] is None:
                data["not_found"] = True
                data["error"] = f"Объект {selected['object_id']} в Афине не найден"
        elif selected["view"] == VIEW_REMOVALS:
            data["table"] = _removals_table(client, selected, period, page_size)
        else:
            data["table"] = _listings_table(client, selected, page_size)
    except AfinaError as exc:
        data["failed"] = True
        data["error"] = str(exc)
    return data


def _listings_table(client: AfinaClient, selected: dict[str, Any],
                    page_size: int) -> dict[str, Any]:
    filters = LISTING_FILTERS[selected["filter"]][1]
    payload = client.listings(
        page=selected["page"], size=page_size,
        search=selected["q"] or None, **filters,
    )
    return _table(payload, [_listing_row(item) for item in payload.get("items", [])])


def _removals_table(client: AfinaClient, selected: dict[str, Any],
                    period: dict[str, str], page_size: int) -> dict[str, Any]:
    payload = client.removals(
        page=selected["page"], size=page_size,
        search=selected["q"] or None,
        # start/end — включающие календарные даты, ровно то, что ждёт Афина.
        # since/until витрины сюда не годятся: правая граница там исключающая.
        since=period.get("start"), until=period.get("end"),
    )
    return _table(payload, [_removal_row(item) for item in payload.get("items", [])])


def _table(payload: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "rows": rows,
        "total": _int(payload.get("total")),
        "page": max(1, _int(payload.get("page")) or 1),
        "pages": max(1, _int(payload.get("total_pages")) or 1),
    }


def _listing_row(item: dict[str, Any]) -> dict[str, Any]:
    """Строка таблицы объектов.

    Копии не схлопываем: одна квартира в двух рекламных аккаунтах — две
    строки, и это не дубль, а два размещения, каждое со своей судьбой.
    """
    row = dict(item)
    row["removal"] = removal_of(item)
    row["ad_period"] = _ad_period(item)
    return row


def _removal_row(item: dict[str, Any]) -> dict[str, Any]:
    """Строка истории снятий.

    Здесь причина берётся на момент события, поэтому показываем её всегда —
    в отличие от карточки, где она может быть устаревшей.
    """
    row = dict(item)
    row["removal"] = {
        "reason": item.get("removal_reason"),
        "category": item.get("removal_reason_category"),
        "comment": item.get("removal_comment"),
    }
    return row


def removal_of(item: dict[str, Any]) -> dict[str, Any] | None:
    """Причина снятия, если она ещё описывает текущее состояние.

    Афина отдаёт причину как есть, из карточки, и при возврате объекта в
    рекламу её не стирает. Показать её рядом со статусом «В рекламе» значит
    напечатать неправду, поэтому за пределами статуса «Снят с рекламы»
    причины у нас нет.
    """
    if (item.get("status") or "").strip() != STATUS_REMOVED:
        return None
    if not (item.get("removal_reason") or item.get("removal_reason_category")):
        return None
    return {
        "reason": item.get("removal_reason"),
        "category": item.get("removal_reason_category"),
        "comment": item.get("removal_comment"),
    }


def _ad_period(item: dict[str, Any]) -> str | None:
    """Текущий период размещения как есть.

    ad_date_from/ad_date_to Афина хранит строками «01.08.2026» и ничем их не
    проверяет. Разбирать их в дату — значит однажды упасть на мусоре ради
    формата, который и так уже русский.
    """
    start = (item.get("ad_date_from") or "").strip()
    end = (item.get("ad_date_to") or "").strip()
    if start and end:
        return f"{start} — {end}"
    return start or end or None


def _page(value: Any) -> int:
    number = _optional_int(value)
    return number if number and number > 0 else 1


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
