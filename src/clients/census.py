"""Счётчики портфеля: чем кончились ключи и почему (раздел 2 ТЗ, диагностика).

Правило ключа написано и проверено тестами, но тест отвечает на вопрос
«делает ли код то, что задумано», а не «задумано ли верно». Второй ответ
даёт только живой портфель, и даёт его числами: сколько клиентов склеилось
по телефону, скольким это запретил признак агента, сколько номеров спорны.

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

# Во что складывать причины признака агента. Порядок важен: строки причин
# приходят из classify_counterparty свободным текстом, и первая подошедшая
# выигрывает. Самая слабая ветка — «название сделки»: её и усиливает
# правило накопления по контакту, поэтому смотреть на неё нужно отдельно.
_AGENT_BUCKETS = (
    ("тип контакта", "тип контакта"),
    ("должность", "должность"),
    ("в имени контакта", "имя контакта"),
    ("в названии сделки", "название сделки"),
    (AGENT_STAGE_WHY, "стадия «Агент»"),
)
_AGENT_OTHER = "прочее"

# Что конфликт телефона означает на самом деле. ТЗ отказывается склеивать,
# предполагая двух разных людей; в CRM чаще встречается обратное — один
# человек, заведённый дважды, то есть ровно тот случай, ради которого
# телефонный ключ и существует. Различить их можно по именам контактов, и
# для этого не нужно ни одного имени показывать: достаточно сказать,
# совпали они или нет.
SAME_PERSON = "тот же человек"
SAME_SURNAME = "одна фамилия, разные имена"
DIFFERENT = "разные люди"
UNKNOWN = "имён нет"


@dataclass(frozen=True)
class Conflicts:
    """Спорные номера, разложенные по тому, чем они объясняются."""

    total: int = 0
    same_person: int = 0
    same_surname: int = 0
    different: int = 0
    unknown: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "всего": self.total,
            SAME_PERSON: self.same_person,
            SAME_SURNAME: self.same_surname,
            DIFFERENT: self.different,
            UNKNOWN: self.unknown,
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
    conflicts: Conflicts = field(default_factory=Conflicts)


def _bucket(reason: str) -> str:
    for marker, name in _AGENT_BUCKETS:
        if marker in reason:
            return name
    return _AGENT_OTHER


def _name(contact: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """Фамилия и имя контакта в сравнимом виде. None — контакта нет."""
    if not contact:
        return None
    last = str(contact.get("LAST_NAME") or "").strip().casefold()
    first = str(contact.get("NAME") or "").strip().casefold()
    return last, first


def _verdict(ids: tuple[int, ...], contacts: Mapping[int, Mapping[str, Any]]) -> str:
    """Чем объясняется один спорный номер."""
    names = [_name(contacts.get(contact_id)) for contact_id in ids]
    if any(name is None for name in names):
        return UNKNOWN
    surnames = {name[0] for name in names if name}
    if not surnames or "" in surnames:
        return UNKNOWN
    if len(surnames) > 1:
        return DIFFERENT
    firsts = {name[1] for name in names if name}
    if len(firsts) == 1 and "" not in firsts:
        return SAME_PERSON
    return SAME_SURNAME


def take(
    assignment: Assignment,
    contacts: Mapping[int, Mapping[str, Any]],
) -> Census:
    """Пересчитать портфель.

    Считается по клиентам: вопрос «сколько людей не склеилось по телефону»,
    а не «сколько карточек». Карточек у одного клиента может быть
    несколько, и они бы его переголосовали.
    """
    by_key = {}
    for decision in assignment.decisions.values():
        by_key.setdefault(decision.key, decision)

    by_kind: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    agents_by_reason: dict[str, int] = {}
    agents = 0
    for key, decision in by_key.items():
        kind = key.split(":", 1)[0]
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_reason[decision.key_reason] = by_reason.get(decision.key_reason, 0) + 1
        if decision.is_agent:
            agents += 1
            name = _bucket(decision.agent_reason)
            agents_by_reason[name] = agents_by_reason.get(name, 0) + 1

    counted = {SAME_PERSON: 0, SAME_SURNAME: 0, DIFFERENT: 0, UNKNOWN: 0}
    for ids in assignment.conflicts.values():
        counted[_verdict(ids, contacts)] += 1

    return Census(
        cards=len(assignment.decisions),
        clients=len(by_key),
        by_kind=dict(sorted(by_kind.items())),
        by_reason=dict(sorted(by_reason.items(), key=lambda pair: -pair[1])),
        agents=agents,
        agents_by_reason=dict(sorted(agents_by_reason.items(), key=lambda pair: -pair[1])),
        conflicts=Conflicts(
            total=len(assignment.conflicts),
            same_person=counted[SAME_PERSON],
            same_surname=counted[SAME_SURNAME],
            different=counted[DIFFERENT],
            unknown=counted[UNKNOWN],
        ),
    )
