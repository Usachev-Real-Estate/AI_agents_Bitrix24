"""Работа по карточкам: звонили или нет.

Движение по стадиям отвечает на вопрос «карточка двигалась». Этот модуль —
на вопрос «по карточке работали», и для объекта собственника это разные
вопросы. Объект в рекламе месяцами стоит на одной стадии, пока брокер по
нему звонит; рядом стоит такой же, по которому не звонил никто. По стадии
они неотличимы.

Именно из-за этой неразличимости стадия «Поиск клиента» была исключена из
подсчёта зависших (metrics._stuck_exempt): норма стадии там — отбор
выживших, и всё честно рекламируемое оказывалось «зависшим». Исключение
опиралось на обещание, что работу на такой стадии покажут действия. Этот
модуль его выполняет.

Что считается разговором. Пока только CALL и MEETING. TODO и TASKS_TASK —
это дело, заведённое себе, и задача, поставленная сотруднику; на портале их
5 902 за год, почти четверть всех действий. Засчитывать их все работой
значит выдать лучшим работником того, кто аккуратно ведёт список дел и не
звонит.

НО: агентство отмечает встречу именно делом — заводит TODO, а после
проведения ставит ему «выполнено» (ответ собственника 08.09). Значит по
карточке, где брокер съездил на встречу, этот счёт покажет молчание, и
число молчащих здесь — ВЕРХНЯЯ ГРАНИЦА, а не факт. Насколько она завышена,
меряет scripts/meeting_probe.py; до его прогона число нельзя ставить в
сводку как окончательное и нельзя предъявлять человеку.

Направление считается, но не фильтрует. Входящий звонок — это клиент
позвонил сам, а не брокер сработал, и «из них исходящих» стоит отдельной
колонкой. Но карточка, по которой был хоть какой-то разговор, работается —
а из 818 открытых карточек собственников 512 не имеют ни одного звонка ни в
ту, ни в другую сторону, и на этом фоне спор про направление второй.

Общий контакт. Один человек бывает и собственником, и покупателем; его
звонок по покупке попадёт в счёт карточки объекта. Таких карточек три из
818, и ошибка у них в безопасную сторону: карточка выглядит более
отработанной, чем она есть. Обвинить брокера напрасно этот перекос не может
— только не заметить молчание, и число таких карточек печатается рядом.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import plans
from metrics import _rows, _share

# Разговор с человеком. Всё остальное в таблице действий —
# планирование: дело себе и задача сотруднику.
TALK: tuple[str, ...] = ("CALL", "MEETING")

# Сколько дней без разговора делают карточку молчащей. Две недели, а не
# неделя как у движения: объект в рекламе живёт медленнее сделки, и
# недельный порог назвал бы молчащей половину нормально ведомых карточек.
SILENT_DAYS = 14

# Сколько строк показывать поимённо. Список — приглашение открыть карточку,
# а не отчёт: три строки читают, двадцать пролистывают.
TOP = 5

_TALK_SQL = ", ".join(f"'{kind}'" for kind in TALK)


def card_work(
    conn,
    categories: Sequence[int] | None = None,
    *,
    silent_days: int = SILENT_DAYS,
    department_id: int | None = None,
) -> dict[str, Any]:
    """Кто разговаривал по своим открытым карточкам, а кто нет.

    Считается по ОТКРЫТЫМ карточкам: закрытая сделка молчит по праву, и
    сложить её с забытой значит утопить вторую в первых.

    Действие ищется и на сделке, и на контакте сделки. Взяв только сделку,
    отчёт назвал бы молчащими тех, кто звонил: на портале действий на
    контактах 13 277 против 7 131 на сделках. У карточек собственников
    перевес обратный, но 458 разговоров через контакт — это 458 карточек,
    которые иначе выглядели бы заброшенными.
    """
    where, params = plans.category_filter("d", categories)
    params["dept"] = department_id
    rows = _rows(
        conn,
        f"""
        SELECT d.deal_id, d.title, d.stage_id, d.assigned_by_id,
               COALESCE(s.name, d.stage_id) AS stage_name,
               COALESCE(s.sort, 0) AS stage_sort,
               COALESCE(u.name, '') AS broker,
               COALESCE(u.department_name, '') AS department,
               COUNT(a.activity_id) AS talks,
               SUM(CASE WHEN a.direction = 2 THEN 1 ELSE 0 END) AS outgoing,
               SUM(CASE WHEN a.provider_type_id = 'MEETING'
                        THEN 1 ELSE 0 END) AS meetings,
               (julianday('now') - julianday(MAX(a.created_at))) AS quiet_days,
               (julianday('now') - julianday(d.date_create)) AS age_days
        FROM v_deal d
        LEFT JOIN v_user u ON u.user_id = d.assigned_by_id
        LEFT JOIN dim_stage s
               ON s.stage_id = d.stage_id AND s.category_id = d.category_id
        LEFT JOIN v_activity a
               ON ((a.owner_type_id = 2 AND a.owner_id = d.deal_id)
                   OR (a.owner_type_id = 3 AND d.contact_id IS NOT NULL
                       AND d.contact_id > 0 AND a.owner_id = d.contact_id))
              AND a.provider_type_id IN ({_TALK_SQL})
        WHERE d.is_closed = 0 AND {where}
          AND (:dept IS NULL OR u.department_id = :dept)
        GROUP BY d.deal_id
        """,
        params,
    )
    for row in rows:
        row["talks"] = row["talks"] or 0
        row["outgoing"] = row["outgoing"] or 0
        row["meetings"] = row["meetings"] or 0
        row["untouched"] = row["talks"] == 0
        quiet = row["quiet_days"]
        row["quiet_days"] = round(quiet, 1) if quiet is not None else None
        row["silent"] = bool(row["talks"] and quiet is not None
                             and quiet >= silent_days)
        # Дни с заведения — целые: десятая доля дня у карточки,
        # лежащей полгода, это шум с видом точности.
        row["age_days"] = round(row["age_days"] or 0)

    return {
        "silent_days": silent_days,
        "cards": len(rows),
        **_totals(rows),
        "by_stage": _group(rows, "stage_id", "stage_name",
                           sort=lambda item: item["stage_sort"]),
        "by_user": _group(rows, "assigned_by_id", "broker", extra="department"),
        # Самые старые из ни разу не тронутых: карточка, лежащая полгода без
        # единого звонка, — это не «ещё не дошли руки».
        "worst": sorted(
            (row for row in rows if row["untouched"]),
            key=lambda row: -row["age_days"],
        )[:TOP],
        "shared_contacts": _shared_contacts(conn, categories),
    }


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    untouched = sum(1 for row in rows if row["untouched"])
    silent = sum(1 for row in rows if row["silent"])
    return {
        "untouched": untouched,
        "untouched_share": _share(untouched, len(rows)),
        "silent": silent,
        "silent_share": _share(silent, len(rows)),
        # Ни разу не звонили плюс звонили и бросили. Именно это число
        # отвечает на вопрос «сколько карточек лежит без работы».
        "cold": untouched + silent,
        "cold_share": _share(untouched + silent, len(rows)),
        "talks": sum(row["talks"] for row in rows),
        "outgoing": sum(row["outgoing"] for row in rows),
        "meetings": sum(row["meetings"] for row in rows),
    }


def _group(
    rows: list[dict[str, Any]],
    key: str,
    label: str,
    *,
    extra: str | None = None,
    sort=None,
) -> list[dict[str, Any]]:
    """Свод по стадии или по человеку — теми же правилами, что и итог."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row[key], []).append(row)
    result = []
    for value, items in groups.items():
        entry = {
            "key": value,
            "name": items[0][label] or "—",
            "cards": len(items),
            **_totals(items),
        }
        if extra:
            entry[extra] = items[0][extra]
        if sort is not None:
            entry["sort"] = sort(items[0])
        result.append(entry)
    if sort is not None:
        return sorted(result, key=lambda item: item["sort"])
    # Худшие сверху: разговор начинают с того, у кого лежит больше всего.
    return sorted(result, key=lambda item: (-item["cold"], -item["cards"]))


def _shared_contacts(conn, categories: Sequence[int] | None) -> int:
    """Карточки, чей контакт есть в другой воронке.

    Их звонки по другой сделке попадут в счёт этой. Перекос в безопасную
    сторону — карточка выглядит отработаннее, — но число обязано быть на
    виду: молчание оно спрятать может.
    """
    where, params = plans.category_filter("d", categories)
    # Тот же список воронок, те же параметры — второй набор имён завёл бы
    # два места, где правится одно правило.
    other, _ = plans.category_filter("o", categories)
    row = _rows(
        conn,
        f"""
        SELECT COUNT(DISTINCT d.deal_id) AS n
        FROM v_deal d
        JOIN v_deal o ON o.contact_id = d.contact_id AND o.deal_id <> d.deal_id
                     AND NOT ({other})
        WHERE d.is_closed = 0 AND {where}
          AND d.contact_id IS NOT NULL AND d.contact_id > 0
        """,
        params,
    )
    return row[0]["n"] if row else 0
