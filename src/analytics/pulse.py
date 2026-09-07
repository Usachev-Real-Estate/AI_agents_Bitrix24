"""Пульс: план, факт и темп периода одним расчётом.

Отдельный модуль, а не функция в metrics: план живёт в plans, а plans уже
читает metrics ради границ суток и перцентилей. Сборка, которой нужны оба,
получила бы круговой импорт, и лечить его пришлось бы отложенными импортами
внутри функций — то есть прятать связь вместо того, чтобы её назвать.

Главное правило этого файла: **сделка засчитывается отделу, в котором она
закрыта.** Решение агентства от 07.09. Новичок без нормы приносит отделу
настоящие деньги, и вычитать их из выполнения значит недосчитывать работу
отдела.

У решения есть цена, и она названа рядом, а не спрятана: ``fact_on_plan``
показывает, сколько из факта сделали те, кто норму несёт. Отдел, закрывший
план чужими руками, отличается от отдела, где сработали плановые люди, — и на
экране эта разница видна, хотя на процент выполнения не влияет.

Один расчёт питает три канала: веб-страницу, утренний дайджест в Битрикс и
будущий MCP-сервер. Поэтому здесь возвращаются данные, а не текст, а область
видимости приходит соединением — форматирование и адресация живут снаружи.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import metrics
import plans
from metrics import _money_of, _one, _rows, _share, base_currency

# Сколько рабочих дней должно пройти, чтобы линейный прогноз что-то
# значил. На третьем дне квартала доля срока — четыре процента, и любое
# деление на неё даёт число, которое скачет втрое от одной сделки.
PROJECTION_MIN_DAYS = 5


def pulse(
    conn,
    period_code: str,
    metric: str = plans.METRIC_COMMISSION,
    today: str | None = None,
) -> dict[str, Any]:
    """План, факт и темп квартала — по компании и по отделам.

    ``today`` задаёт правую границу прошедшего срока; None — сейчас. Параметр
    существует ради тестов и ради дайджеста, который считает вчерашний день
    закрытым: в 9 утра «прошло дней» не должно прыгать вместе с часами.
    """
    period = plans.period(conn, period_code)
    plan = plans.plan(conn, period_code, metric)
    facts = _facts(conn, period["starts_at"], period["ends_at"])

    on_plan_ids = {
        member["user_id"]
        for row in plan["departments"]
        for member in row["members"]
        if member["plan"] is not None
    }

    departments, total_fact, on_plan_fact, others_deals = [], 0.0, 0.0, 0
    for row in plan["departments"]:
        own = [f for f in facts if f["department_id"] == row["department_id"]]
        # Решение агентства от 07.09: сделка засчитывается отделу, в котором
        # закрыта, независимо от того, несёт ли её автор норму. Новичок без
        # плана приносит отделу настоящие деньги, и вычитать их из
        # выполнения значит недосчитывать работу отдела.
        #
        # Цена решения названа рядом, а не спрятана: fact_on_plan показывает,
        # сколько сделали те, кто обещал. Отдел, закрывший план чужими
        # руками, отличается от отдела, где сработали плановые люди, и на
        # экране это видно.
        fact = sum(f["amount"] for f in own)
        planned = sum(f["amount"] for f in own if f["user_id"] in on_plan_ids)
        rest = fact - planned
        total_fact += fact
        on_plan_fact += planned
        others_deals += sum(f["deals"] for f in own if f["user_id"] not in on_plan_ids)
        departments.append({
            "department_id": row["department_id"],
            "name": row["name"],
            "plan": row["plan"],
            "fact": round(fact, 2),
            "fact_on_plan": round(planned, 2),
            "others_fact": round(rest, 2),
            "deals": sum(f["deals"] for f in own),
            "on_plan": row["on_plan"],
            "without_norm": row["without_norm"],
            "rop_known": row["rop_known"],
            **plans.pace(fact, row["plan"], *_elapsed(period, today)),
        })

    elapsed, total_days = _elapsed(period, today)
    # Отделы сортируются по плану, а не по факту: экран отвечает на вопрос
    # «где сосредоточена цель», и отдел с самой большой целью должен быть
    # сверху даже в месяц, когда он не продал ничего.
    departments.sort(key=lambda row: -(row["plan"] or 0))
    return {
        "period": period,
        "metric": metric,
        "plan": plan["plan"],
        "fact": round(total_fact, 2),
        "departments": departments,
        "brokers_on_plan": len(on_plan_ids),
        "without_norm": sum(row["without_norm"] for row in plan["departments"]),
        "departments_without_rop": plan["departments_without_rop"],
        # Сколько из факта сделали люди, несущие норму. Число не меняет
        # выполнения, но отвечает на вопрос, который иначе не задать: отдел
        # выполнил план сам или за счёт тех, кому плана не ставили.
        "fact_on_plan": round(on_plan_fact, 2),
        "others": {
            "fact": round(total_fact - on_plan_fact, 2),
            "deals": others_deals,
            "people": sum(row["without_norm"] for row in plan["departments"]),
        },
        # Простейший прогноз: сколько выйдет, если темп не изменится. Он и
        # подписан именно так. Взвешенный прогноз по стадиям честнее, но
        # требует своей функции и своего покрытия; линейный не притворяется
        # чем-то большим, а до конца квартала отвечает на вопрос «успеваем ли»
        # ровно так же. None до пятого рабочего дня: делить на долю срока
        # размером в три дня значит печатать случайное число крупным шрифтом.
        "projection": (
            round(total_fact * total_days / elapsed, 2)
            if elapsed >= PROJECTION_MIN_DAYS else None
        ),
        "coverage": _coverage(conn, period["starts_at"], period["ends_at"]),
        "currency": base_currency(),
        **plans.pace(total_fact, plan["plan"], elapsed, total_days),
    }


def _elapsed(period: dict[str, Any], today: str | None) -> tuple[int, int]:
    """Рабочих дней прошло и всего. Сегодняшний день считается прошедшим.

    Граница прошедшего — завтрашняя полночь: правая граница у периодов
    исключающая, и без сдвига сегодняшний рабочий день не попал бы в счёт,
    а темп с утра до полуночи занижался бы на один день из шестидесяти.
    """
    total = plans.working_days(period["starts_at"], period["ends_at"])
    now = (
        datetime.fromisoformat(today) if today
        else datetime.now(metrics.BUSINESS_TZ)
    ).astimezone(metrics.BUSINESS_TZ)
    tomorrow = datetime(
        now.year, now.month, now.day, tzinfo=metrics.BUSINESS_TZ,
    ) + timedelta(days=1)
    ends = datetime.fromisoformat(period["ends_at"])
    edge = min(tomorrow.astimezone(timezone.utc), ends)
    gone = plans.working_days(period["starts_at"], edge.isoformat())
    return min(max(gone, 0), total), total


def _facts(conn, since: str, until: str) -> list[dict[str, Any]]:
    """Выигранные деньги периода по людям и отделам.

    Валюта учитывается так же, как в money(): сделки не в базовой валюте в
    сумму не входят — курса у витрины нет, и сложить их с рублями значит
    напечатать неверное число, а не приблизительное.
    """
    return _rows(
        conn,
        f"""
        SELECT d.assigned_by_id AS user_id,
               u.department_id AS department_id,
               COUNT(*) AS deals,
               COALESCE(SUM(CASE WHEN {_money_of('d')} THEN d.opportunity ELSE 0 END), 0)
                   AS amount
        FROM v_deal d
        JOIN v_user u ON u.user_id = d.assigned_by_id
        WHERE d.is_won = 1 AND d.closedate IS NOT NULL
          AND d.closedate >= :since AND d.closedate < :until
        GROUP BY d.assigned_by_id, u.department_id
        """,
        {"since": since, "until": until, "base": base_currency()},
    )


def _coverage(conn, since: str, until: str) -> dict[str, Any]:
    """Покрытие поля суммы у выигранных сделок периода.

    Обязательно рядом с планом: при заполненности в 70 процентов выполнение
    занижено, а не «маленькое», и решение по такому числу принимается другое.
    """
    row = _one(
        conn,
        f"""
        SELECT COUNT(*) AS deals,
               SUM(CASE WHEN opportunity > 0 AND {_money_of()} THEN 1 ELSE 0 END) AS filled,
               SUM(CASE WHEN NOT {_money_of()} THEN 1 ELSE 0 END) AS foreign_deals
        FROM v_deal
        WHERE is_won = 1 AND closedate IS NOT NULL
          AND closedate >= :since AND closedate < :until
        """,
        {"since": since, "until": until, "base": base_currency()},
    )
    deals = int(row.get("deals") or 0)
    foreign = int(row.get("foreign_deals") or 0)
    filled = int(row.get("filled") or 0)
    return {
        "deals": deals,
        "filled": filled,
        "foreign": foreign,
        "share": _share(filled, deals - foreign),
    }
