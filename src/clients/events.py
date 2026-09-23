"""Лента событий клиента и агрегаты по ней (раздел 4 ТЗ, шаги 6 и 8).

Лента — это ответ на вопрос «что по клиенту происходило», собранный из
четырёх источников витрины: комментарии таймлайна, дела, звонки и движение
по стадиям. Агрегаты — тот же материал, свёрнутый в числа для списка.

Считаются они из одних и тех же строк витрины, а не одно из другого: лента
уезжает в базу и живёт там своей жизнью (расшифровка может приехать через
сутки и переписать событие), а числа обязаны отвечать ровно тому прогону,
который их посчитал.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from clients.mart import CALL_PROVIDER, OWNER_TYPE_CONTACT, OWNER_TYPE_DEAL, Portfolio
from clients.schema import EVENT_ACTIVITY, EVENT_CALL, EVENT_COMMENT, EVENT_STAGE
from clients.schema import OWNER_CONTACT, OWNER_DEAL

logger = logging.getLogger(__name__)

# Окно «свежих» комментариев ответственного — раздел 3 ТЗ,
# comments_by_assignee_30d.
FRESH_DAYS = 30


@dataclass(frozen=True)
class Event:
    """Одно событие ленты, готовое к записи."""

    client_key: str
    at: str
    kind: str
    source_id: str
    entity_type: str
    entity_id: int
    author_id: int | None
    is_system: bool
    payload: dict[str, Any]

    @property
    def payload_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class Totals:
    """Свёрнутые числа по одному клиенту."""

    last_event_at: str | None
    last_touch_at: str | None
    last_touch_kind: str | None
    last_touch_author_id: int | None
    silence_days: int | None
    comments_total: int
    comments_by_assignee: int
    comments_by_assignee_30d: int
    calls_total: int
    next_step_at: str | None
    next_step_overdue: bool | None


def stage_source_id(entity_type: str, entity_id: int, at: str,
                    stage_from: str, stage_to: str) -> str:
    """Отпечаток перехода по стадии.

    Всегда хэш, и никогда `fact_stage_event.id`: тот перевыдаётся заново
    при каждом прогоне ETL, потому что `replace_stage_events` переписывает
    ленту сущности целиком через DELETE + INSERT `[V3]`. Ключ на нём плодил
    бы дубликаты каждые пятнадцать минут.

    Считается sha1, а не встроенным `hash()`: тот солится PYTHONHASHSEED и
    от запуска к запуску даёт разные числа. На этом проект уже обжигался —
    тесты строили идентификаторы из `hash(строка)` и падали на одном
    конкретном значении соли.
    """
    raw = "|".join((entity_type, str(entity_id), at, stage_from, stage_to))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def build_events(
    portfolio: Portfolio,
    key_by_deal: Mapping[int, str],
    key_by_contact: Mapping[int, str],
) -> list[Event]:
    """Собрать ленту всего портфеля.

    События, у которых не нашлось клиента, отбрасываются молча: это дело на
    контакте, чьи сделки не попали в воронки портфеля. Оно не наше, и
    привязывать его наугад значит показать брокеру чужой разговор.
    """
    events: list[Event] = []
    events.extend(_comment_events(portfolio, key_by_deal))
    events.extend(_activity_events(portfolio, key_by_deal, key_by_contact))
    events.extend(_stage_events(portfolio, key_by_deal))
    events.sort(key=lambda event: (event.client_key, event.at, event.source_id))
    return events


def _comment_events(portfolio: Portfolio, key_by_deal: Mapping[int, str]) -> list[Event]:
    out: list[Event] = []
    for row in portfolio.comments:
        key = key_by_deal.get(int(row["entity_id"]))
        if not key:
            continue
        out.append(Event(
            client_key=key,
            at=row["created_at"],
            kind=EVENT_COMMENT,
            source_id=str(row["comment_id"]),
            entity_type=OWNER_DEAL,
            entity_id=int(row["entity_id"]),
            author_id=_int(row.get("author_id")),
            # Роботная запись портала — не работа человека. Колонка та же,
            # которой этап 2 пометит наши собственные комментарии: вопрос,
            # на который она отвечает, один — «это писал человек?».
            is_system=bool(row.get("is_auto")),
            payload={"body": row.get("body") or ""},
        ))
    return out


def _activity_events(
    portfolio: Portfolio,
    key_by_deal: Mapping[int, str],
    key_by_contact: Mapping[int, str],
) -> list[Event]:
    out: list[Event] = []
    for row in portfolio.activities:
        owner_type = int(row["owner_type_id"])
        owner_id = int(row["owner_id"])
        if owner_type == OWNER_TYPE_DEAL:
            key, entity_type = key_by_deal.get(owner_id), OWNER_DEAL
        elif owner_type == OWNER_TYPE_CONTACT:
            key, entity_type = key_by_contact.get(owner_id), OWNER_CONTACT
        else:
            continue
        if not key:
            continue
        is_call = (row.get("provider_type_id") or "") == CALL_PROVIDER
        out.append(Event(
            client_key=key,
            at=row["created_at"],
            kind=EVENT_CALL if is_call else EVENT_ACTIVITY,
            source_id=str(row["activity_id"]),
            entity_type=entity_type,
            entity_id=owner_id,
            # Кто дело ЗАВЁЛ, а не на ком оно висит: звонок за брокера
            # часто заводит колл-центр или РОП.
            author_id=_int(row.get("author_id")),
            is_system=False,
            payload={
                "subject": row.get("subject") or "",
                "description": row.get("description") or "",
                "direction": row.get("direction"),
                "completed": bool(row.get("completed")),
                "deadline": row.get("deadline"),
                "start_time": row.get("start_time"),
                "end_time": row.get("end_time"),
                "responsible_id": row.get("responsible_id"),
            },
        ))
    return out


def _stage_events(portfolio: Portfolio, key_by_deal: Mapping[int, str]) -> list[Event]:
    out: list[Event] = []
    previous_entity: int | None = None
    previous_stage = ""
    for row in portfolio.moves:
        entity_id = int(row["entity_id"])
        if entity_id != previous_entity:
            previous_entity, previous_stage = entity_id, ""
        stage_to = row["stage_id"]
        stage_from, previous_stage = previous_stage, stage_to
        key = key_by_deal.get(entity_id)
        if not key:
            continue
        category_id = int(row.get("category_id") or 0)
        out.append(Event(
            client_key=key,
            at=row["entered_at"],
            kind=EVENT_STAGE,
            source_id=stage_source_id(
                OWNER_DEAL, entity_id, row["entered_at"], stage_from, stage_to,
            ),
            entity_type=OWNER_DEAL,
            entity_id=entity_id,
            author_id=None,
            is_system=False,
            payload={
                "stage_from": stage_from,
                "stage_to": stage_to,
                "stage_name": portfolio.stages.get((stage_to, category_id), ""),
            },
        ))
    return out


def totals(
    events: Sequence[Event],
    *,
    assignee_id: int | None,
    now: datetime,
) -> Totals:
    """Свернуть ленту одного клиента в числа списка.

    Касанием считается то, что сделал человек: комментарий, дело, звонок,
    движение по стадии. Роботная запись портала касанием не считается —
    иначе «по карточке работали» отвечало бы «да» там, где карточку только
    задевала рассылка.
    """
    if not events:
        return Totals(None, None, None, None, None, 0, 0, 0, 0, None, None)

    touches = [event for event in events if not event.is_system]
    last_touch = max(touches, key=lambda event: event.at) if touches else None
    comments = [event for event in events if event.kind == EVENT_COMMENT
                and not event.is_system]
    fresh_from = (now - timedelta(days=FRESH_DAYS)).isoformat()
    by_assignee = [
        event for event in comments
        if assignee_id and event.author_id == assignee_id
    ]

    return Totals(
        last_event_at=max(event.at for event in events),
        last_touch_at=last_touch.at if last_touch else None,
        last_touch_kind=last_touch.kind if last_touch else None,
        last_touch_author_id=last_touch.author_id if last_touch else None,
        silence_days=_days_since(last_touch.at, now) if last_touch else None,
        comments_total=len(comments),
        comments_by_assignee=len(by_assignee),
        comments_by_assignee_30d=sum(1 for event in by_assignee if event.at >= fresh_from),
        calls_total=sum(1 for event in events if event.kind == EVENT_CALL),
        **_next_step(events, now),
    )


def _next_step(events: Sequence[Event], now: datetime) -> dict[str, Any]:
    """Ближайший назначенный шаг и просрочен ли он.

    Шаг — это срок незакрытого дела. Закрытое дело обещанием быть перестало:
    его уже сделали. Срок «нет срока» портал отдаёт 2100 годом, и витрина
    уже превратила его в NULL при загрузке — здесь достаточно пустоты.
    """
    moment = now.isoformat()
    open_deadlines = sorted(
        event.payload["deadline"]
        for event in events
        if event.kind in (EVENT_ACTIVITY, EVENT_CALL)
        and not event.payload.get("completed")
        and event.payload.get("deadline")
    )
    if not open_deadlines:
        return {"next_step_at": None, "next_step_overdue": None}
    ahead = [value for value in open_deadlines if value >= moment]
    return {
        "next_step_at": ahead[0] if ahead else open_deadlines[0],
        "next_step_overdue": not ahead,
    }


def _days_since(value: str, now: datetime) -> int | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, (now - moment).days)
