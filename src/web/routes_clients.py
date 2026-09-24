"""Ручки книги клиентов (раздел 7.2 ТЗ).

Живут под `{base_path}/api`, и это не косметика: `RequireAuthMiddleware`
отказывает по-разному в зависимости от пути. Под `/api` анонимный запрос
получает 401 и пустое тело, а вне его — 303 на форму входа, то есть
программа-читатель вместо отказа получила бы HTML и разбирала бы его как
данные.

Все три ручки читают только через область видимости (`clients_scope`): на
соединении без неё запрос упадёт на отсутствующем `v_client`, а не покажет
РОПу всю компанию.

Отдельным файлом, а не в `routes_api.py`: тот про витрину, эти про книгу, и
общего у них — только префикс.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import clients_read as read
from clients_scope import BookMissing, scoped_clients
from context import scope_for
from transcripts_read import read_transcript

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Параметры фильтра, которые ручка списка принимает из строки запроса.
# Перечислены поимённо: «всё, что пришло» означало бы, что имя колонки из
# адресной строки попадает в запрос.
FILTER_PARAMS = (
    "triage_state", "assignee_id", "department_id", "category_id",
    "stage_id", "silence_gt", "is_agent", "reviewed",
)


# Что отвечать, когда книги нет. 503, а не пустой список: «книга не
# собрана» и «клиентов ноль» — разные вещи, и пустой ответ на первое
# читался бы как второе. 404 тоже не годится: адрес правильный.
BOOK_MISSING = {
    "error": "clients_book_missing",
    "detail": "Книга клиентов ещё не собрана. Запустите src/clients/build.py.",
}


def _book_missing() -> JSONResponse:
    return JSONResponse(BOOK_MISSING, status_code=503,
                        headers={"Cache-Control": "no-store"})


def _filters(request: Request) -> dict[str, Any]:
    params = request.query_params
    return {name: params.get(name) for name in FILTER_PARAMS if params.get(name)}


@router.get("/clients", response_model=None)
async def api_clients(request: Request) -> StreamingResponse | JSONResponse:
    """Список клиентов потоком JSONL (раздел 7.2).

    Поток, а не массив: список читает и человек через страницу, и модель
    целиком, и держать две тысячи строк в памяти ради того, чтобы тут же
    их отдать, незачем. JSONL читается построчно — потребителю не нужно
    дожидаться конца ответа, чтобы разобрать начало.
    """
    scope = scope_for(request)
    filters = _filters(request)
    limit = request.query_params.get("limit")
    cursor = request.query_params.get("cursor")

    # Книга проверяется ДО начала потока: обнаружив её отсутствие внутри
    # генератора, ответить кодом было бы уже нечем — заголовки ушли.
    try:
        with scoped_clients(scope):
            pass
    except BookMissing:
        return _book_missing()

    def lines() -> Iterator[bytes]:
        # Соединение открывается внутри генератора и закрывается вместе с
        # ним: открой его снаружи — и оно останется висеть, если читатель
        # оборвёт ответ на середине.
        with scoped_clients(scope) as conn:
            conn.row_factory = sqlite3.Row
            for row in read.list_clients(conn, filters=filters, limit=limit,
                                         cursor=cursor):
                yield (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/clients/{client_key:path}", response_model=None)
async def api_client(request: Request, client_key: str) -> JSONResponse:
    """Клиент целиком: он сам, карточки, вся лента, все разборы.

    `client_key` объявлен `:path`, потому что ключ телефонный и начинается
    с `p:+7…`. Обычный параметр пути режется по слэшу — его в ключе нет, но
    плюс и двоеточие FastAPI пропускает только так, и первый же клиент с
    ключом `c:88` иначе вернул бы 404 на ровном месте.
    """
    try:
        with scoped_clients(scope_for(request)) as conn:
            conn.row_factory = sqlite3.Row
            found = read.read_client(conn, client_key)
    except BookMissing:
        return _book_missing()

    if found is None:
        # Клиента нет и клиент чужой отвечают одинаково. Разные ответы
        # превратили бы ручку в перечислитель: по 404 против 403 можно
        # узнать, какие ключи в книге есть, не видя ни одного.
        raise HTTPException(status_code=404, detail="Клиент не найден")

    return JSONResponse(found, headers={"Cache-Control": "no-store"})


@router.get("/calls/{activity_id}/transcript", response_model=None)
async def api_call_transcript(request: Request, activity_id: int) -> JSONResponse:
    """Текст одного звонка — по клику со страницы клиента (раздел 9).

    В ленту расшифровки не рендерятся: у клиента с сорока звонками
    страница весила бы мегабайты.

    Сам текст лежит в базе аудита, где области видимости нет вовсе.
    Поэтому право спрашивается У КНИГИ: чей это звонок, видит ли его
    пользователь — и только потом читается текст. Обратный порядок отдал
    бы чужой разговор тому, кто угадал номер.
    """
    try:
        with scoped_clients(scope_for(request)) as conn:
            owner = read.client_of_call(conn, activity_id)
    except BookMissing:
        return _book_missing()

    if owner is None:
        raise HTTPException(status_code=404, detail="Звонок не найден")

    text = read_transcript(activity_id)
    if text is None:
        # Звонок есть, текста нет: очередь не дошла, расшифровка не
        # получилась, кэш недоступен. Для читателя это одно и то же —
        # читать нечего, — и различать эти случаи в ответе значило бы
        # рассказывать про устройство очереди тому, кто спросил про звонок.
        return JSONResponse(
            {"activity_id": activity_id, "client_key": owner, "text": None},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    return JSONResponse(
        {"activity_id": activity_id, "client_key": owner, "text": text},
        headers={"Cache-Control": "no-store"},
    )
