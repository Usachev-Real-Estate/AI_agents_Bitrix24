"""Раздел «Объекты»: витрина Афины, приведённая к виду страницы.

Здесь всё, что нужно знать шаблону, и ничего про транспорт — за него
отвечает afina.py. Разделение нужно ради тестов: подменить клиент в одном
месте проще, чем поднимать HTTP.

Считаем у себя, а не запросами. Афина умеет фильтровать только по статусу,
сайту и рекламному аккаунту: ни отдела, ни брокера, ни дат в параметрах
`/listings` нет, хотя все эти поля в карточке лежат. Поэтому раз в минуту
берётся снимок рабочих объектов — «в рекламе», «на сайте» и «снятые с
рекламы», меньше тысячи карточек, — а отделы, брокеры и периоды считаются по
нему локально. Черновики и архив в снимок не входят: ни в одной запрошенной
цифре они не участвуют, а всего карточек в Афине 38 тысяч, и тянуть их
постранично нельзя.

Данные этого раздела не проходят через область видимости витрины
(`analytics/scope.py`): та ограничивает отделы SQL-представлениями над
fact_*/dim_*, а объекты приходят по HTTP и ни в какие представления не
попадают. Отдел брокера Афина называет своим справочником, сопоставить его
с отделами Битрикса нечем — фильтр по отделу здесь свой и правами не
управляет. Поэтому раздел открыт только администратору: конструкция
закрывается при сбое, а не открывается.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from afina import AfinaClient, AfinaError

SLUG = "objects"

VIEW_LISTINGS = "listings"
VIEW_REMOVALS = "removals"
VIEWS = ((VIEW_LISTINGS, "Объекты"), (VIEW_REMOVALS, "Снятия за период"))

# Статусы Афины. Причину снятия она не чистит при возврате в рекламу, поэтому
# у объекта «В рекламе» может лежать прошлогоднее «Продано другими».
STATUS_REMOVED = "Снят с рекламы"
STATUS_IN_AD = "В рекламе"

# site_sync_status = unpublished — объект снят с сайта. Это состояние, а не
# событие: даты снятия с сайта в Афине нет ни в поле, ни в журнале, поэтому
# «снят с сайта» за период посчитать нечем и плитка всегда про «сейчас».
SITE_UNPUBLISHED = "unpublished"

# Чипы над таблицей. Чип выбирает, о чём таблица ниже и какая метрика
# считается за период; сами плитки он не сужает — иначе «в рекламе» под
# фильтром «в рекламе» было бы равно выборке, а «сняты» — нулю.
LISTING_FILTERS: dict[str, tuple[str, dict[str, bool]]] = {
    "all": ("Все", {}),
    "in_ad": ("Сейчас в рекламе", {"in_ad": True}),
    "published": ("Опубликованы на сайте", {"is_published": True}),
    "removed": ("Сняты с рекламы", {"removed_from_ad": True}),
}
DEFAULT_FILTER = "all"

# Что чип считает «своим» событием: поле даты и подпись метрики за период.
FILTER_METRICS: dict[str, tuple[str, str, str]] = {
    "in_ad": ("published_to_ads_at", "Выставлено в рекламу", "in_ad"),
    "published": ("published_to_site_at", "Выставлено на сайт", "is_published"),
    "removed": ("removed_from_ad_at", "Снято с рекламы", "removed_from_ad"),
}

# Плитки по всей базе: ключ ответа `/summary` → подпись и пояснение.
SUMMARY_TILES = (
    ("total", "Всего объектов", "Все карточки Афины, включая черновики и копии"),
    ("in_ad", "В рекламе", "Статус «В рекламе» прямо сейчас"),
    ("is_published", "На сайте", "Опубликованы на сайте прямо сейчас"),
    ("removed_from_ad", "Сняты с рекламы", "Статус «Снят с рекламы» прямо сейчас"),
)

# Плитки состояния под каждый чип, кроме «за период» — её добавляем отдельно,
# потому что подпись у неё меняется вместе с выбранным периодом.
FILTER_TILES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "in_ad": (
        ("in_ad", "В рекламе", "Статус «В рекламе» прямо сейчас"),
        ("is_published", "На сайте", "Опубликованы на сайте прямо сейчас"),
        ("removed_from_ad", "Сняты с рекламы", "Статус «Снят с рекламы» сейчас"),
    ),
    "published": (
        ("is_published", "На сайте", "Опубликованы на сайте прямо сейчас"),
        ("site_removed", "Сняты с сайта", "Сняты с сайта сейчас; даты снятия Афина не хранит"),
        ("in_ad", "В рекламе", "Статус «В рекламе» прямо сейчас"),
    ),
    "removed": (
        ("removed_from_ad", "Сняты с рекламы", "Статус «Снят с рекламы» сейчас"),
        ("is_published", "На сайте", "Из них всё ещё опубликованы на сайте"),
        ("site_removed", "Сняты с сайта", "Сняты с сайта сейчас; даты снятия Афина не хранит"),
    ),
}

# Снимок живёт минуту: за это время чипы, отделы и периоды переключаются без
# единого запроса к Афине, а данные не успевают устареть настолько, чтобы это
# было заметно на счётчиках объявлений.
SNAPSHOT_TTL_SECONDS = 60.0

# Предохранитель на случай, если рабочих объектов окажется кратно больше
# ожидаемой тысячи. Обрезку видно на странице, молчать о ней нельзя.
SNAPSHOT_CAP = 5000

_cache: dict[str, Any] = {}


def reset_cache() -> None:
    """Забыть снимок. Нужна тестам и ручной проверке после выкладки."""
    _cache.clear()


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
        "department": (params.get("department") or "").strip(),
        "broker": (params.get("broker") or "").strip(),
        "page": _page(params.get("page")),
        "object_id": _optional_int(params.get("id")),
    }


# --- снимок рабочих объектов ---


def snapshot(client: AfinaClient) -> dict[str, Any]:
    """Рабочие объекты Афины одним куском, не чаще раза в минуту.

    Объединяем три выборки по id: объект бывает одновременно в рекламе и на
    сайте, и без слияния он посчитался бы дважды.
    """
    cached = _cache.get("snapshot")
    if cached and time.monotonic() - cached["at"] < SNAPSHOT_TTL_SECONDS:
        return cached

    merged: dict[int, dict[str, Any]] = {}
    for filters in ({"in_ad": True}, {"is_published": True}, {"removed_from_ad": True}):
        for item in client.fetch_all(cap=SNAPSHOT_CAP, **filters):
            flat_id = item.get("id")
            if flat_id is not None:
                merged[flat_id] = item

    items = [_listing_row(item) for item in merged.values()]
    fresh = {
        "at": time.monotonic(),
        "items": items,
        # Обрезка возможна на каждой из трёх выборок, поэтому сравниваем с
        # потолком, а не с суммой: точное число здесь и не нужно.
        "truncated": len(merged) >= SNAPSHOT_CAP,
    }
    _cache["snapshot"] = fresh
    return fresh


def departments_of(items: list[dict[str, Any]]) -> list[str]:
    """Отделы, которые реально встречаются в снимке, по алфавиту.

    Справочник отделов Афина по API не отдаёт, да он и не нужен: пустой
    пункт в фильтре ничего не выбирает и только мешает.
    """
    return sorted({(item.get("department_name") or "").strip()
                   for item in items} - {""})


def scope(items: list[dict[str, Any]], selected: dict[str, Any]) -> list[dict[str, Any]]:
    """Сузить снимок отделом, брокером и поиском.

    Чип сюда не входит намеренно: он выбирает метрику и содержимое таблицы,
    а плитки описывают отдел целиком — иначе «Сняты с рекламы» под фильтром
    «в рекламе» всегда показывали бы ноль.
    """
    rows = items
    if selected["department"]:
        rows = [r for r in rows
                if (r.get("department_name") or "").strip() == selected["department"]]
    if selected["broker"]:
        rows = [r for r in rows
                if (r.get("assignee_name") or "").strip() == selected["broker"]]
    if selected["q"]:
        rows = [r for r in rows if _matches(r, selected["q"])]
    return rows


def _matches(item: dict[str, Any], needle: str) -> bool:
    """Поиск по тем же правилам, что у Афины: цифры — это id, иначе подстрока."""
    value = needle.strip()
    if value.isdigit():
        return item.get("id") == int(value)
    haystack = " ".join(
        str(item.get(field) or "")
        for field in ("address", "complex_name", "title", "district")
    ).lower()
    return value.lower() in haystack


# --- плитки ---


def tiles_for(items: list[dict[str, Any]], selected: dict[str, Any],
              period: dict[str, str]) -> list[dict[str, Any]]:
    """Плитки под выбранный чип: состояние сейчас плюс метрика за период."""
    tiles = [
        {"label": label, "value": _count(items, key), "hint": hint}
        for key, label, hint in FILTER_TILES[selected["filter"]]
    ]
    field, title, _ = FILTER_METRICS[selected["filter"]]
    tiles.append({
        "label": f"{title} за период",
        "value": len([r for r in items if _in_period(r.get(field), period)]),
        "hint": f"Событие попало в период «{period.get('label', '')}»",
    })
    return tiles


def summary_tiles(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Плитки по всей базе — единственное, что считает сама Афина."""
    return [
        {"label": label, "value": summary.get(key), "hint": hint}
        for key, label, hint in SUMMARY_TILES
    ]


def _count(items: list[dict[str, Any]], key: str) -> int:
    return sum(1 for item in items if item.get(key))


def _in_period(value: Any, period: dict[str, str]) -> bool:
    """Попало ли событие в период витрины [since, until).

    Правая граница исключающая — так же, как у остального дашборда: иначе
    последний день либо теряется, либо задваивается при сравнении периодов.
    """
    moment = _moment(value)
    since, until = _moment(period.get("since")), _moment(period.get("until"))
    if moment is None or since is None or until is None:
        return False
    return since <= moment < until


def _moment(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


# --- таблица по отделам и брокерам ---


def breakdown(items: list[dict[str, Any]], selected: dict[str, Any],
              period: dict[str, str]) -> dict[str, Any]:
    """Отделы и брокеры со счётчиками, отсортированные по метрике чипа.

    Строка на брокера, отдел — заголовком группы: так видно и вклад
    человека, и итог отдела, а сортировка по одной колонке не рвёт группы.
    """
    field, title, metric = FILTER_METRICS[selected["filter"]]
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        department = (item.get("department_name") or "").strip() or "Без отдела"
        broker = (item.get("assignee_name") or "").strip() or "Без брокера"
        group = groups.setdefault(department, {"department": department, "brokers": {}})
        row = group["brokers"].setdefault(broker, _empty_row(broker))
        row["in_ad"] += 1 if item.get("in_ad") else 0
        row["is_published"] += 1 if item.get("is_published") else 0
        row["removed_from_ad"] += 1 if item.get("removed_from_ad") else 0
        row["site_removed"] += 1 if item.get("site_removed") else 0
        row["period"] += 1 if _in_period(item.get(field), period) else 0

    rendered = []
    for group in groups.values():
        brokers = sorted(group["brokers"].values(),
                         key=lambda r: (-r[metric], -r["period"], r["broker"]))
        rendered.append({
            "department": group["department"],
            "brokers": brokers,
            "totals": _totals(brokers),
        })
    rendered.sort(key=lambda g: (-g["totals"][metric], g["department"]))
    return {
        "groups": rendered,
        "metric": metric,
        "period_label": f"{title} за период",
        "totals": _totals([b for g in rendered for b in g["brokers"]]),
    }


COUNT_KEYS = ("in_ad", "is_published", "removed_from_ad", "site_removed", "period")


def _empty_row(broker: str) -> dict[str, Any]:
    return dict({"broker": broker}, **{key: 0 for key in COUNT_KEYS})


def _totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {key: sum(row[key] for row in rows) for key in COUNT_KEYS}


# --- сборка данных страницы ---


def load(client: AfinaClient, selected: dict[str, Any], period: dict[str, str],
         page_size: int) -> dict[str, Any]:
    """Собрать данные раздела. Отказ Афины — не исключение, а состояние.

    Страница обязана отрисоваться и когда Афина молчит: человеку нужнее
    понять, что сломалось, чем увидеть пятисотую.
    """
    data: dict[str, Any] = {
        "tiles": [], "scoped": False, "table": None, "card": None,
        "breakdown": None, "departments": [], "objects": None,
        "truncated": False, "error": None,
        # Два разных несчастья: источник отказал или объекта просто нет.
        # Для страницы это одна плашка, а для JSON — 502 против 404, и
        # различать их надо здесь, пока известно, что именно случилось.
        "failed": False, "not_found": False,
    }
    try:
        if selected["object_id"] is not None:
            found = client.listing(selected["object_id"])
            # Карточку готовим так же, как строку таблицы: иначе период
            # размещения и причина снятия молча пропадут именно там, где
            # человек и открыл объект, чтобы их прочитать.
            data["card"] = _listing_row(found) if found else None
            if data["card"] is None:
                data["not_found"] = True
                data["error"] = f"Объект {selected['object_id']} в Афине не найден"
            data["tiles"] = summary_tiles(client.summary())
        elif selected["view"] == VIEW_REMOVALS:
            data["tiles"] = summary_tiles(client.summary())
            data["table"] = _removals_table(client, selected, period, page_size)
        elif selected["filter"] == DEFAULT_FILTER:
            # «Все» — единственный срез, который снимком не покрыть: карточек
            # 38 тысяч, и постранично их не собрать. Отдаём то, что считает
            # сама Афина, и не предлагаем ни отделов, ни периода.
            data["tiles"] = summary_tiles(client.summary())
        else:
            data.update(_workspace(client, selected, period, page_size))
            data["scoped"] = True
    except AfinaError as exc:
        data["failed"] = True
        data["error"] = str(exc)
    return data


def _workspace(client: AfinaClient, selected: dict[str, Any],
               period: dict[str, str], page_size: int) -> dict[str, Any]:
    """Плитки, разбивка и — если выбран брокер — его объекты."""
    taken = snapshot(client)
    scoped = scope(taken["items"], selected)
    result = {
        "tiles": tiles_for(scoped, selected, period),
        "departments": departments_of(taken["items"]),
        "breakdown": breakdown(scoped, selected, period),
        "truncated": taken["truncated"],
        "objects": None,
    }
    if selected["broker"]:
        # Строка брокера без возможности раскрыть — тупик: видно число, но
        # не видно, из каких объектов оно сложилось.
        result["objects"] = _objects_table(scoped, selected, page_size)
    return result


def _objects_table(items: list[dict[str, Any]], selected: dict[str, Any],
                   page_size: int) -> dict[str, Any]:
    """Объекты выбранного брокера, постранично по снимку."""
    metric = FILTER_METRICS[selected["filter"]][2]
    rows = sorted((r for r in items if r.get(metric)),
                  key=lambda r: -(r.get("id") or 0))
    size = max(1, page_size)
    pages = max(1, -(-len(rows) // size))
    page = min(max(1, selected["page"]), pages)
    start = (page - 1) * size
    return {"rows": rows[start:start + size], "total": len(rows),
            "page": page, "pages": pages}


def _removals_table(client: AfinaClient, selected: dict[str, Any],
                    period: dict[str, str], page_size: int) -> dict[str, Any]:
    payload = client.removals(
        page=selected["page"], size=page_size,
        search=selected["q"] or None,
        # start/end — включающие календарные даты, ровно то, что ждёт Афина.
        # since/until витрины сюда не годятся: правая граница там исключающая.
        since=period.get("start"), until=period.get("end"),
    )
    rows = [_removal_row(item) for item in payload.get("items", [])]
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
    # Афина отдаёт снятие с сайта состоянием, а не флагом; приводим к флагу,
    # чтобы считать его так же, как остальные срезы.
    row["site_removed"] = (item.get("site_sync_status") or "").strip() == SITE_UNPUBLISHED
    row["removed_from_ad"] = (item.get("status") or "").strip() == STATUS_REMOVED
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
