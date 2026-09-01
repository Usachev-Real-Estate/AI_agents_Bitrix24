"""Bitrix24 REST-клиент для ETL витрины.

Почему не fast_bitrix24, который используется в остальном проекте:

1. ETL — самый тяжёлый потребитель API в системе, и ему нужен явный контроль
   над темпом запросов и повторами. В проекте rate limiting и retry
   отсутствуют вовсе, а батчи fast_bitrix24 на больших объёмах ловят
   OPERATION_TIME_LIMIT (см. комментарий в src/contact_source_lock.py:134).
2. Сырой httpx сохраняет camelCase-параметры, без которых не работает
   crm.stagehistory.list (entityTypeId). Это тот же приём, что в
   src/exclusive_expiry.py:47.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

logger = logging.getLogger(__name__)

# Лимит портала — 2 запроса в секунду. Берём с небольшим запасом вниз:
# упереться в лимит дороже, чем потерять доли секунды на прогоне.
DEFAULT_RPS = 2.0
DEFAULT_TIMEOUT = 60.0
MAX_ATTEMPTS = 4
PAGE_SIZE = 50

# Ошибки, которые лечатся повтором. Всё остальное — баг в запросе,
# и повторять его значит молча жечь лимит портала.
RETRYABLE_ERRORS = frozenset({
    "QUERY_LIMIT_EXCEEDED",
    "OPERATION_TIME_LIMIT",
    "INTERNAL_SERVER_ERROR",
    "ERROR_UNEXPECTED_ANSWER",
})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class BitrixError(RuntimeError):
    """Ошибка Bitrix REST, которую не имеет смысла повторять."""

    def __init__(self, method: str, code: str, description: str) -> None:
        super().__init__(f"{method}: {code} — {description}")
        self.method = method
        self.code = code
        self.description = description


def to_utc_iso(value: Any) -> str | None:
    """Привести дату Bitrix к UTC ISO-8601.

    Bitrix отдаёт даты в таймзоне портала («2026-08-21T10:30:00+03:00»), иногда
    без времени («2026-08-21»). Хранить их как есть — значит получить сдвиг на
    три часа в суточных срезах: сделка, созданная в 01:00 МСК, попадёт во
    вчерашний день.

    Returns:
        Строка UTC ISO-8601 или None, если значение пустое / не разбирается.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("0000"):
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            logger.debug("Не разобрана дата Bitrix: %r", value)
            return None
    if dt.tzinfo is None:
        # Дата без смещения приходит только от «date»-полей портала (МСК).
        dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
    return dt.astimezone(timezone.utc).isoformat()


def utc_now_iso() -> str:
    """Текущий момент как UTC ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


class RateLimiter:
    """Простой токен-бакет, потокобезопасный."""

    def __init__(self, rps: float = DEFAULT_RPS) -> None:
        self._min_interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class BitrixClient:
    """Клиент входящего вебхука с ограничением темпа и повторами."""

    def __init__(
        self,
        webhook_url: str,
        *,
        rps: float = DEFAULT_RPS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base = webhook_url.rstrip("/") + "/"
        self._limiter = RateLimiter(rps)
        self._client = httpx.Client(timeout=timeout)
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BitrixClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Один REST-вызов с повторами. Возвращает содержимое `result`."""
        envelope = self.call_envelope(method, params)
        if isinstance(envelope, dict) and "result" in envelope:
            return envelope["result"]
        return envelope

    def call_envelope(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Как call(), но отдаёт весь конверт ответа.

        Пагинация через start/next живёт в конверте, а не в result — без
        доступа к нему постраничная выгрузка молча обрывается на первых 50
        записях.
        """
        payload = params or {}
        last_exc: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._limiter.acquire()
            self.request_count += 1
            try:
                response = self._client.post(self._base + method, json=payload)
            except httpx.RequestError as exc:
                last_exc = exc
                self._backoff(attempt, method, f"сеть: {exc}")
                continue

            if response.status_code in RETRYABLE_STATUS:
                last_exc = BitrixError(method, str(response.status_code), response.text[:200])
                self._backoff(attempt, method, f"HTTP {response.status_code}")
                continue

            try:
                data = response.json()
            except ValueError as exc:
                last_exc = exc
                self._backoff(attempt, method, "ответ не JSON")
                continue

            if isinstance(data, dict) and data.get("error"):
                code = str(data.get("error"))
                description = str(data.get("error_description") or "")
                if code in RETRYABLE_ERRORS:
                    last_exc = BitrixError(method, code, description)
                    self._backoff(attempt, method, code)
                    continue
                raise BitrixError(method, code, description)

            if response.status_code >= 400:
                raise BitrixError(method, str(response.status_code), response.text[:200])

            return data

        raise BitrixError(
            method,
            "RETRIES_EXHAUSTED",
            f"{MAX_ATTEMPTS} попыток исчерпаны: {last_exc}",
        )

    @staticmethod
    def _backoff(attempt: int, method: str, reason: str) -> None:
        if attempt >= MAX_ATTEMPTS:
            return
        delay = 2 ** (attempt - 1)
        logger.warning(
            "%s: попытка %d/%d не удалась (%s), повтор через %ds",
            method, attempt, MAX_ATTEMPTS, reason, delay,
        )
        time.sleep(delay)

    def list_by_id(
        self,
        method: str,
        params: dict[str, Any],
        *,
        start_id: int = 0,
    ) -> Iterator[dict[str, Any]]:
        """Пагинация по ID — быстрый путь для больших списков.

        Обычная пагинация через `start` заставляет Bitrix считать COUNT на
        каждой странице, и на десятках тысяч записей выгрузка вырождается в
        часы. Приём `start: -1` + `filter[>ID]` + `order[ID]=ASC` — штатный
        способ портала выгружать большие списки: COUNT отключается, каждая
        страница берётся по индексу.
        """
        last_id = start_id
        while True:
            page_params = dict(params)
            page_filter = dict(page_params.get("filter") or {})
            page_filter[">ID"] = last_id
            page_params["filter"] = page_filter
            page_params["order"] = {"ID": "ASC"}
            page_params["start"] = -1

            rows = _as_records(self.call(method, page_params))
            if not rows:
                return

            for row in rows:
                yield row

            new_last = _coerce_int(rows[-1].get("ID"))
            if new_last <= last_id:
                # Без этой проверки битый ответ портала крутит цикл вечно.
                logger.warning(
                    "%s: пагинация по ID встала на ID=%s — останавливаемся",
                    method, last_id,
                )
                return
            last_id = new_last
            if len(rows) < PAGE_SIZE:
                return

    def list_paged(
        self,
        method: str,
        params: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        """Пагинация через start/next со стоп-гардом.

        Для методов без пригодного для сортировки ID (crm.stagehistory.list).
        Стоп-гард скопирован из домашнего стиля проекта
        (src/deal_source_lock.py:74-84).
        """
        start = 0
        while True:
            page_params = dict(params)
            page_params["start"] = start
            raw = self.call_envelope(method, page_params)
            rows = _as_records(raw)
            for row in rows:
                yield row

            nxt = raw.get("next") if isinstance(raw, dict) else None
            if nxt is None or not rows:
                return
            if int(nxt) <= start:
                logger.warning(
                    "%s: пагинация встала на start=%s (next=%s) — останавливаемся",
                    method, start, nxt,
                )
                return
            start = int(nxt)


def _as_records(payload: Any) -> list[dict[str, Any]]:
    """Развернуть ответ Bitrix в список словарей.

    Форма ответа зависит от метода: crm.deal.list кладёт список прямо в
    result, crm.stagehistory.list — в result.items, crm.category.list — в
    result.categories.
    """
    LIST_KEYS = ("items", "result", "tasks", "categories", "users")

    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in LIST_KEYS:
        inner = payload.get(key)
        if isinstance(inner, list):
            return [row for row in inner if isinstance(row, dict)]
        if isinstance(inner, dict):
            for nested_key in LIST_KEYS:
                nested = inner.get(nested_key)
                if isinstance(nested, list):
                    return [row for row in nested if isinstance(row, dict)]
    return []


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
