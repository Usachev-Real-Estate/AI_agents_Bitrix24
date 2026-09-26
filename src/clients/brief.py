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


# Подписи полей клиента для текстовой выписки и их порядок на экране.
# Отдельно от `CLIENT_FIELDS`, потому что читателю нужны слова, а не имена
# колонок: `assignee_count` он прочтёт как что угодно, «брокеров на
# клиенте» — однозначно. Каждое поле выписки обязано быть здесь, и это
# проверено тестом: иначе колонка, добавленная в `CLIENT_FIELDS`, молча
# исчезла бы из текстовой формы, оставшись в JSON.
_CLIENT_LABELS = (
    ("name", "имя"),
    ("triage_state", "состояние"),
    ("triage_reason", "правило"),
    ("assignee_name", "ответственный"),
    ("assignee_count", "брокеров на клиенте"),
    ("is_agent", "агент"),
    ("silence_days", "дней тишины"),
    ("last_touch_at", "последнее касание"),
    ("next_step_at", "следующий шаг"),
    ("next_step_overdue", "шаг просрочен"),
    ("calls_total", "звонков"),
    ("calls_with_transcript", "из них с расшифровкой"),
    ("comments_total", "записей в карточках"),
    ("comments_by_assignee", "из них ответственного"),
)

# Подписи карточек. `closed=0` читатель обязан понять с первого взгляда:
# на этом уже спотыкалась разбирающая модель — она прочла рабочую стадию
# «Закрытая продажа (На сайт)» как завершённую сделку. «закрыта: нет»
# спутать не с чем, «closed=0» приглашает догадываться.
_CARD_LABELS = (
    ("title", "название"),
    ("stage_name", "стадия"),
    ("stage_id", "код стадии"),
    ("closed", "закрыта"),
    ("date_create", "заведена"),
)

# Поля-признаки: в базе 0/1, на экране «нет»/«да». Ноль в такой колонке
# читатель переводит сам, и именно на этом переводе разбирающая модель уже
# ошиблась — прочла рабочую стадию как завершённую сделку. Пусто остаётся
# прочерком: `next_step_overdue` без значения это «не измерено», а не «нет».
_FLAGS = frozenset({"closed", "is_agent", "next_step_overdue", "ответственный"})

# Пусто — это «не задано», а не ноль. Прочерк сохраняет разницу, которая в
# ленте значит буквально разные вещи: `ответственный=0` — запись сделал не
# ответственный, `ответственный=—` — кто сделал, неизвестно.
NOT_SET = "—"


def as_text(brief: Brief) -> str:
    """Та же выписка словами — для читателя, который видит страницу.

    Нужна не для красоты. `as_payload` уходит в запрос как JSON, а список
    книги браузер отдаёт типом `application/x-ndjson` — такой ответ он
    скачивает файлом, а не показывает, и расширение, читающее
    отрендеренную страницу, увидело бы пустоту.

    Телефона здесь нет не потому, что его не печатают, а потому, что
    `CLIENT_FIELDS` его не пускает: текстовая форма берёт данные оттуда же,
    откуда JSON, и добавить в неё поле мимо этого списка нельзя.

    **Подрезка названа и тут.** Раздел «ВЫПИСКА» идёт вторым, до самих
    данных: читатель, у которого кончится бюджет, должен узнать про
    скрытые события раньше, чем начнёт делать по ним выводы.

    **Пустые разделы пишутся вслух.** Раздела нет — читается как «не
    смотрели»; раздел со словом «нет» — как «смотрели, ничего». Для
    выписки, по которой решают судьбу человека, это разные вещи.
    """
    blocks = [
        _client_block(brief),
        _summary_block(brief),
        _cards_block(brief),
        _timeline_block(brief),
        _talks_block(brief),
    ]
    return "\n\n".join(blocks) + "\n"


def _client_block(brief: Brief) -> str:
    lines = ["КЛИЕНТ"]
    lines += [f"  {label}: {_value(name, brief.client.get(name))}"
              for name, label in _CLIENT_LABELS]
    return "\n".join(lines)


def _labelled(entry: Mapping[str, Any], labels: Sequence[tuple[str, str]]) -> str:
    """Запись одной строкой по заданным подписям, в их порядке.

    Не `_fields`: там имена приходят из самой записи, и для карточки это
    были бы имена колонок. `closed=0` читателю ничего не говорит.
    """
    return "  ".join(f"{label}: {_value(name, entry.get(name))}"
                     for name, label in labels)


def _summary_block(brief: Brief) -> str:
    """Что известно о самой выписке: на чём отвечать и чего в ней нет.

    Счётчики берутся из `Trimmed.as_dict`, а не пишутся своими словами:
    одна формулировка на JSON и на текст — одно место, где её править.
    """
    lines = ["ВЫПИСКА",
             f"  есть на чём отвечать: {_flat(brief.enough_data)}"]
    lines += [f"  {label}: {count}" for label, count in brief.trimmed.as_dict().items()]
    return "\n".join(lines)


def _cards_block(brief: Brief) -> str:
    lines = [_head("КАРТОЧКИ", len(brief.cards))]
    lines += [f"  {_labelled(card, _CARD_LABELS)}" for card in brief.cards]
    return "\n".join(lines)


def _timeline_block(brief: Brief) -> str:
    """Лента с числом «столько из столького».

    Всего событий — показанные плюс скрытые: своего счётчика у выписки нет
    и быть не должно, иначе он разойдётся с подрезкой.
    """
    shown = len(brief.timeline)
    total = shown + brief.trimmed.events
    extra = f" из {total}" if total != shown else ""
    lines = [_head("ЛЕНТА", shown, extra=extra)]
    lines += [f"  {_fields(entry)}" for entry in brief.timeline]
    return "\n".join(lines)


def _talks_block(brief: Brief) -> str:
    """Разговоры: заголовок строкой, текст как есть.

    Единственный раздел, где переводы строк внутри значения сохраняются:
    это и есть содержание, а не подпись к нему.
    """
    lines = [_head("РАЗГОВОРЫ", len(brief.talks))]
    for talk in brief.talks:
        # Всё, кроме текста: перечислять поля по именам значило бы завести
        # второй список, который забудут поправить вместе с `_talks`.
        head = {name: value for name, value in talk.items() if name != "текст"}
        lines.append(f"  --- {_fields(head)} ---")
        lines.append(str(talk.get("текст") or ""))
    return "\n".join(lines)


def _head(title: str, count: int, *, extra: str = "") -> str:
    """Заголовок раздела с числом. Ноль — словом: «0» ищется глазами хуже."""
    return f"{title}: {count if count else 'нет'}{extra}"


def _fields(entry: Mapping[str, Any]) -> str:
    """Запись одной строкой: все её поля, в том порядке, в каком положены.

    Перечислять поля по именам нельзя: `_entry` кладёт «стадию» только
    стадиям, «секунд» только звонкам, и второй список пришлось бы править
    вместе с первым — а забытое поле пропало бы из выписки молча, оставшись
    в JSON. Здесь печатается то, что есть.
    """
    return "  ".join(f"{name}={_value(name, value)}" for name, value in entry.items())


def _value(name: str, value: Any) -> str:
    """Значение поля с поправкой на признаки."""
    if name in _FLAGS and value is not None and value != "":
        return "да" if value else "нет"
    return _flat(value)


def _flat(value: Any) -> str:
    """Значение в одну строку.

    Перевод строки внутри записи склеил бы два события в одно и сдвинул бы
    всю ленту: читатель видит текст, а не разметку, и границы записей у
    него только эти.
    """
    if value is None or value == "":
        return NOT_SET
    if value is True:
        return "да"
    if value is False:
        return "нет"
    return " ".join(str(value).split())
