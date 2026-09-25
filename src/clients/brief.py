"""Что модель видит про одного клиента (раздел 6.3 ТЗ).

Между книгой и моделью. Разбор обязан быть воспроизводимым: чтобы понять,
почему модель ответила так, нужно уметь показать ровно то, что ей дали.
Поэтому сборка выписки — чистая функция, без базы и без портала, а всё
чтение живёт в `clients.review`.

**Лента подрезается, и подрезается ВСЛУХ.** У клиента с сорока звонками
выписка не влезет в запрос, но молча обрезанная история — худшее из
возможного: модель уверенно ответит про то, чего не читала, и отличить
такой ответ от честного нельзя ничем. Поэтому в выписке лежит `trimmed` —
сколько событий и расшифровок осталось за кадром, — и системная подсказка
велит модели об этом сказать.

**Края важнее середины.** Остаются самые старые события — с чего всё
началось, — и самые свежие — где всё стоит сейчас. Выпадает середина:
именно она предсказуема и именно её можно пересказать по краям.

**Разговоры идут от новых к старым.** Решение принимают по последнему
разговору, а не по первому; если бюджет кончится, кончиться он должен на
прошлогоднем звонке.

**Телефона в выписке нет.** Чтобы сказать, что с клиентом происходит,
номер не нужен, а выписка уходит наружу — в чужой сервис. Имя остаётся:
без него модель не отличит клиента от брокера в расшифровке.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from clients.facts import duration_sec
from clients.schema import EVENT_CALL, EVENT_COMMENT, EVENT_STAGE

# Сколько событий ленты показываем. Сорок — это примерно полгода активной
# работы по карточке; больше модель всё равно пересказывает, а не читает.
TIMELINE_LIMIT = 40
# Из них — самых ранних. Начало истории отвечает на вопрос «чего человек
# хотел», и без него свежий хвост читается как разговор с незнакомцем.
TIMELINE_HEAD = 5

# Разговоров и символов на них. Потолок в символах, а не в токенах: токены
# считает провайдер по своим правилам, а обрезать надо здесь и предсказуемо.
TRANSCRIPT_LIMIT = 8
TRANSCRIPT_CHARS = 20000

# Одна расшифровка длиннее этого — обрезается с конца. Конец разговора
# обычно и есть договорённость, но целиком часовой звонок съест весь бюджет
# и не оставит места остальным.
TRANSCRIPT_ONE_CHARS = 6000


@dataclass(frozen=True)
class FeedEvent:
    """Событие ленты в том виде, в каком его читает выписка.

    Свой тип, а не `clients.events.Event`: у того нет
    `author_is_assignee`, а выписке он нужен — чужая запись в карточке это
    или помощь, или чужая работа. Заодно тип объявляет вслух, что именно
    выписке требуется: читающему не надо гадать, какие из девяти полей
    сборщика она трогает.
    """

    at: str
    kind: str
    source_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    is_system: bool = False
    author_is_assignee: int | None = None


@dataclass(frozen=True)
class Trimmed:
    """Чего модель НЕ увидела. Пустой — увидела всё."""

    events: int = 0
    transcripts: int = 0
    cut_texts: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"событий скрыто": self.events,
                "разговоров не показано": self.transcripts,
                "разговоров обрезано": self.cut_texts}

    def __bool__(self) -> bool:
        return bool(self.events or self.transcripts or self.cut_texts)


@dataclass(frozen=True)
class Brief:
    """Выписка по клиенту — ровно то, что уйдёт в модель."""

    client: dict[str, Any] = field(default_factory=dict)
    cards: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    talks: list[dict[str, Any]] = field(default_factory=list)
    trimmed: Trimmed = Trimmed()
    # Есть ли на чём отвечать. Нет — разбор всё равно делается, но помечает
    # себя: вывод по датам и чужим пометкам и вывод по словам клиента это
    # разные вещи, и брокер обязан различать их с первого взгляда.
    enough_data: bool = False

    def as_payload(self) -> dict[str, Any]:
        """То, что уходит в запрос. Ключи русские — их читает модель."""
        return {
            "клиент": self.client,
            "карточки": self.cards,
            "лента": self.timeline,
            "разговоры": self.talks,
            "чего не показано": self.trimmed.as_dict(),
            "есть на чём отвечать": self.enough_data,
        }


# Поля клиента, которые видит модель. Перечислены поимённо: «всё, что есть
# в строке» однажды принесло бы наружу колонку, заведённую для другого.
CLIENT_FIELDS = (
    "name", "triage_state", "triage_reason", "assignee_name", "assignee_count",
    "silence_days", "last_touch_at", "next_step_at", "next_step_overdue",
    "calls_total", "calls_with_transcript", "comments_total",
    "comments_by_assignee", "is_agent",
)

CARD_FIELDS = ("title", "stage_name", "stage_id", "closed", "date_create")


def build(
    client: Mapping[str, Any],
    cards: Sequence[Mapping[str, Any]],
    events: Sequence[FeedEvent],
    texts: Mapping[int, str],
) -> Brief:
    """Собрать выписку. Ни базы, ни портала — только то, что дали."""
    timeline, cut_events = _timeline(events)
    talks, skipped, cut_texts = _talks(events, texts)
    return Brief(
        client={name: client.get(name) for name in CLIENT_FIELDS},
        cards=[{name: card.get(name) for name in CARD_FIELDS} for card in cards],
        timeline=timeline,
        talks=talks,
        trimmed=Trimmed(events=cut_events, transcripts=skipped, cut_texts=cut_texts),
        enough_data=bool(talks) or _has_human_note(events),
    )


def _timeline(events: Sequence[FeedEvent]) -> tuple[list[dict[str, Any]], int]:
    """Лента по возрастанию времени, подрезанная по краям."""
    ordered = sorted(events, key=lambda event: (event.at, event.source_id))
    if len(ordered) <= TIMELINE_LIMIT:
        return [_entry(event) for event in ordered], 0
    head = ordered[:TIMELINE_HEAD]
    tail = ordered[len(ordered) - (TIMELINE_LIMIT - TIMELINE_HEAD):]
    return [_entry(event) for event in (*head, *tail)], len(ordered) - TIMELINE_LIMIT


def _entry(event: FeedEvent) -> dict[str, Any]:
    payload = event.payload or {}
    entry: dict[str, Any] = {
        "когда": event.at,
        "что": event.kind,
        # Чужая запись в карточке — это или помощь, или чужая работа, и
        # решать, что именно, должен читатель. Ноль значит «не
        # ответственный», пусто — «неизвестно».
        "ответственный": event.author_is_assignee,
    }
    if event.is_system:
        entry["робот"] = True
    if event.kind == EVENT_CALL:
        entry["направление"] = _direction(payload)
        entry["секунд"] = duration_sec(payload)
    elif event.kind == EVENT_STAGE:
        entry["стадия"] = payload.get("stage_to") or payload.get("stage_id")
    text = payload.get("body") or payload.get("subject") or ""
    if text:
        entry["текст"] = str(text)
    return entry


def _direction(payload: Mapping[str, Any]) -> str:
    """Словом, а не числом: «1» и «2» модель прочтёт как попало."""
    try:
        value = int(payload.get("direction"))
    except (TypeError, ValueError):
        return "неизвестно"
    return {1: "входящий", 2: "исходящий"}.get(value, "неизвестно")


def _talks(
    events: Sequence[FeedEvent],
    texts: Mapping[int, str],
) -> tuple[list[dict[str, Any]], int, int]:
    """Разговоры с текстом: новые первыми, пока хватает бюджета."""
    calls = [event for event in events if event.kind == EVENT_CALL]
    calls.sort(key=lambda event: event.at, reverse=True)

    talks: list[dict[str, Any]] = []
    left = TRANSCRIPT_CHARS
    skipped = cut = 0
    for event in calls:
        text = str(texts.get(_source_id(event)) or "").strip()
        if not text:
            continue
        if len(talks) >= TRANSCRIPT_LIMIT or left <= 0:
            skipped += 1
            continue
        piece = text[:TRANSCRIPT_ONE_CHARS]
        piece = piece[:left]
        if len(piece) < len(text):
            cut += 1
        left -= len(piece)
        talks.append({
            "когда": event.at,
            "направление": _direction(event.payload or {}),
            "секунд": duration_sec(event.payload or {}),
            "текст": piece,
        })
    return talks, skipped, cut


def _has_human_note(events: Iterable[FeedEvent]) -> bool:
    """Есть ли хоть одна человеческая запись в карточке.

    Роботная не в счёт: автоматический комментарий пишет интеграция, и
    выводов о клиенте из него не сделать. Если нет ни разговора, ни
    человеческой записи, разбору остаются одни даты — и он обязан это
    сказать о себе сам.
    """
    return any(
        event.kind == EVENT_COMMENT and not event.is_system
        and str((event.payload or {}).get("body") or "").strip()
        for event in events
    )


def _source_id(event: FeedEvent) -> int:
    try:
        return int(event.source_id)
    except (TypeError, ValueError):
        return 0
