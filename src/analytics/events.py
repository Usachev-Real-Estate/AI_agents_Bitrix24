"""Что случилось с воронкой за окно: события, а не уровни.

«Пульс» отвечает на вопрос «где мы стоим». Этот модуль — на вопрос «что
изменилось со вчера», и разница принципиальная.

Агентство закрывает 19 сделок за квартал в воронке плана — это полторы
сделки в неделю. На таких числах любая недельная конверсия шум: одна сделка
меняет её вдвое, и отчёт будет каждый день кричать о просадке, которой нет.
Поэтому здесь считаются СОБЫТИЯ — сделка встала, сделка сдвинулась, сделка
вернулась назад, человек не двигал ничего неделю. Событие на маленьких
числах честно, процент — нет.

Второе правило: пустой блок не печатается. Отчёт, который каждый день
сообщает «ничего не произошло», перестают открывать раньше, чем в нём
появится что-то важное.

Пороги простоя берутся у metrics._stuck_rows — там же, где их берёт экран.
Второй ответ на вопрос «какая сделка зависла» означал бы, что дайджест и
дашборд однажды назовут разные карточки.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import metrics
import plans
from metrics import _rows, base_currency

# Сколько дней без движения делают человека «молчащим». Один день молчания —
# это нормальная работа, поэтому блок ежедневный, а горизонт у него недельный.
SILENCE_DAYS = 7

# Сколько карточек показывать поимённо. Список в сообщении — не отчёт, а
# приглашение открыть дашборд: три строки читают, двадцать пролистывают.
TOP = 3


def funnel_events(
    conn,
    since: str,
    until: str,
    categories: Sequence[int] | None = None,
    on_plan_ids: set[int] | None = None,
    silence_days: int = SILENCE_DAYS,
) -> dict[str, Any]:
    """События воронки за окно [since, until)."""
    cats = tuple(categories) if categories is not None else plans.plan_category_ids()
    window_days = max(
        (plans._day_of(until) - plans._day_of(since)).days, 1,
    )
    moves = _moves(conn, since, until, cats)
    return {
        "window": {"since": since, "until": until, "days": window_days},
        "stalled": _stalled(conn, cats, window_days),
        "advanced": _pack([m for m in moves if m["to_sort"] > m["from_sort"]]),
        "returned": _pack([m for m in moves if m["to_sort"] < m["from_sort"]]),
        "left_work": _left_work(conn, since, until, cats),
        "silent": _silent(conn, cats, on_plan_ids, silence_days),
        "quality": _quality(conn, since, until, cats),
        "currency": base_currency(),
    }


def _stalled(conn, categories: Sequence[int], window_days: int) -> dict[str, Any]:
    """Сделки, перешагнувшие порог простоя внутри окна.

    Не «стоят сейчас» — тех 224, и список этот не меняется неделями. Здесь
    те, что остановились ТОЛЬКО ЧТО: их ещё помнят, и с ними ещё можно
    что-то сделать.

    Порог перехода вычисляется из тех же чисел, что и сам простой: сколько
    дней карточка стоит и какова норма её стадии. Разница между ними меньше
    длины окна — значит порог пройден внутри окна.
    """
    fresh = []
    for category_id in categories:
        for row in metrics._stuck_rows(conn, category_id):
            over = row["days_in_stage"] - row["threshold_days"]
            if 0 < over <= window_days:
                fresh.append({**row, "days_over": round(over, 1)})
    fresh.sort(key=lambda row: -(row.get("opportunity") or 0))
    return _pack(fresh)


def _moves(
    conn, since: str, until: str, categories: Sequence[int],
) -> list[dict[str, Any]]:
    """Переходы между стадиями внутри окна, поимённо.

    metrics.stage_transitions() отвечает на тот же вопрос счётчиками для
    тепловой карты; здесь нужны сами карточки, поэтому запрос свой, а
    правило соседства стадий — то же: e2.seq = e1.seq + 1.

    Закрытые сделки исключены. Выигранная сделка уже названа в строке про
    деньги, и повторять её в «сдвинулись вперёд» значит показать одни и те
    же деньги дважды.

    Проигрышные стадии исключены отдельно, а не через is_closed. У продавцов
    «Отложенная продажа» стоит по порядку выше «Переговоров», и по одному
    только sort уход в неё читался бы как движение вперёд — то есть потеря
    собственника попадала бы в блок хороших новостей.
    """
    where, params = plans.category_filter("d", categories)
    return _rows(
        conn,
        f"""
        SELECT d.deal_id, d.title, d.opportunity, d.currency_id,
               COALESCE(sf.name, e1.stage_id) AS from_name,
               COALESCE(st.name, e2.stage_id) AS to_name,
               COALESCE(sf.sort, 0) AS from_sort,
               COALESCE(st.sort, 0) AS to_sort,
               COALESCE(u.name, '') AS assignee
        FROM v_stage_event e1
        JOIN v_stage_event e2
          ON e2.entity_type = e1.entity_type AND e2.entity_id = e1.entity_id
         AND e2.seq = e1.seq + 1
        JOIN v_deal d ON d.deal_id = e1.entity_id
        LEFT JOIN dim_stage sf
          ON sf.stage_id = e1.stage_id AND sf.category_id = d.category_id
        LEFT JOIN dim_stage st
          ON st.stage_id = e2.stage_id AND st.category_id = d.category_id
        LEFT JOIN v_user_all u ON u.user_id = d.assigned_by_id
        WHERE e1.entity_type = 'deal' AND d.is_closed = 0 AND {where}
          AND COALESCE(st.semantic, '') <> 'lost'
          AND e2.entered_at >= :since AND e2.entered_at < :until
        ORDER BY d.opportunity DESC
        """,
        {"since": since, "until": until, **params},
    )


def _left_work(
    conn, since: str, until: str, categories: Sequence[int],
) -> dict[str, Any]:
    """Карточки, ушедшие из работы в окне: проиграны или отложены.

    Главный вопрос собственника про воронку продавцов — где брокеры не
    дорабатывают. Отвечает на него не число потерь, а стадия, С КОТОРОЙ
    ушли: собственник, потерянный на переговорах, и собственник, до которого
    не доехали на встречу, — это две разные недоработки.

    Отложенная продажа считается потерей наравне с проигрышем: в портале у
    неё семантика lost, и витрина не выдумывает третьего состояния там, где
    агентство завело два.

    Семантика берётся из справочника стадий, а не из порядка: у продавцов
    «Отложенная продажа» стоит выше «Переговоров», и по sort уход в неё
    выглядел бы продвижением вперёд.
    """
    where, params = plans.category_filter("d", categories)
    rows = _rows(
        conn,
        f"""
        SELECT d.deal_id, d.title, d.opportunity, d.currency_id,
               COALESCE(sf.name, e1.stage_id) AS from_name,
               st.name AS to_name,
               COALESCE(u.name, '') AS assignee
        FROM v_stage_event e1
        JOIN v_stage_event e2
          ON e2.entity_type = e1.entity_type AND e2.entity_id = e1.entity_id
         AND e2.seq = e1.seq + 1
        JOIN v_deal d ON d.deal_id = e1.entity_id
        JOIN dim_stage st
          ON st.stage_id = e2.stage_id AND st.category_id = d.category_id
         AND st.semantic = 'lost'
        LEFT JOIN dim_stage sf
          ON sf.stage_id = e1.stage_id AND sf.category_id = d.category_id
        LEFT JOIN v_user_all u ON u.user_id = d.assigned_by_id
        WHERE e1.entity_type = 'deal' AND {where}
          AND e2.entered_at >= :since AND e2.entered_at < :until
        ORDER BY d.opportunity DESC
        """,
        {"since": since, "until": until, **params},
    )
    by_stage: dict[str, int] = {}
    for row in rows:
        by_stage[row["from_name"]] = by_stage.get(row["from_name"], 0) + 1
    return {
        **_pack(rows),
        # Откуда ушли — важнее, чем сколько. Это и есть ответ на вопрос
        # «на каком этапе не дорабатывают».
        "by_stage": sorted(by_stage.items(), key=lambda item: -item[1]),
    }


def _silent(
    conn,
    categories: Sequence[int],
    on_plan_ids: set[int] | None,
    silence_days: int,
) -> dict[str, Any]:
    """Кто не двигал ни одной своей карточки дольше порога.

    Считается по открытым сделкам: у человека без единой сделки в работе
    молчание означает не простой, а отсутствие работы вовсе — это отдельная
    строка, и путать их нельзя.

    Дни календарные, а не рабочие: «не двигал восемь дней» — это про
    карточку, а не про табель, и выходные для клиента не оправдание.
    """
    where, params = plans.category_filter("d", categories)
    rows = _rows(
        conn,
        f"""
        SELECT d.assigned_by_id AS user_id,
               COALESCE(u.name, '') AS name,
               COALESCE(u.department_name, '') AS department,
               COUNT(DISTINCT d.deal_id) AS deals,
               MIN(julianday('now') - julianday(last.entered_at)) AS quiet_days
        FROM v_deal d
        JOIN v_user u ON u.user_id = d.assigned_by_id
        LEFT JOIN v_stage_event last
          ON last.entity_type = 'deal' AND last.entity_id = d.deal_id
         AND last.seq = (SELECT MAX(x.seq) FROM v_stage_event x
                         WHERE x.entity_type = 'deal' AND x.entity_id = d.deal_id)
        WHERE d.is_closed = 0 AND {where}
        GROUP BY d.assigned_by_id, u.name, u.department_name
        """,
        params,
    )
    people = []
    for row in rows:
        if on_plan_ids is not None and row["user_id"] not in on_plan_ids:
            continue
        quiet = row["quiet_days"]
        if quiet is None or quiet < silence_days:
            continue
        people.append({
            "user_id": row["user_id"],
            "name": row["name"] or f"ID {row['user_id']}",
            "department": row["department"],
            "deals": row["deals"],
            "quiet_days": int(quiet),
        })
    people.sort(key=lambda item: -item["quiet_days"])
    return {"people": people, "idle_days": silence_days}


def _quality(
    conn, since: str, until: str, categories: Sequence[int],
) -> dict[str, Any]:
    """Порча данных, случившаяся внутри окна.

    Не «заполнено 85%» — этот уровень месяцами один и тот же, и в ежедневном
    отчёте он превращается в фон. Здесь только то, что испортилось ВЧЕРА:
    сделка закрыта без суммы, карточка заведена без ответственного. Такое
    чинится за минуту, пока помнят, о чём речь.
    """
    where, params = plans.category_filter("d", categories)
    won = _rows(
        conn,
        f"""
        SELECT d.deal_id, d.title, COALESCE(u.name, '') AS assignee
        FROM v_deal d
        LEFT JOIN v_user_all u ON u.user_id = d.assigned_by_id
        WHERE d.is_won = 1 AND (d.opportunity IS NULL OR d.opportunity <= 0)
          AND d.closedate >= :since AND d.closedate < :until AND {where}
        ORDER BY d.deal_id
        """,
        {"since": since, "until": until, **params},
    )
    orphan = _rows(
        conn,
        f"""
        SELECT d.deal_id, d.title FROM v_deal d
        WHERE (d.assigned_by_id IS NULL OR d.assigned_by_id = 0)
          AND d.date_create >= :since AND d.date_create < :until AND {where}
        ORDER BY d.deal_id
        """,
        {"since": since, "until": until, **params},
    )
    return {"won_without_amount": won, "without_assignee": orphan}


def _pack(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Счётчик, сумма и первые несколько карточек поимённо.

    Сумма — по всем найденным, список — по первым: иначе итог описывал бы
    только показанное, а это ровно та ошибка, из-за которой сумма зависших
    считается отдельной функцией, а не сложением видимых строк.
    """
    base = base_currency()
    amount = 0.0
    for row in rows:
        currency = (row.get("currency_id") or "").strip().upper()
        if currency and currency != base:
            continue
        amount += float(row.get("opportunity") or 0)
    return {
        "deals": len(rows),
        "amount": round(amount, 0),
        "top": rows[:TOP],
    }
