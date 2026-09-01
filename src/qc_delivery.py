"""Кому уходит отчёт QC: РОПу лично или в общий чат.

Решение агентства от 31.08. Отчёт нужен РОПу, чтобы контролировать работу
своих брокеров, — значит и приходить он должен каждому свой, лично, а не
общим списком, в котором надо искать себя.

РОПы названы поимённо, а не угаданы по должности. До этого карта строилась
по подстроке в WORK_POSITION («руководитель отдела продаж», «роп»), и это
догадка: она ловит лишних и пропускает тех, у кого должность записана
иначе. Список из пяти фамилий дало агентство — он и есть источник правды.

Молчаливой недостачи тут быть не должно. Не нашли названного РОПа среди
активных пользователей, нашли двух однофамильцев, не знаем подразделения
брокера — во всех этих случаях карточки уходят в общий чат, а прогон
говорит об этом в лог. Отчёт, не дошедший ни до кого, выглядит точно так
же, как отчёт, в котором не было нарушений.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Фамилии РОПов — решение агентства от 31.08. Сравнение по фамилии в нижнем
# регистре: в портале встречаются и «Шпырная Юлия», и лишние пробелы.
ROP_SURNAMES: tuple[str, ...] = (
    "шпырная",
    "резников",
    "кретов",
    "трофимова",
    "волкова",
)

# Чей отдел разбирает владелец отчёта сам. Волкова личных сообщений не
# получает — её карточки уходят в общий чат отдельным сообщением, чтобы их
# распределили по брокерам вручную.
ROP_TO_CHAT: frozenset[str] = frozenset({"волкова"})


@dataclass(frozen=True)
class Rop:
    """Найденный в портале РОП."""

    user_id: int
    surname: str
    full_name: str

    @property
    def personal(self) -> bool:
        """Слать ли ему лично."""
        return self.surname not in ROP_TO_CHAT


def _clean(value: Any) -> str:
    return str(value or "").strip()


def build_rop_directory(users: list[dict[str, Any]]) -> dict[int, Rop]:
    """user_id → РОП, по списку фамилий.

    Однофамильцев не разрешаем догадкой: если под фамилию подходят двое,
    ни один не берётся, а прогон говорит об этом вслух. Ошибиться тут —
    значит отправить отчёт чужого отдела постороннему человеку.
    """
    by_surname: dict[str, list[dict[str, Any]]] = {}
    for user in users:
        if not isinstance(user, dict):
            continue
        surname = _clean(user.get("LAST_NAME")).lower()
        if surname in ROP_SURNAMES:
            by_surname.setdefault(surname, []).append(user)

    directory: dict[int, Rop] = {}
    for surname in ROP_SURNAMES:
        found = by_surname.get(surname) or []
        if not found:
            logger.warning(
                "РОП «%s» не найден среди активных пользователей — "
                "его карточки уйдут в общий чат", surname,
            )
            continue
        if len(found) > 1:
            logger.warning(
                "Под фамилию «%s» подходят %d пользователей (%s) — "
                "лично никому не шлём, карточки уйдут в общий чат",
                surname, len(found),
                ", ".join(_clean(u.get("ID")) for u in found),
            )
            continue
        user = found[0]
        uid = int(_clean(user.get("ID")) or 0)
        if not uid:
            continue
        full = " ".join(
            part for part in (
                _clean(user.get("LAST_NAME")), _clean(user.get("NAME")),
            ) if part
        )
        directory[uid] = Rop(user_id=uid, surname=surname, full_name=full)
    return directory


# Ключ группы для карточек, у которых РОПа определить не удалось: брокер без
# подразделения, подразделение без РОПа, РОП не найден или неоднозначен.
UNASSIGNED = 0


def group_deals_by_rop(
    deals: list[dict[str, Any]],
    broker_dept_map: dict[int, int],
    rop_map: dict[int, int],
    directory: dict[int, Rop],
) -> dict[int, list[dict[str, Any]]]:
    """Сделки по РОПам; всё неопознанное — в UNASSIGNED.

    Именно «в UNASSIGNED», а не «мимо»: карточка, выпавшая из рассылки
    молча, ничем не отличается от карточки, по которой не было вопросов.
    """
    groups: dict[int, list[dict[str, Any]]] = {}
    for deal in deals:
        broker_id = int(_clean(deal.get("ASSIGNED_BY_ID")) or 0)
        dept_id = broker_dept_map.get(broker_id) or 0
        rop_id = int(rop_map.get(dept_id) or 0)
        # РОП отдела найден, но в списке агентства его нет — значит лично
        # ему мы не пишем: список из пяти фамилий и есть решение о том, кто
        # получает отчёт.
        key = rop_id if rop_id in directory else UNASSIGNED
        groups.setdefault(key, []).append(deal)
    return groups


def split_delivery(
    groups: dict[int, list[dict[str, Any]]],
    directory: dict[int, Rop],
) -> tuple[list[tuple[Rop, list[dict[str, Any]]]], list[dict[str, Any]]]:
    """Разложить группы на «лично» и «в общий чат».

    В чат уходят карточки отдела, чей РОП личных сообщений не получает
    (Волкова), и всё, что осталось без РОПа. Возвращаем их одним списком:
    в чате это одно сообщение, а не два одинаковых по смыслу.
    """
    personal: list[tuple[Rop, list[dict[str, Any]]]] = []
    to_chat: list[dict[str, Any]] = []
    for key, deals in groups.items():
        rop = directory.get(key)
        if rop is not None and rop.personal:
            personal.append((rop, deals))
        else:
            to_chat.extend(deals)
    personal.sort(key=lambda pair: pair[0].surname)
    return personal, to_chat
