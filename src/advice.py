"""Советы руководителю: что делать сегодня, и что из вчерашнего сработало.

Отчёт говорит «вот числа». Совет говорит «сделай это». Разница не в тоне, а
в устройстве, и всё в этом модуле подчинено четырём правилам.

СОВЕТ БЕЗ ИМЕНИ И ЧИСЛА — НЕ СОВЕТ. Каждый обязан назвать человека или
карточку и число, из которого он вырос. «Повысьте конверсию» — наблюдение,
и правило, которое не может назвать конкретное, просто не срабатывает.

МЕСТ ТРИ, И ОНИ ЗАКРЕПЛЕНЫ: деньги, работа, вчерашнее событие. Общий
рейтинг схлопнулся бы: 315 нетронутых карточек — число большое, и оно
вытесняло бы всё остальное каждое утро месяцами. Закреплённые места
гарантируют, что руководитель каждый день видит и деньги, и работу, и
новости, а не одну самую громкую цифру.

ПАМЯТЬ ВАЖНЕЕ ПРАВИЛ. Три одинаковые строки каждое утро — и сводку
перестают открывать через неделю. Поэтому совет не повторяется семь дней;
раньше — только если стало заметно хуже, и тогда он прямо говорит, когда
о нём шла речь и каким число было тогда.

ПРОВЕРЯЕМОСТЬ ЗАМЫКАЕТ КРУГ. У каждого совета есть число, и на следующем
прогоне оно пересчитывается. Стало лучше — сводка говорит «сработало» и
закрывает совет. Без этого система советует в пустоту и не знает, слушают
её или нет.

Направление у числа одно для всех правил: БОЛЬШЕ ЗНАЧИТ ХУЖЕ. Иначе
сравнение «лучше или хуже» пришлось бы держать в каждом правиле, и однажды
они разошлись бы — молча, потому что «стало лучше» выглядит правдоподобно
всегда.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

# Места в сводке. Порядок здесь — порядок в сообщении.
SLOT_MONEY = "money"
SLOT_WORK = "work"
SLOT_ACUTE = "acute"
SLOTS: tuple[str, ...] = (SLOT_MONEY, SLOT_WORK, SLOT_ACUTE)

# Сколько дней молчать об уже сказанном.
COOLDOWN_DAYS = 7

# Насколько должно стать хуже, чтобы повторить раньше срока. Двадцать
# процентов — это заметный сдвиг, а не дрожание числа на одной карточке.
WORSE_BY = 0.20

# Насколько должно стать лучше, чтобы объявить «сработало». Тот же порог:
# иначе система хвалила бы себя за случайное колебание.
BETTER_BY = 0.20

# Сколько дней ждать результата, прежде чем перестать следить за советом.
# Дольше месяца — это уже не «сделай сегодня», а другой разговор.
FOLLOW_DAYS = 30


@dataclass(frozen=True)
class Advice:
    """Один совет. Все четыре части обязательны — см. модульную строку."""

    rule: str
    subject: str
    slot: str
    value: float
    # Короткое имя адресата для строки «сработало»: «Марат Абзалилов»,
    # «Отдел Волкова», «Ленинский 45». Заголовок для этого не годится — в
    # нём стоит число, и рядом с «было 30, стало 18» оно читается третьим.
    who: str
    title: str
    action: str
    proof: str
    check: str
    weight: float = 0.0
    # Место, куда смотреть: ссылка на карточку или на раздел дашборда.
    link: str = ""
    # Уровень или событие. Уровень («30 карточек холодные») улучшается, и
    # его падение — заслуга, о которой стоит сказать. Событие («сделка
    # откатилась вчера») не улучшается: назавтра его просто нет в окне.
    # Похвалить за исчезновение события значит записать себе в актив ход
    # календаря — сводка соврала бы, и с виду убедительно.
    durable: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return (self.rule, self.subject)


@dataclass
class Memory:
    """Что уже говорили. Соответствует строке advice_log."""

    rule: str
    subject: str
    first_sent_at: str
    last_sent_at: str
    sent_count: int
    first_value: float
    last_value: float
    label: str = ""
    closed_at: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.rule, self.subject)


@dataclass
class Selection:
    """Итог отбора: что сказать сегодня и что из сказанного сработало."""

    advices: list[Advice] = field(default_factory=list)
    # Причина, по которой совет попал в сводку: «впервые» или «стало хуже».
    reasons: dict[tuple[str, str], str] = field(default_factory=dict)
    resolved: list[dict[str, Any]] = field(default_factory=list)
    # Советы, промолчавшие из-за паузы. Нужны не сводке, а тестам и
    # диагностике: без них «почему сегодня пусто» не ответить.
    muted: list[Advice] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def days_since(stamp: str | None, now: datetime | None = None) -> float | None:
    moment = _parse(stamp)
    if moment is None:
        return None
    return ((now or _now()) - moment).total_seconds() / 86400.0


def select(
    candidates: Sequence[Advice],
    memory: dict[tuple[str, str], Memory],
    *,
    now: datetime | None = None,
    slots: Sequence[str] = SLOTS,
) -> Selection:
    """Что сказать сегодня: по одному совету на место, плюс «сработало».

    Кандидатов правила отдают всех подряд, без отбора: значение нужно и тем
    из них, о ком речь уже шла, — иначе проверить «стало лучше» было бы не с
    чем, и совет, сработавший вчера, молча исчез бы вместо похвалы.
    """
    moment = now or _now()
    current = {advice.key: advice for advice in candidates}
    result = Selection(resolved=_resolved(current, memory, moment))
    closed = {row["rule"] + "\x00" + row["subject"] for row in result.resolved}

    for slot in slots:
        pool = [advice for advice in candidates if advice.slot == slot]
        pool.sort(key=lambda advice: (-advice.weight, advice.rule, advice.subject))
        for advice in pool:
            if advice.rule + "\x00" + advice.subject in closed:
                continue
            reason = _why_now(advice, memory.get(advice.key), moment)
            if reason is None:
                result.muted.append(advice)
                continue
            result.advices.append(advice)
            result.reasons[advice.key] = reason
            break
    return result


def _why_now(advice: Advice, seen: Memory | None, now: datetime) -> str | None:
    """Причина сказать это сегодня. None — значит промолчать.

    Совет, о котором ещё не говорили, идёт всегда. О сказанном молчим
    неделю — и нарушаем паузу только если стало заметно хуже: повторить с
    тем же числом значит признаться, что сводка не следит за результатом.

    Закрытый совет молчит по тем же правилам, а не выходит назавтра как
    новый. Иначе получается качель: «сработало, было 30, стало 10» сегодня
    и «у него 10 карточек без следа» завтра — про одно и то же, в
    противоположных тонах, и так на каждом шаге медленного улучшения.
    Проблема, ставшая заметно хуже после закрытия, паузу всё равно рвёт: это
    уже не улучшение, а возврат.
    """
    if seen is None:
        return "впервые"
    if seen.last_value > 0 and advice.value >= seen.last_value * (1 + WORSE_BY):
        return "хуже" if not seen.closed_at else "вернулось"
    quiet = days_since(seen.closed_at or seen.last_sent_at, now)
    if quiet is None or quiet >= COOLDOWN_DAYS:
        return "вернулось" if seen.closed_at else "снова"
    return None


def _resolved(
    current: dict[tuple[str, str], Advice],
    memory: dict[tuple[str, str], Memory],
    now: datetime,
) -> list[dict[str, Any]]:
    """Советы, после которых стало лучше. Их закрывают и об этом говорят.

    Отсутствие среди кандидатов — тоже улучшение: правило перестало видеть
    проблему. Значение тогда ноль, и это честно: «было тридцать, стало
    ноль» — ровно то, что произошло.
    """
    done = []
    for key, seen in memory.items():
        if seen.closed_at:
            continue
        age = days_since(seen.first_sent_at, now)
        if age is None or age > FOLLOW_DAYS:
            continue
        advice = current.get(key)
        # Событие, ушедшее из окна, — не победа. Судить о нём можно только
        # по тому, повторилось ли оно, и об этом говорит уже память, а не
        # похвала. Пока правило не видит его сегодня, сказать нечего.
        if advice is not None and not advice.durable:
            continue
        if advice is None and not _was_durable(key, memory):
            continue
        value = advice.value if advice else 0.0
        if seen.last_value <= 0 or value > seen.last_value * (1 - BETTER_BY):
            continue
        done.append({
            "rule": seen.rule,
            "subject": seen.subject,
            "was": seen.last_value,
            "now": value,
            "days": round(days_since(seen.last_sent_at, now) or 0),
            # Имя берётся из памяти, если правило проблемы уже не видит:
            # именно в этом случае назвать его не из чего.
            "who": (advice.who if advice else "") or seen.label,
        })
    done.sort(key=lambda row: -(row["was"] - row["now"]))
    return done


# Правила о событиях. Их советы не хвалят за исчезновение: событие уходит из
# окна само, по календарю. Список здесь, а не в правилах, потому что решать
# это должен отбор — правило может исчезнуть из набора вместе со своими
# кандидатами, и тогда судить о старой записи в памяти было бы не по чему.
EPISODIC: frozenset[str] = frozenset({"deal_returned", "deal_left_work"})


def _was_durable(key: tuple[str, str], memory: dict[tuple[str, str], Memory]) -> bool:
    return key[0] not in EPISODIC


# --------------------------------------------------------------------------
# хранилище
# --------------------------------------------------------------------------

def load(conn) -> dict[tuple[str, str], Memory]:
    rows = conn.execute(
        "SELECT rule, subject, first_sent_at, last_sent_at, sent_count,"
        " first_value, last_value, label, closed_at FROM advice_log"
    ).fetchall()
    memory = {}
    for row in rows:
        item = Memory(*row)
        memory[item.key] = item
    return memory


def remember(conn, selection: Selection, *, now: datetime | None = None) -> None:
    """Записать сказанное и закрыть сработавшее.

    Пишется после отправки, а не до: сводка, упавшая на отправке, не должна
    замолчать про эту проблему на неделю.
    """
    stamp = (now or _now()).isoformat()
    for advice in selection.advices:
        conn.execute(
            """
            INSERT INTO advice_log(rule, subject, first_sent_at, last_sent_at,
                                   sent_count, first_value, last_value, label,
                                   closed_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, NULL)
            ON CONFLICT(rule, subject) DO UPDATE SET
                last_sent_at = excluded.last_sent_at,
                sent_count = advice_log.sent_count + 1,
                last_value = excluded.last_value,
                label = excluded.label,
                -- Совет вернулся после закрытия: это новая история, и
                -- считать её продолжением старой значит потерять момент,
                -- когда проблема вернулась.
                first_sent_at = CASE WHEN advice_log.closed_at IS NULL
                                     THEN advice_log.first_sent_at
                                     ELSE excluded.first_sent_at END,
                first_value = CASE WHEN advice_log.closed_at IS NULL
                                   THEN advice_log.first_value
                                   ELSE excluded.first_value END,
                closed_at = NULL
            """,
            (advice.rule, advice.subject, stamp, stamp, advice.value,
             advice.value, advice.who),
        )
    for row in selection.resolved:
        conn.execute(
            "UPDATE advice_log SET closed_at = ?, last_value = ?"
            " WHERE rule = ? AND subject = ?",
            (stamp, row["now"], row["rule"], row["subject"]),
        )


def history(conn, limit: int = 50) -> list[dict[str, Any]]:
    """Что советовали и чем кончилось — для страницы и для разбора."""
    rows = conn.execute(
        "SELECT rule, subject, first_sent_at, last_sent_at, sent_count,"
        " first_value, last_value, label, closed_at FROM advice_log"
        " ORDER BY last_sent_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(zip(
        ("rule", "subject", "first_sent_at", "last_sent_at", "sent_count",
         "first_value", "last_value", "label", "closed_at"), row,
    )) for row in rows]


def only_named(candidates: Iterable[Advice]) -> list[Advice]:
    """Отсечь советы без имени и числа.

    Последний рубеж, а не основной: правило обязано не создавать таких
    вовсе. Но правило пишет человек, и «сделайте что-нибудь с воронкой»
    появится однажды само — лучше пусть не дойдёт до сводки.
    """
    named = []
    for advice in candidates:
        if not advice.subject or not advice.action or advice.value <= 0:
            logger.warning("совет без адресата или числа отброшен: %s", advice.rule)
            continue
        named.append(advice)
    return named
