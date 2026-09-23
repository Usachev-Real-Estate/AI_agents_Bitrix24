"""Добор контактов из портала (раздел 2.2 ТЗ).

Витрина **не хранит контактов вообще** `[V9]`: ни таблицы, ни имени, ни
телефона — только `contact_id` числом на карточке сделки. Значит сам ключ
клиента из витрины не вычисляется, и без этого добора клиентского слоя нет.

Берутся только те контакты, на которые ссылаются карточки портфеля, и
берутся пачками по пятьдесят — размер страницы портала. Фильтр `@ID`, а не
`ID`: список значений в Битриксе задаётся префиксом `@` (документация
crm.contact.list), и без него фильтр по списку либо игнорируется, либо
понимается иначе, а ответ в обоих случаях выглядит правдоподобно.

**Инкремента по DATE_MODIFY здесь нет, и это решение, а не недоделка.**
ТЗ описывает его как «последующие прогоны берут только изменившиеся,
остальное из кэша». Кэш пришлось бы восстанавливать из `client_aliases`, а
он не видит удалений: телефон, стёртый в портале, остался бы в кэше
навсегда и продолжал бы склеивать людей, которых уже ничего не связывает.
Полный добор по списку идентификаторов стоит порядка двадцати-шестидесяти
запросов раз в сутки и не умеет ошибаться таким образом.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

# Разбор конверта ответа портала берётся готовый: форма ответа зависит от
# метода (список в result, в result.items, в result.categories), и вторая
# копия этого знания разойдётся с первой на первом же новом методе.
from analytics.client import _as_records

logger = logging.getLogger(__name__)

# Поля, которых хватает на ключ, имя и признак агента. Лишнего не просим:
# переписка и почта клиенту в базе разбора не нужны.
CONTACT_SELECT = ("ID", "NAME", "LAST_NAME", "SECOND_NAME", "PHONE", "TYPE_ID", "POST")

# Размер страницы портала. Просить больше бессмысленно — вернут пятьдесят.
BATCH = 50


@dataclass(frozen=True)
class ContactFetch:
    """Что удалось забрать и чего не удалось."""

    contacts: dict[int, dict[str, Any]]
    missing: tuple[int, ...]
    failed: tuple[int, ...]

    @property
    def complete(self) -> bool:
        """Прогон полон, только если портал ответил на всё.

        `missing` полнотой не считается: контакт, удалённый в CRM, — это
        ответ портала, а не его отказ. Сделки такого контакта всё равно
        соберутся под ключ `c:<id>` и клиента не потеряют.
        """
        return not self.failed


def fetch_contacts(
    client,
    ids: Sequence[int],
    *,
    batch: int = BATCH,
) -> ContactFetch:
    """Карточки контактов по списку идентификаторов.

    Упавшая пачка не роняет прогон: она превращается в признак неполноты.
    Клиентский слой при неполном прогоне не перезаписывает агрегаты
    (раздел 4, шаг 2), и это куда лучше, чем оставить портфель без
    пересборки из-за одного таймаута портала.
    """
    wanted = tuple(sorted({int(value) for value in ids if value}))
    contacts: dict[int, dict[str, Any]] = {}
    failed: list[int] = []

    for start in range(0, len(wanted), batch):
        chunk = wanted[start:start + batch]
        try:
            rows = _as_records(client.call("crm.contact.list", {
                "filter": {"@ID": list(chunk)},
                "select": list(CONTACT_SELECT),
            }))
        except Exception as error:  # noqa: BLE001 - причина уходит в лог и в прогон
            logger.warning(
                "Контакты %d–%d не забраны: %s", chunk[0], chunk[-1], error,
            )
            failed.extend(chunk)
            continue
        for row in rows:
            contact_id = _int(row.get("ID"))
            if contact_id:
                contacts[contact_id] = row

    missing = tuple(
        value for value in wanted if value not in contacts and value not in set(failed)
    )
    if missing:
        logger.info("Портал не знает %d контактов — сделки уйдут под ключ c:", len(missing))
    if failed:
        logger.warning("Не забрано контактов: %d — прогон неполон", len(failed))
    return ContactFetch(contacts=contacts, missing=missing, failed=tuple(failed))


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0
