"""Кто по ту сторону карточки: клиент или агент.

Разница практическая. Агент — не клиент: он не «остывает», ему не нужны
подборки и просмотры «для себя», и требовать от брокера развёрнутого
рассказа о его мотивации бессмысленно. Спутать их — значит либо предъявить
брокеру за холодность там, где её не бывает, либо, наоборот, принять
агентскую заявку за живого покупателя и посчитать её потерянной.

Признак ищется в CRM, а не в голове модели: сначала тип контакта (его
проставляют руками и именно для этого), потом должность, потом имя и
название сделки — брокеры подписывают такие карточки словом «агент», и
это самый частый признак на портале.

В state уходит только источник признака, но не сам текст: имя контакта —
персональные данные, а состояние карточки лежит в базе замаскированным.
"""

from __future__ import annotations

import re
from typing import Any

WHO_CLIENT = "client"
WHO_AGENT = "agent"

# «агентство» — это мы сами, а не контрагент: в названиях сделок оно
# попадается («агентство продавца»), и без отсечки каждая такая карточка
# уезжала бы в агентские.
_AGENT_RE = re.compile(r"агент(?!ств)|риел?тор|риэлтор|маклер", re.I)

_NAME_FIELDS = ("NAME", "LAST_NAME", "SECOND_NAME")

# Справочник типов контакта читается один раз за прогон: он общий для
# портала и меняется раз в год.
_TYPE_NAMES: dict[str, str] = {}


def set_contact_type_names(names: dict[str, str]) -> None:
    """Подставить справочник типов контакта на весь прогон."""
    _TYPE_NAMES.clear()
    _TYPE_NAMES.update(names or {})


def _is_agent(text: Any) -> bool:
    return bool(_AGENT_RE.search(str(text or "")))


def classify_counterparty(
    deal: dict[str, Any],
    contacts: list[dict[str, Any]] | None = None,
    type_names: dict[str, str] | None = None,
) -> dict[str, str]:
    """Клиент или агент по ту сторону сделки.

    Возвращает {"who": "client"|"agent", "why": "<откуда признак>"}.
    По умолчанию — клиент: агентская карточка это исключение, и молчание
    CRM должно читаться как «обычный клиент», а не как «непонятно кто».
    """
    type_names = type_names or _TYPE_NAMES
    for contact in contacts or []:
        if not isinstance(contact, dict):
            continue
        type_id = str(contact.get("TYPE_ID") or contact.get("type_id") or "").strip()
        type_name = type_names.get(type_id, "")
        if _is_agent(type_name):
            return {"who": WHO_AGENT, "why": f"тип контакта «{type_name}»"}
        if _is_agent(contact.get("POST")):
            return {"who": WHO_AGENT, "why": "должность контакта"}
        if any(_is_agent(contact.get(field)) for field in _NAME_FIELDS):
            return {"who": WHO_AGENT, "why": "пометка «агент» в имени контакта"}

    title = deal.get("TITLE") or deal.get("title")
    if _is_agent(title):
        return {"who": WHO_AGENT, "why": "пометка «агент» в названии сделки"}

    return {"who": WHO_CLIENT, "why": ""}
