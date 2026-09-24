"""Счётчики портфеля: чем кончились ключи и почему (раздел 2 ТЗ, диагностика).

Правило ключа написано и проверено тестами, но тест отвечает на вопрос
«делает ли код то, что задумано», а не «задумано ли верно». Второй ответ
даёт только живой портфель, и даёт его числами.

Здесь нет ни одного имени и ни одного телефона — только счётчики. Разбор
портфеля читают люди, которым имена клиентов видеть незачем, и попадает он
в лог крона, который потом пересылают.

Отдельным модулем, а не в keys.py: тот отвечает на вопрос «какой ключ у
этой карточки» и обязан остаться чистым. Диагностика к правилу не
относится и меняться будет чаще него.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from clients.keys import AGENT_STAGE_WHY, Assignment
from clients.names import fingerprint, name_parts

# Во что складывать причины признака агента. Порядок важен: строки причин
# приходят из classify_counterparty свободным текстом, и первая подошедшая
# выигрывает.
_AGENT_BUCKETS = (
    ("тип контакта", "тип контакта"),
    ("должность", "должность"),
    ("в имени контакта", "имя контакта"),
    ("в названии сделки", "название сделки"),
    (AGENT_STAGE_WHY, "стадия «Агент»"),
)
_AGENT_OTHER = "прочее"

# Чем объясняется номер, оставшийся спорным.
#
# Разряды считаются по номерам, которые правило склеить ОТКАЗАЛОСЬ, —
# склеенные уходят в свой счётчик. Поэтому «тот же человек» здесь не ноль
# только в одном случае: имя совпало, но это же имя встретилось и на
# другом общем номере, то есть перед нами заполнитель, а не человек
# (см. split_shared). Этот разряд и есть цена защиты от заполнителей —
# её видно в каждом прогоне, а не только когда что-то разъехалось.
SAME_PERSON = "тот же человек"
SAME_SURNAME = "одна фамилия, разные имена"
DIFFERENT = "разные люди"
ONE_NAMELESS = "имя есть только у одного"
NO_NAMES = "имён нет ни у кого"

_VERDICTS = (SAME_PERSON, SAME_SURNAME, DIFFERENT, ONE_NAMELESS, NO_NAMES)


@dataclass(frozen=True)
class Linked:
    """Что известно о карточке помимо ключа: воронка и ответственный."""

    category_id: int | None = None
    assignee_id: int | None = None


@dataclass(frozen=True)
class Conflicts:
    """Спорные номера, разложенные по тому, чем они объясняются."""

    total: int = 0
    by_verdict: dict[str, int] = field(default_factory=dict)
    by_owners: dict[str, int] = field(default_factory=dict)
    both_funnels: int = 0
    several_brokers: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "всего": self.total,
            **self.by_verdict,
            "контактов на номере": self.by_owners,
            "в разных воронках": self.both_funnels,
            "у разных брокеров": self.several_brokers,
        }


@dataclass(frozen=True)
class Census:
    """Портфель в числах. Считается по КЛИЕНТАМ, а не по карточкам."""

    cards: int = 0
    clients: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    agents: int = 0
    agents_by_reason: dict[str, int] = field(default_factory=dict)
    both_funnels: int = 0
    several_brokers: int = 0
    merged_phones: int = 0
    merged_contacts: int = 0
    conflicts: Conflicts = field(default_factory=Conflicts)


def _bucket(reason: str) -> str:
    for marker, name in _AGENT_BUCKETS:
        if marker in reason:
            return name
    return _AGENT_OTHER


def _verdict(ids: tuple[int, ...], contacts: Mapping[int, Mapping[str, Any]]) -> str:
    """Чем объясняется один общий номер.

    Сравнивается имя ЦЕЛИКОМ, а не только фамилия. В карточках, заведённых
    автоматически по входящему звонку, фамилия чаще всего пуста, а имя
    лежит в одном поле строкой; первая редакция этого разбора опёрлась на
    фамилию и отправила в «не знаем» 231 конфликт из 237.
    """
    named = [name_parts(contacts.get(contact_id)) for contact_id in ids]
    known = [parts for parts in named if parts]
    if not known:
        return NO_NAMES
    if len(known) < len(named):
        # Безымянный огрызок против живой карточки. Почти наверняка та же
        # запись, заведённая автоматом, — но «почти» здесь и есть ответ.
        return ONE_NAMELESS
    if len({fingerprint(contacts.get(contact_id)) for contact_id in ids}) == 1:
        return SAME_PERSON
    surnames = {parts[0] for parts in known}
    return SAME_SURNAME if len(surnames) == 1 else DIFFERENT


def _owners_bucket(count: int) -> str:
    """Номер на двоих — скорее дубль; номер на пятерых — офис или агент."""
    return str(count) if count < 4 else "4+"


def take(
    assignment: Assignment,
    contacts: Mapping[int, Mapping[str, Any]],
    *,
    cards: Mapping[int, Linked] | None = None,
) -> Census:
    """Пересчитать портфель.

    Считается по клиентам: вопрос «сколько людей не склеилось по телефону»,
    а не «сколько карточек». Карточек у одного клиента может быть
    несколько, и они бы его переголосовали.
    """
    linked = cards or {}
    by_key: dict[str, list[int]] = {}
    for deal_id, decision in sorted(assignment.decisions.items()):
        by_key.setdefault(decision.key, []).append(deal_id)
    first = {key: assignment.decisions[deals[0]] for key, deals in by_key.items()}

    by_kind: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    agents_by_reason: dict[str, int] = {}
    agents = both_funnels = several_brokers = 0
    for key, decision in first.items():
        by_kind[key.split(":", 1)[0]] = by_kind.get(key.split(":", 1)[0], 0) + 1
        by_reason[decision.key_reason] = by_reason.get(decision.key_reason, 0) + 1
        if decision.is_agent:
            agents += 1
            name = _bucket(decision.agent_reason)
            agents_by_reason[name] = agents_by_reason.get(name, 0) + 1
        funnels, brokers = _spread(by_key[key], linked)
        both_funnels += len(funnels) > 1
        several_brokers += len(brokers) > 1

    return Census(
        cards=len(assignment.decisions),
        clients=len(by_key),
        by_kind=dict(sorted(by_kind.items())),
        by_reason=dict(sorted(by_reason.items(), key=lambda pair: -pair[1])),
        agents=agents,
        agents_by_reason=dict(sorted(agents_by_reason.items(), key=lambda pair: -pair[1])),
        both_funnels=both_funnels,
        several_brokers=several_brokers,
        merged_phones=len(assignment.merged),
        merged_contacts=len({i for ids in assignment.merged.values() for i in ids}),
        conflicts=_conflicts(assignment, contacts, linked),
    )


def _spread(deal_ids: list[int], linked: Mapping[int, Linked]) -> tuple[set, set]:
    """Воронки и ответственные по набору карточек."""
    funnels = {linked[d].category_id for d in deal_ids if d in linked}
    brokers = {linked[d].assignee_id for d in deal_ids if d in linked}
    return funnels - {None}, brokers - {None}


def _conflicts(
    assignment: Assignment,
    contacts: Mapping[int, Mapping[str, Any]],
    linked: Mapping[int, Linked],
) -> Conflicts:
    """Разложить спорные номера.

    Кроме имён смотрим, что за карточками стоит: один человек бывает и
    собственником, и покупателем, и ведут его разные брокеры. Разные
    воронки или разные ответственные на одном номере — довод за то, что
    перед нами один человек в двух ролях, а не двое разных.
    """
    deals_of: dict[int, list[int]] = {}
    for deal_id, decision in sorted(assignment.decisions.items()):
        if decision.contact_id:
            deals_of.setdefault(decision.contact_id, []).append(deal_id)

    by_verdict = dict.fromkeys(_VERDICTS, 0)
    by_owners: dict[str, int] = {}
    both_funnels = several_brokers = 0
    for ids in assignment.conflicts.values():
        by_verdict[_verdict(ids, contacts)] += 1
        bucket = _owners_bucket(len(ids))
        by_owners[bucket] = by_owners.get(bucket, 0) + 1
        deal_ids = [d for contact_id in ids for d in deals_of.get(contact_id, ())]
        funnels, brokers = _spread(deal_ids, linked)
        both_funnels += len(funnels) > 1
        several_brokers += len(brokers) > 1

    return Conflicts(
        total=len(assignment.conflicts),
        by_verdict=dict(by_verdict),
        by_owners=dict(sorted(by_owners.items())),
        both_funnels=both_funnels,
        several_brokers=several_brokers,
    )
