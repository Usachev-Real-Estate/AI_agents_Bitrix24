"""Клиент витрины объектов Афины CRM (`GET /api/public/dashboard/*`).

Это не витрина каталога для сайта: каталог статусы, причины снятия и даты
событий намеренно не отдаёт, а дашборду нужны именно они. Ключ при этом тот
же самый — `X-API-Key` со значением `PUBLIC_API_KEY` на стороне Афины.

Ходит только сервер. Браузеру CSP дашборда (`connect-src 'self'`) запрещает
запрос на чужой домен, и это к лучшему: ключ в разметке оказаться не должен.

Ретраев здесь нет намеренно. Это путь отрисовки страницы, а не фоновая
задача: вторая попытка удваивает ожидание человека, а честнее показать
плашку «Афина не ответила» и оставить страницу отзывчивой. По той же
причине таймаут короткий (AFINA_API_TIMEOUT_SECONDS), а не 60 с, как у
фоновых задач.
"""

from __future__ import annotations

import logging
import socket
import ssl
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DASHBOARD_PREFIX = "/api/public/dashboard"

# Потолок страницы на стороне Афины. Просить больше — получить 422.
MAX_PAGE_SIZE = 100

# Пустой ответ Афины, чтобы вызывающий код не разбирал None отдельно.
EMPTY_PAGE: dict[str, Any] = {"items": [], "total": 0, "page": 1, "size": 0,
                              "total_pages": 1}


class AfinaError(RuntimeError):
    """Афина не ответила или ответила отказом.

    Текст исключения показывается человеку на странице, поэтому в нём не
    должно быть ни ключа, ни URL, ни тела ответа — только то, что помогает
    понять, чинить настройку или ждать.
    """


class AfinaNotFound(AfinaError):
    """Объекта с таким id в Афине нет."""


class AfinaClient:
    """Чтение витрины объектов Афины по ключу.

    Клиент одноразовый и без состояния: соединение живёт ровно один запрос.
    Держать общий httpx.Client на процесс в вебе нечем — страница делает
    два-три обращения за отрисовку, и выигрыш не окупает общего состояния.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._base = (base_url or "").rstrip("/")
        self._key = (api_key or "").strip()
        self._timeout = float(timeout or 10.0)

    def summary(self, *, ad_account_id: int | None = None) -> dict[str, Any]:
        """Счётчики: всего, в рекламе, на сайте, сняты с рекламы.

        Счётчики независимы и пересекаются: складывать их нельзя — объект
        может быть одновременно в рекламе и опубликован на сайте.
        """
        payload = self._get("/summary", {"ad_account_id": ad_account_id})
        return payload if isinstance(payload, dict) else {}

    def listings(
        self,
        *,
        page: int = 1,
        size: int = 50,
        search: str | None = None,
        in_ad: bool | None = None,
        is_published: bool | None = None,
        removed_from_ad: bool | None = None,
        statuses: list[str] | None = None,
        ad_account_id: int | None = None,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """Страница объектов: текущее состояние плюс даты из журнала Афины."""
        return self._page("/listings", {
            "page": page,
            "size": _clamp_size(size),
            "search": search,
            "in_ad": in_ad,
            "is_published": is_published,
            "removed_from_ad": removed_from_ad,
            "status": statuses,
            "ad_account_id": ad_account_id,
            "sort": sort,
        })

    def listing(self, flat_id: int) -> dict[str, Any] | None:
        """Один объект по id CRM. None — такого объекта в Афине нет."""
        try:
            payload = self._get(f"/listings/{int(flat_id)}", {}, allow_not_found=True)
        except AfinaNotFound:
            return None
        return _with_utc_dates(payload) if isinstance(payload, dict) else None

    def removals(
        self,
        *,
        page: int = 1,
        size: int = 50,
        search: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """История снятий с рекламы: события журнала, а не текущий статус.

        Границы since/until — календарные дни UTC и обе включающие; это
        контракт Афины, а не наш выбор.
        """
        return self._page("/removals", {
            "page": page,
            "size": _clamp_size(size),
            "search": search,
            "since": since,
            "until": until,
        })

    # --- внутреннее ---

    def _page(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Ответ с пагинацией, у которого даты уже приведены к UTC."""
        payload = self._get(path, params)
        if not isinstance(payload, dict):
            return dict(EMPTY_PAGE)
        items = payload.get("items")
        payload["items"] = [
            _with_utc_dates(item) for item in items if isinstance(item, dict)
        ] if isinstance(items, list) else []
        return payload

    def _get(self, path: str, params: dict[str, Any],
             *, allow_not_found: bool = False) -> Any:
        if not self._key:
            # Сюда не должны доходить: страница проверяет настройку раньше.
            raise AfinaError("Ключ AFINA_API_KEY не задан")
        url = f"{self._base}{DASHBOARD_PREFIX}{path}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(
                    url, params=_query(params), headers={"X-API-Key": self._key},
                )
        except httpx.TimeoutException as exc:
            logger.warning("Афина не ответила за %.0f с: %s", self._timeout, path)
            raise AfinaError(
                f"Афина не ответила за {self._timeout:g} с",
            ) from exc
        except UnicodeEncodeError as exc:
            # Заголовок HTTP — только latin-1. Кириллица в ключе означает
            # опечатку при копировании (русская «с» вместо латинской), и
            # httpx падает на кодировании ещё до запроса, мимо HTTPError.
            logger.warning("В AFINA_API_KEY недопустимые для заголовка символы")
            raise AfinaError("Ключ Афины содержит недопустимые символы: "
                             "проверьте AFINA_API_KEY") from exc
        except httpx.InvalidURL as exc:
            # InvalidURL — единственная ошибка httpx вне иерархии HTTPError.
            # Ловится отдельно, иначе опечатка в AFINA_API_BASE_URL уходит
            # мимо всех except и превращается в пятисотую вместо плашки.
            logger.warning("Неразбираемый AFINA_API_BASE_URL: %s", type(exc).__name__)
            raise AfinaError("Адрес Афины разобрать не удалось: "
                             "проверьте AFINA_API_BASE_URL") from exc
        except httpx.HTTPError as exc:
            # Тип ошибки, а не её текст: в текст httpx кладёт полный URL.
            logger.warning("Афина недоступна: %s (%s)", path, type(exc).__name__)
            raise AfinaError(_transport_reason(exc)) from exc

        if response.status_code >= 400:
            raise _error_for(path, response, allow_not_found=allow_not_found)
        try:
            return response.json()
        except ValueError as exc:
            logger.warning("Афина вернула не JSON на %s", path)
            raise AfinaError("Афина вернула не JSON") from exc


# Даты событий: Афина отдаёт их наивными (без смещения), по соглашению UTC.
DATE_FIELDS = (
    "created_at", "published_to_ads_at", "published_to_site_at",
    "last_renewed_at", "removed_from_ad_at", "removed_at",
)


def _with_utc_dates(item: dict[str, Any]) -> dict[str, Any]:
    """Дописать датам смещение UTC.

    Фильтры шаблонов (`date_ru`, `datetime_ru`) переводят время в московское
    через `astimezone`, а тот для наивной даты берёт часовой пояс сервера.
    На сервере в UTC это сработало бы случайно, а на сервере в Москве
    сместило бы все даты на три часа назад. Смещение проставляем здесь, у
    источника, где точно известно, что оно UTC.
    """
    for field in DATE_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and _is_naive_moment(value):
            item[field] = f"{value}+00:00"
    return item


def _is_naive_moment(value: str) -> bool:
    """Момент времени без смещения. Дата без времени сюда не попадает."""
    if "T" not in value:
        return False
    tail = value[10:]
    return not (tail.endswith("Z") or "+" in tail or "-" in tail)


def _transport_reason(exc: Exception) -> str:
    """Почему запрос не дошёл — словами, которые говорят, что проверять.

    httpx складывает не разрешившееся имя, отказ в соединении и непринятый
    сертификат в один ConnectError. Для человека это три разные поломки с
    тремя разными действиями, и одно «Афина недоступна» на всех отправляет
    его гадать. Настоящая причина лежит в цепочке __cause__.
    """
    if isinstance(exc, httpx.ProxyError):
        return ("Запрос к Афине ушёл через прокси и не дошёл: "
                "проверьте HTTP_PROXY и HTTPS_PROXY в окружении дашборда")
    for cause in _causes(exc):
        if isinstance(cause, ssl.SSLError):
            return ("Сертификат Афины не принят: "
                    "проверьте цепочку сертификатов и время на сервере")
        if isinstance(cause, socket.gaierror):
            return ("Имя из AFINA_API_BASE_URL не разрешается в адрес: "
                    "проверьте его и DNS контейнера дашборда")
        if isinstance(cause, ConnectionRefusedError):
            return ("Афина отказала в соединении: проверьте, что её адрес "
                    "доступен именно с сервера дашборда")
    return "Афина недоступна: с сервера дашборда до неё не достучаться"


def _causes(exc: BaseException):
    """Цепочка причин исключения — httpx прячет настоящую ошибку в ней."""
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _clamp_size(size: Any) -> int:
    """Размер страницы в границах Афины: 1..100."""
    try:
        return max(1, min(MAX_PAGE_SIZE, int(size)))
    except (TypeError, ValueError):
        return 20


def _query(params: dict[str, Any]) -> dict[str, Any]:
    """Параметры для httpx: без пустых значений и с булевыми как true/false."""
    clean: dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value == "" or value == []:
            continue
        clean[key] = "true" if value is True else "false" if value is False else value
    return clean


def _error_for(path: str, response: httpx.Response, *,
               allow_not_found: bool = False) -> AfinaError:
    """Отказ Афины — в лог целиком, человеку — что с этим делать.

    Тело в ответ не попадает: там бывает и эхо запроса, а страница дашборда
    не место для чужой диагностики.
    """
    code = response.status_code
    logger.warning("Афина ответила %s на %s: %s", code, path, response.text[:200])
    if code == 404:
        # 404 бывает двух разных смыслов. У карточки объекта это штатный
        # ответ «такого id нет». На остальных адресах это значит, что самих
        # эндпоинтов витрины на сервере нет — бэкенд Афины не пересобран, — и
        # сказать про это «объект не найден» значит отправить искать не там.
        if allow_not_found:
            return AfinaNotFound("Объект не найден")
        return AfinaError("Афина не знает эндпоинтов витрины: "
                          "проверьте AFINA_API_BASE_URL и версию Афины")
    if code in (401, 403):
        return AfinaError("Афина не приняла ключ: проверьте AFINA_API_KEY")
    if code == 503:
        return AfinaError("На стороне Афины не настроен PUBLIC_API_KEY")
    if code == 422:
        return AfinaError("Афина не приняла параметры запроса")
    return AfinaError(f"Афина ответила ошибкой {code}")
