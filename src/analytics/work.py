"""Работа по карточкам: разговаривали, отметились или не трогали.

Движение по стадиям отвечает на вопрос «карточка двигалась», не «по
карточке работали». Объект в рекламе месяцами стоит на одной стадии и у
того, кто по нему звонит, и у того, кто забыл его в день заведения. Из-за
этой неразличимости «Поиск клиента» исключён из подсчёта зависших
(metrics._stuck_exempt); исключение опиралось на обещание, что работу
покажут действия, и этот модуль его выполняет.

СОСТОЯНИЙ ТРИ, А НЕ ДВА. Это главное решение здесь, и оно продиктовано
устройством портала, а не вкусом.

Агентство отмечает работу делами: «Связаться с клиентом» — 3 085 дел за
год, «Встреча с клиентом» — 401. Встречу заводят делом и после проведения
ставят «выполнено»; отдельных активностей «встреча» всего 68. Считать
только звонки и встречи значит назвать молчащими 511 карточек из 819.
Засчитать все выполненные дела — 315. Разница в 196 карточек, и ни одно из
двух чисел не верно: в первом теряются встречи, во втором «Отчет» и
«Актуальный» становятся работой с клиентом.

Поэтому карточка бывает в одном из трёх состояний:

* **разговор был** — есть завершённый звонок или встреча;
* **только отметка** — есть выполненное дело, но записи разговора нет.
  Брокер утверждает, что работал; портал этого не видел. Это не обвинение:
  человек мог звонить с личного телефона. Но и не работа — это вопрос,
  который надо задать;
* **ничего** — ни разговора, ни отметки.

Сложить второе с первым значит поверить отметке на слово. Сложить со
третьим — обвинить того, кто работал мимо портала. Оба слипания дают число,
которое выглядит правдоподобно и неверно, поэтому состояния держатся
раздельно и в сводке называются раздельно.

Регистр кириллицы разбирается в Python. SQLite lower() латиницу опускает, а
кириллицу нет: «Встреча» и «встреча» для него разные строки, и отбор по
теме, сделанный в SQL, тихо терял бы половину дел.

ВСТРЕЧА БЫВАЕТ НЕ ТОЛЬКО ПРОВЕДЁННОЙ. Из 68 активностей «встреча» в портале
завершены 26; остальные 42 — назначенные, и по ним неизвестно даже,
состоялись ли они. Засчитать такую разговором значит записать в актив то,
чего ещё не было.

Состояний четыре, и ценное среди них одно: срок прошёл, а «выполнено» не
поставлено. Либо встреча не состоялась, либо о ней не отчитались, и оба
случая — работа руководителя. Проведённые и ещё не наступившие вопросов не
вызывают, а встречи без даты считаются отдельно: по ним просрочку не
отличить вовсе, и если их много, признак не годится и дату придётся брать
из поля карточки, а не из дела.

Пропущенные звонки. Незавершённый входящий — это непринятый вызов
(подтверждено собственником 08.09), и у всех 5 169 таких записей время
окончания равно времени начала. Но по открытым карточкам их всего 63:
подавляющее большинство пропущенных не привязано ни к одной открытой
сделке. Потери сидят не в пайплайне, а на входе — поэтому «кто не берёт
трубку» считается по всем звонкам человека, а не по карточкам.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import plans
from metrics import _rows, _share

# Запись разговора. MEETING в портале почти не используют — встречу заводят
# делом, — но там, где он есть, это встреча.
CALL = "CALL"
MEETING = "MEETING"

# Дело: отметка о работе. Задача сотруднику (TASKS_TASK) сюда не входит —
# её ставят внутри агентства, к клиенту она отношения не имеет.
MARK = "TODO"

# Слова, по которым выполненное дело считается встречей. Проверены на живых
# темах: «Встреча с клиентом» 401, «Показ» 36, «просмотр» 18. Слово «объект»
# в список не входит — оно попадается в «Договориться на рекламу объекта»,
# где никакой встречи нет.
MEETING_WORDS = ("встреч", "показ", "просмотр")

# Сколько дней без разговора делают карточку молчащей. Две недели, а не
# неделя как у движения: объект в рекламе живёт медленнее сделки, и
# недельный порог назвал бы молчащей половину нормально ведомых карточек.
SILENT_DAYS = 14

# Сколько строк показывать поимённо: три читают, двадцать пролистывают.
TOP = 5

# Минимум входящих, чтобы попасть в таблицу «не берут трубку». У человека с
# пятью входящими и двумя пропущенными выходит 40%, и он возглавил бы
# таблицу, ничего при этом не значив.
MIN_INCOMING = 30


def _looks_like_meeting(subject: str) -> bool:
    text = (subject or "").lower()
    return any(word in text for word in MEETING_WORDS)


def card_work(
    conn,
    categories: Sequence[int] | None = None,
    *,
    silent_days: int = SILENT_DAYS,
    department_id: int | None = None,
) -> dict[str, Any]:
    """Что происходило по открытым карточкам воронки.

    Считается по ОТКРЫТЫМ: закрытая сделка молчит по праву, и сложить её с
    забытой значит утопить вторую в первых.

    Действие ищется и на сделке, и на её контакте. Звонок чаще висит на
    контакте (13 277 против 7 131 по порталу), и счёт только по сделке
    назвал бы молчащими тех, кто звонил.
    """
    cards = {row["deal_id"]: _blank(row) for row in _cards(conn, categories, department_id)}
    for act in _acts(conn, categories, department_id):
        card = cards.get(act["deal_id"])
        if card is not None:
            _apply(card, act)
    rows = list(cards.values())
    for row in rows:
        _settle(row, silent_days)

    return {
        "silent_days": silent_days,
        "cards": len(rows),
        **_totals(rows),
        "by_stage": _group(rows, "stage_id", "stage_name",
                           sort=lambda item: item["stage_sort"]),
        "by_user": _group(rows, "assigned_by_id", "broker", extra="department"),
        # Самые старые из тех, где не было вообще ничего: карточка, лежащая
        # полгода без звонка и без отметки, — это не «не дошли руки».
        "worst": sorted(
            (row for row in rows if row["state"] == "nothing"),
            key=lambda row: -row["age_days"],
        )[:TOP],
        "pickup": _pickup(conn, department_id),
        "shared_contacts": _shared_contacts(conn, categories),
    }


def _cards(conn, categories, department_id) -> list[dict[str, Any]]:
    where, params = plans.category_filter("d", categories)
    params["dept"] = department_id
    return _rows(
        conn,
        f"""
        SELECT d.deal_id, d.title, d.stage_id, d.assigned_by_id,
               COALESCE(s.name, d.stage_id) AS stage_name,
               COALESCE(s.sort, 0) AS stage_sort,
               COALESCE(u.name, '') AS broker,
               COALESCE(u.department_name, '') AS department,
               (julianday('now') - julianday(d.date_create)) AS age_days
        FROM v_deal d
        LEFT JOIN v_user u ON u.user_id = d.assigned_by_id
        LEFT JOIN dim_stage s
               ON s.stage_id = d.stage_id AND s.category_id = d.category_id
        WHERE d.is_closed = 0 AND {where}
          AND (:dept IS NULL OR u.department_id = :dept)
        """,
        params,
    )


def _acts(conn, categories, department_id) -> list[dict[str, Any]]:
    """Действия по открытым карточкам — строками, а не итогом.

    Итог здесь посчитать нельзя: встреча узнаётся по ТЕМЕ дела, а тему
    приходится опускать в нижний регистр в Python — SQLite делает это только
    с латиницей, и «Встреча» с «встреча» для него разные строки. Отбор,
    сделанный в SQL, тихо терял бы половину дел.
    """
    where, params = plans.category_filter("d", categories)
    params["dept"] = department_id
    return _rows(
        conn,
        f"""
        SELECT d.deal_id, a.provider_type_id, a.direction, a.completed,
               a.subject, a.created_at, a.start_time
        FROM v_deal d
        LEFT JOIN v_user u ON u.user_id = d.assigned_by_id
        JOIN v_activity a
             ON ((a.owner_type_id = 2 AND a.owner_id = d.deal_id)
                 OR (a.owner_type_id = 3 AND d.contact_id IS NOT NULL
                     AND d.contact_id > 0 AND a.owner_id = d.contact_id))
        WHERE d.is_closed = 0 AND {where}
          AND (:dept IS NULL OR u.department_id = :dept)
          AND a.provider_type_id IN (:call, :meet, :mark)
        """,
        {**params, "call": CALL, "meet": MEETING, "mark": MARK},
    )


def _blank(row: dict[str, Any]) -> dict[str, Any]:
    row.update({"calls": 0, "outgoing": 0, "missed": 0, "marks": 0,
                "meetings": 0, "meetings_overdue": 0, "meetings_planned": 0,
                "meetings_undated": 0, "last_talk": None})
    row["age_days"] = round(row["age_days"] or 0)
    return row


def _apply(card: dict[str, Any], act: dict[str, Any]) -> None:
    """Разложить одно действие по счётчикам карточки.

    Состоявшимся считается только завершённое. Назначенная встреча —
    это намерение, и засчитать её разговором значит записать в актив то,
    чего ещё не было: из 68 активностей «встреча» в портале 42 не
    завершены, и по ним неизвестно даже, состоялись ли они.
    """
    kind, done = act["provider_type_id"], bool(act["completed"])
    if kind == CALL:
        if not done and act["direction"] == 1:
            # Непринятый вызов. Разговором он не был — считать его работой
            # значит записать в актив то, что клиент не дозвонился.
            card["missed"] += 1
            return
        card["calls"] += 1
        if act["direction"] == 2:
            card["outgoing"] += 1
        _touch(card, act)
        return

    if kind == MEETING or (kind == MARK and _looks_like_meeting(act["subject"])):
        _meeting(card, act, done)
        return
    if kind == MARK and done:
        # Выполненное дело, не признанное встречей: «Связаться с клиентом»,
        # «Отчет». Отметка о работе, но не запись разговора.
        card["marks"] += 1


def _meeting(card: dict[str, Any], act: dict[str, Any], done: bool) -> None:
    """Встреча в одном из четырёх состояний.

    Ценное среди них одно: срок прошёл, а «выполнено» не поставлено. Либо
    встреча не состоялась, либо о ней не отчитались, и оба случая — работа
    руководителя. Проведённые и ещё не наступившие вопросов не вызывают.

    Без даты начала просрочку не отличить вовсе. Такие считаются отдельно и
    печатаются рядом: если их много, признак не годится и дату придётся
    брать из поля карточки, а не из дела.
    """
    if done:
        card["meetings"] += 1
        _touch(card, act)
    elif not act["start_time"]:
        card["meetings_undated"] += 1
    elif _is_past(act["start_time"]):
        card["meetings_overdue"] += 1
    else:
        card["meetings_planned"] += 1


def _touch(card: dict[str, Any], act: dict[str, Any]) -> None:
    if act["created_at"] and (card["last_talk"] is None
                              or act["created_at"] > card["last_talk"]):
        card["last_talk"] = act["created_at"]


def _settle(row: dict[str, Any], silent_days: int) -> None:
    row["talks"] = row["calls"] + row["meetings"]
    if row["talks"]:
        row["state"] = "talked"
    elif row["marks"]:
        row["state"] = "marked"
    else:
        row["state"] = "nothing"
    row["quiet_days"] = _days_since(row["last_talk"])
    row["silent"] = bool(row["state"] == "talked" and row["quiet_days"] is not None
                         and row["quiet_days"] >= silent_days)


def _moment(stamp: str | None) -> datetime | None:
    """Разбор отметки времени. Сравнение строк тут не годится: смещение у
    записей бывает разным, и «+03:00» сравнивается с «+00:00» посимвольно."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _days_since(stamp: str | None) -> float | None:
    moment = _moment(stamp)
    if moment is None:
        return None
    return round((datetime.now(timezone.utc) - moment).total_seconds() / 86400.0, 1)


def _is_past(stamp: str | None) -> bool:
    moment = _moment(stamp)
    return moment is not None and moment < datetime.now(timezone.utc)


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    talked = sum(1 for row in rows if row["state"] == "talked")
    marked = sum(1 for row in rows if row["state"] == "marked")
    nothing = sum(1 for row in rows if row["state"] == "nothing")
    silent = sum(1 for row in rows if row["silent"])
    missed = sum(1 for row in rows if row["missed"])
    return {
        "talked": talked,
        "marked": marked,
        "marked_share": _share(marked, len(rows)),
        "nothing": nothing,
        "nothing_share": _share(nothing, len(rows)),
        "silent": silent,
        # Ни отметки, ни разговора плюс разговор, брошенный давно. Отметка
        # без разговора сюда не входит: это отдельный вопрос, а не приговор.
        "cold": nothing + silent,
        "cold_share": _share(nothing + silent, len(rows)),
        "talks": sum(row["talks"] for row in rows),
        "outgoing": sum(row["outgoing"] for row in rows),
        "meetings": sum(row["meetings"] for row in rows),
        # Просроченная встреча — единственное состояние, требующее
        # разбора: срок прошёл, дело не закрыто.
        "meetings_overdue": sum(row["meetings_overdue"] for row in rows),
        "meetings_planned": sum(row["meetings_planned"] for row in rows),
        "meetings_undated": sum(row["meetings_undated"] for row in rows),
        "marks": sum(row["marks"] for row in rows),
        "missed": sum(row["missed"] for row in rows),
        "missed_cards": missed,
        # Пропущенный, за которым перезвонили, — это работа. Не перезвонили
        # ни разу — это потерянный человек, и число у них разное.
        "never_returned": sum(
            1 for row in rows if row["missed"] and not row["outgoing"]
        ),
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
        entry = {"key": value, "name": items[0][label] or "—",
                 "cards": len(items), **_totals(items)}
        if extra:
            entry[extra] = items[0][extra]
        if sort is not None:
            entry["sort"] = sort(items[0])
        result.append(entry)
    if sort is not None:
        return sorted(result, key=lambda item: item["sort"])
    # Худшие сверху: разговор начинают с того, у кого лежит больше всего.
    return sorted(result, key=lambda item: (-item["cold"], -item["cards"]))


def _sales_departments() -> tuple[int, ...]:
    """Отделы, которые продают. Берутся у plans, а не заводятся здесь.

    Тот же список читают план и отчёт по собственникам. Второй ответ на
    вопрос «кто брокер» означал бы, что два отчёта одного агентства в один
    день называют разных людей.

    Пустой кортеж означает «не знаем», а не «никто»: настройка могла не
    прочитаться, и тогда отбор не сужается вовсе. Иначе сбой конфига молча
    убрал бы из сводки всех до единого, и выглядело бы это как «сегодня
    трубку берут все».
    """
    try:
        import plans

        return plans.sales_department_ids()
    except Exception:  # pragma: no cover — конфиг недоступен в изолированных тестах
        return ()


def _service_names() -> set[str]:
    """Учётные записи, которые не человек. Список ведёт агентство.

    Не выдумывается здесь: тот же перечень уже используется замком источника
    у контактов — это готовый ответ агентства на вопрос «кто из этих имён не
    сотрудник». Второй список разошёлся бы с первым.
    """
    try:
        from config import get_settings

        return {name.strip().lower()
                for name in get_settings().contact_source_lock_exclude_names}
    except Exception:  # pragma: no cover — конфиг недоступен в изолированных тестах
        return {"агентство недвижимости", "asterisk1 1"}


def _pickup(conn, department_id: int | None) -> list[dict[str, Any]]:
    """Кто не берёт трубку. Считается по всем звонкам, а не по карточкам.

    По открытым карточкам пропущенных всего 63 при 5 169 по порталу:
    подавляющее большинство непринятых не привязано ни к одной открытой
    сделке. Считать их через карточки значит увидеть один процент проблемы —
    потери сидят на входе, до того как заводится сделка.

    Верх этой таблицы на живых данных занимают не брокеры: общая линия
    агентства (929 непринятых из 1286), уволенный сотрудник, на которого всё
    ещё звонят, и бэк-офис (119 из 149). Все три строки — настоящие потери и
    все три остаются на экране, но помечены.

    Спрос с них разный, и в этом всё дело. Общая линия — вопрос
    маршрутизации, а не дисциплины. На уволенного звонить не должны вовсе.
    Бэк-офису входящие сваливает маршрутизация, а не клиент, выбравший
    своего брокера, и мера брокера к нему не применима. Поставить их в
    утреннее сообщение рядом с брокером значит начать разговор не с тем
    человеком — а сводка нужна ровно для того, чтобы начать его с тем.

    Поэтому ``person`` отделяет тех, с кем об этом сегодня говорят, от
    остальных, а решает, кого печатать, уже сводка.
    """
    service = _service_names()
    sales = _sales_departments()
    rows = _rows(
        conn,
        """
        SELECT a.responsible_id AS user_id,
               COALESCE(u.name, '') AS name,
               COALESCE(u.department_name, '') AS department,
               u.department_id AS department_id,
               COALESCE(u.is_active, 0) AS is_active,
               SUM(CASE WHEN a.direction = 1 THEN 1 ELSE 0 END) AS incoming,
               SUM(CASE WHEN a.direction = 1 AND a.completed = 0
                        THEN 1 ELSE 0 END) AS missed
        FROM v_activity a
        LEFT JOIN v_user u ON u.user_id = a.responsible_id
        WHERE a.provider_type_id = :call
          AND (:dept IS NULL OR u.department_id = :dept)
        GROUP BY a.responsible_id
        """,
        {"call": CALL, "dept": department_id},
    )
    people = []
    for row in rows:
        if (row["incoming"] or 0) < MIN_INCOMING:
            continue
        row["missed_share"] = _share(row["missed"], row["incoming"])
        row["service"] = (row["name"] or "").strip().lower() in service
        # Бэк-офис трубку берёт по другим правилам: входящие туда сваливает
        # маршрутизация, а не клиент, выбравший своего брокера. Судить его
        # мерой брокера значит спорить не с тем человеком.
        row["sells"] = not sales or row["department_id"] in sales
        # Тот, с кем можно поговорить об этом сегодня. Уволенный, робот и
        # непродающий в ежедневное сообщение не идут: там нужно действие, а
        # не история и не чужая зона ответственности.
        row["person"] = (bool(row["is_active"]) and not row["service"]
                         and row["sells"])
        people.append(row)
    return sorted(people, key=lambda row: -row["missed_share"])


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
