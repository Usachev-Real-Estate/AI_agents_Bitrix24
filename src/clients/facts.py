"""Факты о клиенте для правил раздела 6.2 ТЗ.

Между лентой и правилами. `clients.triage` обязан остаться чистым — ни
базы, ни портала, ни текста, — а витрину, кэш расшифровок и форму полей
Битрикса кто-то знать должен. Знает этот модуль.

**Текст дальше не идёт.** Здесь читаются и комментарии, и расшифровки, но
наружу уходит один бит `has_refusal`. Правила, которые не видят разговора,
не могут его напечатать — ни в лог, ни в журнал прогонов, ни на экран.

Длительность звонка витрина не хранит: в `fact_activity` есть `start_time`
и `end_time`, а поля `duration` нет `[V28]`. Разность работает, но только
там, где обе метки заполнены, а заполняет ли их портал для звонков —
вопрос к живым данным, а не к документации. Поэтому рядом с фактами
считается `Coverage`: сколько звонков вообще поддаются измерению. Если
окажется, что большинство нет, порог «≥60 секунд» из правила 3 придётся
пересматривать — и это будет видно числом, а не догадкой.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from clients.schema import EVENT_ACTIVITY, EVENT_CALL, EVENT_COMMENT
from clients.transcripts import find_markers
from clients.triage import MEANINGFUL_CALL_SEC, Facts

logger = logging.getLogger(__name__)

# Направление дела в Битриксе. Числами, как их отдаёт портал и как их уже
# читает витрина (analytics/work.py:365).
INCOMING = 1
OUTGOING = 2


@dataclass(frozen=True)
class Coverage:
    """Сколько звонков поддаётся измерению по длительности.

    Считается по всему портфелю и уходит в лог прогона. Правило 3 стоит на
    пороге в минуту, а порог, применённый к неизмеримому, молча не
    срабатывает никогда — и отличить «дыр в данных нет» от «нечем мерить»
    по одному состоянию клиента невозможно.
    """

    calls: int = 0
    measurable: int = 0
    meaningful: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "звонков": self.calls,
            "с длительностью": self.measurable,
            f"длиннее {MEANINGFUL_CALL_SEC} с": self.meaningful,
        }


def duration_sec(payload: Mapping[str, Any]) -> int | None:
    """Длительность звонка из меток начала и конца. ``None`` — не измерить.

    Отрицательная разность тоже ``None``: конец раньше начала означает, что
    одна из меток не про этот звонок, и считать по ней минуту нельзя.
    """
    started, ended = _moment(payload.get("start_time")), _moment(payload.get("end_time"))
    if started is None or ended is None:
        return None
    seconds = int((ended - started).total_seconds())
    return seconds if seconds >= 0 else None


def coverage(events: Iterable[Any]) -> Coverage:
    """Пересчитать измеримость звонков по всей ленте портфеля."""
    calls = measurable = meaningful = 0
    for event in events:
        if event.kind != EVENT_CALL:
            continue
        calls += 1
        seconds = duration_sec(event.payload)
        if seconds is None:
            continue
        measurable += 1
        meaningful += seconds >= MEANINGFUL_CALL_SEC
    return Coverage(calls=calls, measurable=measurable, meaningful=meaningful)


def calls_with_text(events: Iterable[Any], transcribed: set[int] | None) -> int | None:
    """Сколько звонков клиента расшифровано. ``None`` — кэш недоступен.

    Длительность здесь НЕ смотрится, в отличие от правила 3. Тому нужен
    разговор, по которому текст обязан быть, и сорокасекундный недозвон в
    счёт не идёт. Этому числу нужен материал, который можно прочитать, — а
    короткий звонок с расшифровкой читается не хуже трёхминутного.

    ``None``, а не ноль: колонка `calls_with_transcript` объявлена
    NULL-умеющей ровно затем, чтобы «не считано» отличалось от «нет ни
    одной». Ночь, в которую база аудита оказалась не на месте, не должна
    выглядеть как ночь, в которую у клиентов пропали расшифровки.
    """
    if transcribed is None:
        return None
    return sum(1 for event in events
               if event.kind == EVENT_CALL and int(event.source_id) in transcribed)


def collect(
    events: Sequence[Any],
    deal_ids: Iterable[int],
    *,
    deals: Mapping[int, Mapping[str, Any]],
    last_touch_at: str | None,
    next_step_at: str | None,
    transcribed: set[int] | None,
    refused_calls: set[int] | None,
    markers: Sequence[str],
) -> Facts:
    """Собрать факты по одному клиенту.

    ``transcribed`` и ``refused_calls`` равны ``None``, когда кэш
    расшифровок недоступен. Тогда правила 3 и 2 обязаны промолчать, а не
    сработать на пустоте: у правила 3 не окажется ни одного звонка с
    текстом — и оно объявило бы безданным весь портфель.
    """
    cards = [deals.get(deal_id, {}) for deal_id in deal_ids]
    calls = [event for event in events if event.kind == EVENT_CALL]

    return Facts(
        cards_total=len(cards),
        cards_closed=sum(1 for card in cards if card.get("is_closed")),
        has_refusal=_has_refusal(events, calls, refused_calls, markers),
        calls_without_text=_calls_without_text(calls, transcribed),
        last_incoming_call_at=_last_at(calls, lambda e: _direction(e) == INCOMING),
        last_outgoing_at=_last_at(events, _is_outgoing),
        last_touch_at=last_touch_at,
        next_step_at=next_step_at,
        oldest_card_at=min(
            (str(card["date_create"]) for card in cards if card.get("date_create")),
            default=None,
        ),
    )


def _has_refusal(
    events: Sequence[Any],
    calls: Sequence[Any],
    refused_calls: set[int] | None,
    markers: Sequence[str],
) -> bool:
    """Маркер отказа в комментарии или в расшифровке.

    Роботные записи не смотрим: автоматический комментарий пишет не
    человек, и слово «отказ» в нём — это шаблон интеграции, а не слова
    клиента.
    """
    if refused_calls:
        if any(int(event.source_id) in refused_calls for event in calls):
            return True
    return any(
        find_markers(event.payload.get("body") or "", markers)
        for event in events
        if event.kind == EVENT_COMMENT and not event.is_system
    )


def _calls_without_text(calls: Sequence[Any], transcribed: set[int] | None) -> int:
    """Звонки длиннее минуты, по которым расшифровки нет.

    Кэш недоступен (``None``) — ноль, и правило 3 молчит. Иначе каждый
    клиент со звонком объявлялся бы безданным в ту ночь, когда файл кэша
    оказался не на месте.

    Звонок, длительность которого измерить нечем, тоже не считается: порог
    задан в секундах, и применять его к неизвестному значит выдумывать.
    """
    if transcribed is None:
        return 0
    count = 0
    for event in calls:
        seconds = duration_sec(event.payload)
        if seconds is None or seconds < MEANINGFUL_CALL_SEC:
            continue
        if int(event.source_id) not in transcribed:
            count += 1
    return count


def _is_outgoing(event: Any) -> bool:
    """Мы вышли на связь: исходящий звонок или закрытое дело.

    Комментарий сюда НЕ входит. Написать себе в карточку — не значит
    ответить человеку, который звонил; засчитав это, правило 4 молчало бы
    ровно там, где брокер отметился в CRM вместо того, чтобы перезвонить.

    Незакрытое дело тоже не в счёт: «перезвонить» в планах — это ещё не
    звонок.
    """
    if event.kind == EVENT_CALL:
        return _direction(event) == OUTGOING
    return event.kind == EVENT_ACTIVITY and bool(event.payload.get("completed"))


def _direction(event: Any) -> int | None:
    value = event.payload.get("direction")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _last_at(events: Iterable[Any], fits) -> str | None:
    return max((event.at for event in events if fits(event)), default=None)


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
