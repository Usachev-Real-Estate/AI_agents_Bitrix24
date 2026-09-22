"""Правило ключа клиента (раздел 2.4 ТЗ).

Ключ отвечает на вопрос «какие карточки — это один и тот же человек».
Ошибка в обе стороны стоит дорого и по-разному: не склеили — брокер видит
одного покупателя дважды и дважды ему звонит; склеили лишнего — сорок
разных покупателей превращаются в одного «клиента» с одним ответственным
из сорока, и половина портфеля исчезает из отчётов.

Приоритет:

1. ``p:+7XXXXXXXXXX`` — нормализованный телефон, если контрагент не агент;
2. ``c:<CONTACT_ID>`` — контакт есть, телефон не годится;
3. ``d:<DEAL_ID>`` — нет ни того, ни другого.

**Агент по телефону не склеивается, и признак агента накапливается по
контакту.** Решение агентства: одна сделка назвала контрагента агентом —
агент и все остальные сделки этого контакта. Без накопления признак
зависел бы от карточки: у `classify_counterparty` есть ветка по названию
СДЕЛКИ, поэтому один и тот же контакт классифицировался бы агентом в той
сделке, где брокер подписал «агент» в заголовке, и клиентом в соседней. Две
сделки одного человека уезжали бы к разным клиентам, а правка заголовка
молча переносила бы карточку.

**Отсюда главное следствие: ключи считаются по всему портфелю разом.**
Функция не складывается из кусков — ``assign_keys`` над половиной карточек
может назвать клиентом того, кого над целым назвала бы агентом. Прогон с
``--limit`` ключи писать не имеет права; для этого в журнале прогонов уже
есть ``complete``.

Нормализация телефона берётся готовая, ``kc_owner_import.normalize_phone``:
правило первой редакции ТЗ («ведущую 8 заменить на 7; если 10 цифр —
добавить 7») ломает любой десятизначный номер, начинающийся с восьмёрки,
а существующая функция проверяет длину ДО замены и потому верна.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from counterparty import WHO_AGENT, classify_counterparty
from kc_owner_import import normalize_phone

logger = logging.getLogger(__name__)

KEY_PHONE = "p"
KEY_CONTACT = "c"
KEY_DEAL = "d"

# Стадия «Агент» воронки покупателей (tools.py:75). Задана идентификатором,
# а не именем: урок раздела 6.1 ТЗ — в воронке продавцов имена стадий другие,
# и правило по именам там промахивается молча.
#
# Проверка живёт здесь, а не в classify_counterparty, хотя ТЗ приписывает её
# ей: стадии в той функции нет вовсе `[V19]`. Дописывать её туда нельзя —
# функцию зовёт client_state, завершённый пилот, и менять его поведение
# ради клиентского слоя значит менять то, что уже не пересматривают.
AGENT_STAGE_IDS = frozenset({"C18:UC_2ZBA0G"})
AGENT_STAGE_WHY = "стадия «Агент»"

# Почему ключ получился таким. Хранится в clients.key_reason: вопрос
# «почему эти две карточки не один клиент» задают чаще всех остальных, и
# отвечать на него чтением кода — значит не отвечать.
WHY_PHONE = "склейка по телефону"
WHY_AGENT = "агент: склейка по телефону запрещена"
WHY_CONFLICT = "телефон числится за двумя контактами"
WHY_PHONE_INVALID = "телефон не разобран"
WHY_NO_PHONE = "у контакта нет телефона"
WHY_NO_CONTACT = "ни контакта, ни телефона"

ALIAS_PHONE = "phone"
ALIAS_CONTACT = "contact"


@dataclass(frozen=True)
class Card:
    """Карточка портфеля: сделка и её контакт.

    Контакт один, а не список: витрина хранит у сделки ровно одно поле
    ``fact_deal.contact_id``, и клиентский слой читает её, а не портал.
    """

    deal_id: int
    title: str = ""
    stage_id: str = ""
    contact_id: int | None = None


@dataclass(frozen=True)
class Decision:
    """Решение по одной карточке."""

    deal_id: int
    key: str
    key_reason: str
    contact_id: int | None
    phone_norm: str | None
    phone_raw: str
    phone_valid: bool
    is_agent: bool
    agent_reason: str
    aliases: tuple[tuple[str, str], ...] = field(default=())


@dataclass(frozen=True)
class Assignment:
    """Ключи всего портфеля и телефоны, по которым склейка запрещена."""

    decisions: dict[int, Decision]
    conflicts: dict[str, tuple[int, ...]]


def phone_candidates(contact: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """Номера контакта парами «нормализованный, как в портале».

    Нормализованный пуст, если ``normalize_phone`` отказался. Мусорный номер
    для склейки не годится, но брокеру на экране нужен — раздел 2.4 ТЗ прямо
    требует сохранить его в ``phone_raw``.

    Порядок — по нормализованному номеру, а не как отдал портал: порядок
    значений мультиполя Битрикс между прогонами не гарантирует, и ключ,
    построенный по «первому», прыгал бы от прогона к прогону, отцепляя
    разборы от клиента.
    """
    if not contact:
        return ()
    raw_values = _raw_phones(contact)
    pairs: dict[str, str] = {}
    broken: list[str] = []
    for raw in raw_values:
        try:
            pairs.setdefault(normalize_phone(raw), raw)
        except ValueError:
            broken.append(raw)
    ordered = [(norm, pairs[norm]) for norm in sorted(pairs)]
    ordered.extend(("", raw) for raw in sorted(set(broken)))
    return tuple(ordered)


def _raw_phones(contact: Mapping[str, Any]) -> list[str]:
    """Значения мультиполя PHONE как есть.

    Портал отдаёт список словарей ``{"VALUE": ..., "VALUE_TYPE": ...}``, но
    выгрузки и фикстуры кладут туда и голую строку. Разбираем оба вида:
    упасть на форме поля здесь значит потерять телефон целого контакта.
    """
    value = contact.get("PHONE") or contact.get("phone")
    if not value:
        return []
    if isinstance(value, (str, int)):
        return [str(value)]
    out: list[str] = []
    for item in value if isinstance(value, (list, tuple)) else [value]:
        if isinstance(item, Mapping):
            raw = item.get("VALUE") or item.get("value")
        else:
            raw = item
        if raw:
            out.append(str(raw))
    return out


def _agent_why(card: Card, contact: Mapping[str, Any] | None,
               type_names: Mapping[str, str] | None) -> str:
    """Признак агента по одной карточке. Пусто — обычный клиент."""
    verdict = classify_counterparty(
        {"TITLE": card.title},
        [dict(contact)] if contact else [],
        dict(type_names) if type_names else None,
    )
    if verdict["who"] == WHO_AGENT:
        return verdict["why"] or "признак агента"
    if card.stage_id in AGENT_STAGE_IDS:
        return AGENT_STAGE_WHY
    return ""


def phone_conflicts(
    cards: Iterable[Card],
    contacts: Mapping[int, Mapping[str, Any]],
) -> dict[str, tuple[int, ...]]:
    """Телефоны, которые числятся больше чем за одним контактом.

    Такой номер не склеивает, а путает: два разных человека получили бы
    один ключ. Обе карточки уходят на ``c:``, номер не становится ни ключом,
    ни псевдонимом — иначе псевдоним привёл бы к одному из двух наугад.

    Считается только по контактам, которые действительно стоят на карточках
    портфеля. Посторонний контакт, случайно попавший в справочник, не должен
    отбирать телефон у того, кто им пользуется.
    """
    used = {card.contact_id for card in cards if card.contact_id}
    owners: dict[str, set[int]] = {}
    for contact_id in used:
        for norm, _raw in phone_candidates(contacts.get(contact_id)):
            if norm:
                owners.setdefault(norm, set()).add(contact_id)
    return {
        norm: tuple(sorted(ids)) for norm, ids in sorted(owners.items()) if len(ids) > 1
    }


def assign_keys(
    cards: Sequence[Card],
    contacts: Mapping[int, Mapping[str, Any]],
    *,
    type_names: Mapping[str, str] | None = None,
) -> Assignment:
    """Ключи для всего портфеля разом.

    Разом — не оптимизация, а условие правильности: признак агента
    накапливается по контакту, и над половиной карточек ответ другой.
    См. модульный докстринг.
    """
    ordered = sorted(cards, key=lambda card: card.deal_id)
    conflicts = phone_conflicts(ordered, contacts)

    # Проход первый: кто агент. Порядок по номеру сделки, чтобы причина,
    # попавшая в карточку клиента, не зависела от порядка выдачи портала.
    agent_by_deal: dict[int, str] = {}
    agent_by_contact: dict[int, str] = {}
    for card in ordered:
        why = _agent_why(card, contacts.get(card.contact_id or 0), type_names)
        if not why:
            continue
        agent_by_deal[card.deal_id] = why
        if card.contact_id:
            agent_by_contact.setdefault(card.contact_id, why)

    if agent_by_contact:
        logger.info("Агентских контактов: %d", len(agent_by_contact))
    if conflicts:
        logger.warning(
            "Телефонов за двумя контактами: %d — склейка по ним запрещена",
            len(conflicts),
        )

    decisions: dict[int, Decision] = {}
    for card in ordered:
        decisions[card.deal_id] = _decide(
            card, contacts, conflicts, agent_by_deal, agent_by_contact,
        )
    return Assignment(decisions=decisions, conflicts=conflicts)


def _decide(
    card: Card,
    contacts: Mapping[int, Mapping[str, Any]],
    conflicts: Mapping[str, tuple[int, ...]],
    agent_by_deal: Mapping[int, str],
    agent_by_contact: Mapping[int, str],
) -> Decision:
    """Ключ одной карточки при уже известных агентах и конфликтах."""
    contact_id = card.contact_id or None
    # Контакт берётся по идентификатору с карточки, а не по тому, отдал ли
    # его портал: удалённый контакт всё ещё группирует свои сделки, и
    # ронять их в d:<сделка> значит рассыпать клиента на карточки.
    candidates = phone_candidates(contacts.get(contact_id) if contact_id else None)
    valid = tuple((norm, raw) for norm, raw in candidates if norm)
    usable = tuple((norm, raw) for norm, raw in valid if norm not in conflicts)

    agent_reason = (
        agent_by_deal.get(card.deal_id)
        or (agent_by_contact.get(contact_id, "") if contact_id else "")
    )
    is_agent = bool(agent_reason)

    aliases: list[tuple[str, str]] = []
    if contact_id:
        aliases.append((ALIAS_CONTACT, str(contact_id)))

    if usable and not is_agent:
        phone_norm, phone_raw = usable[0]
        key = f"{KEY_PHONE}:{phone_norm}"
        reason = WHY_PHONE
        # Остальные пригодные номера ведут к тому же клиенту. Телефоны
        # агента и спорные номера не попадают сюда никогда: псевдоним —
        # это «искать клиента по этому номеру», а по ним искать нельзя.
        aliases.extend((ALIAS_PHONE, norm) for norm, _ in usable[1:])
    else:
        phone_norm = None
        phone_raw = valid[0][1] if valid else (candidates[0][1] if candidates else "")
        if is_agent:
            reason = WHY_AGENT
        elif valid:
            reason = WHY_CONFLICT
        elif candidates:
            reason = WHY_PHONE_INVALID
        elif contact_id:
            reason = WHY_NO_PHONE
        else:
            reason = WHY_NO_CONTACT
        key = (
            f"{KEY_CONTACT}:{contact_id}" if contact_id else f"{KEY_DEAL}:{card.deal_id}"
        )

    return Decision(
        deal_id=card.deal_id,
        key=key,
        key_reason=reason,
        contact_id=contact_id,
        phone_norm=phone_norm,
        phone_raw=phone_raw,
        # «Телефон разобран» и «по телефону склеили» — разные вещи: у
        # агента и у спорного номера первое верно, второе нет.
        phone_valid=bool(valid),
        is_agent=is_agent,
        agent_reason=agent_reason,
        aliases=tuple(aliases),
    )
