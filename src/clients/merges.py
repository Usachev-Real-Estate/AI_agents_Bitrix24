"""Переезд ключа клиента (раздел 2.5 ТЗ).

Телефон исправили, контакт слили, контрагента признали агентом — ключ
клиента меняется. Клиент при этом тот же человек, и всё, что о нём знают,
обязано переехать вместе с ключом: карточки, лента, псевдонимы и разборы.

Разборы — единственное, чего нельзя пересобрать: карточки, события и
псевдонимы выводятся из витрины и портала, а разбор написал человек или
модель. Поэтому правило переезда сформулировано от них: **строка прежнего
клиента удаляется только тогда, когда всё его хозяйство доехало до одного
нового ключа.** Остальные случаи оставляют строку на месте и жалуются в
лог. Осиротевший клиент с нулями в списке — неприятность; потерянный
разбор — потеря.

``reviewed_through`` переехавшего разбора не меняется. Если у нового ключа
есть события новее, разбор честно окажется устаревшим: признак `stale`
считается сравнением ``last_event_at > reviewed_through`` (раздел 7.3), и
подпирать его переносом отметки значит объявить прочитанным то, что
прочитано по другой истории.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Sequence

from clients.keys import Decision
from clients.schema import ALIAS_KEY, CLIENT_CHILD_TABLES, ENTITY_DEAL

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    """Один переезд: весь прежний ключ целиком уехал в новый."""

    old_key: str
    new_key: str
    reason: str
    deals: tuple[int, ...]


@dataclass(frozen=True)
class MigrationPlan:
    """Что переезжает и что переехать не смогло."""

    migrations: tuple[Migration, ...]
    split: tuple[str, ...]


def current_links(conn) -> dict[int, str]:
    """Прежняя привязка сделок к клиентам: сделка → ключ."""
    rows = conn.execute(
        "SELECT entity_id, client_key FROM client_links WHERE entity_type = ?",
        (ENTITY_DEAL,),
    ).fetchall()
    return {int(row[0]): row[1] for row in rows}


def plan_migrations(
    links: Mapping[int, str],
    decisions: Mapping[int, Decision],
) -> MigrationPlan:
    """Сравнить прежние ключи с новыми и найти переезды.

    Функция чистая: ей нужны прежняя привязка из базы и новые решения из
    ``keys.assign_keys``, и ничего больше.

    Переездом считается только полный переезд: **все** карточки прежнего
    ключа известны новому прогону и **все** указывают на один и тот же
    новый ключ. Условия нарочно узкие:

    * карточка прежнего ключа, которой нет в решениях, — это не переезд,
      а выпавшая из портфеля сделка. Утверждать по ней, что клиент куда-то
      уехал, не из чего, и удалять его строку с разбором тем более;
    * карточки, разъехавшиеся по нескольким новым ключам, — раскол. Куда
      девать разбор, написанный про клиента целиком, правила нет, поэтому
      прежняя строка остаётся, а случай попадает в ``split`` и в лог. При
      нынешнем правиле ключа раскол невозможен — каждый ключ принадлежит
      ровно одному контакту, — но «невозможно» держится на правиле, а не
      на схеме, и молча терять разбор при его смене нельзя.
    """
    by_old: dict[str, set[int]] = {}
    for deal_id, old_key in links.items():
        by_old.setdefault(old_key, set()).add(deal_id)

    migrations: list[Migration] = []
    split: list[str] = []
    for old_key in sorted(by_old):
        deals = by_old[old_key]
        if not deals <= set(decisions):
            continue
        targets = {decisions[deal_id].key for deal_id in deals}
        if len(targets) > 1:
            split.append(old_key)
            continue
        new_key = targets.pop()
        if new_key == old_key:
            continue
        migrations.append(Migration(
            old_key=old_key,
            new_key=new_key,
            reason=decisions[min(deals)].key_reason,
            deals=tuple(sorted(deals)),
        ))

    if split:
        logger.warning(
            "Карточки %d прежних ключей разъехались — клиенты оставлены как есть: %s",
            len(split), ", ".join(split[:5]),
        )
    return MigrationPlan(migrations=tuple(migrations), split=tuple(split))


def apply_migrations(conn, migrations: Sequence[Migration], *, at: str) -> int:
    """Перенацелить всё хозяйство переехавших ключей. Возвращает число переездов.

    Вызывается в начале своей транзакции: временная таблица нужна ровно на
    время переноса. DDL по временной базе открытую транзакцию не коммитит
    (проверено на python 3.11 / sqlite 3.45), то есть перенос атомарен —
    упавший посередине прогон не оставит половину хозяйства у мёртвого
    ключа.

    Перенос идёт ОДНИМ оператором на таблицу, через соединение с таблицей
    переездов, а не по одному ключу за раз. Это не про скорость: ключи
    умеют меняться местами. Телефон, переехавший с одного контакта на
    другой, даёт одновременно ``p:X → c:77`` и ``c:88 → p:X``, и построчный
    перенос в неудачном порядке утащил бы строки второго ключа следом за
    первым. Один оператор читает соответствие из отдельной таблицы, а не из
    обновляемой, и каждая строка переезжает ровно один раз.

    Первичные ключи при этом не трогаются: переносится только ``client_key``,
    а ``(alias_type, alias_value)``, ``(entity_type, entity_id)`` и
    ``(kind, source_id)`` остаются прежними — столкнуться на вставке нечему.
    """
    if not migrations:
        return 0

    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS key_move ("
        " old_key TEXT PRIMARY KEY, new_key TEXT NOT NULL)"
    )
    conn.execute("DELETE FROM key_move")
    conn.executemany(
        "INSERT INTO key_move(old_key, new_key) VALUES (?, ?)",
        [(item.old_key, item.new_key) for item in migrations],
    )

    for table in CLIENT_CHILD_TABLES:
        conn.execute(
            f"UPDATE {table} SET client_key = ("
            f" SELECT new_key FROM key_move WHERE old_key = {table}.client_key)"
            f" WHERE client_key IN (SELECT old_key FROM key_move)"
        )

    # Прежний ключ остаётся дорогой к клиенту: по нему придут ссылки,
    # закладки и разборы, отправленные до переезда.
    conn.executemany(
        "INSERT INTO client_aliases(client_key, alias_type, alias_value)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(alias_type, alias_value) DO UPDATE SET"
        " client_key = excluded.client_key",
        [(item.new_key, ALIAS_KEY, item.old_key) for item in migrations],
    )
    # Ключ, вернувшийся к себе после цепочки переездов, ведёт сам на себя.
    # Такая запись ничего не ищет и только путает читающего.
    conn.execute(
        "DELETE FROM client_aliases WHERE alias_type = ? AND alias_value = client_key",
        (ALIAS_KEY,),
    )

    conn.executemany(
        "INSERT INTO client_merges(old_key, new_key, reason, detected_at)"
        " VALUES (?, ?, ?, ?)",
        [(item.old_key, item.new_key, item.reason, at) for item in migrations],
    )

    # Удаляется только тот прежний ключ, который никому не стал новым.
    # Ключ, одновременно освобождённый и занятый (телефон перешёл к другому
    # контакту), сносить нельзя: его хозяйство уже перенацелено на него же,
    # а строку клиента перепишет ближайшая же вставка пересборки.
    conn.execute(
        "DELETE FROM clients WHERE client_key IN (SELECT old_key FROM key_move)"
        " AND client_key NOT IN (SELECT new_key FROM key_move)"
    )
    conn.execute("DROP TABLE IF EXISTS temp.key_move")

    logger.info("Переездов ключа: %d", len(migrations))
    return len(migrations)
