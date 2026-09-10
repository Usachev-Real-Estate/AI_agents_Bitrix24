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

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import metrics
import plans
import wording
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
    with_stuck: bool = False,
) -> dict[str, Any]:
    """План, факт и темп квартала — по компании и по отделам.

    ``today`` задаёт правую границу прошедшего срока; None — сейчас. Параметр
    существует ради тестов и ради дайджеста, который считает вчерашний день
    закрытым: в 9 утра «прошло дней» не должно прыгать вместе с часами.

    ``with_stuck`` добавляет деньги, стоящие на зависших сделках, — по
    компании и по отделам. Не по умолчанию: расчёт перебирает стадии каждой
    воронки, а дайджест собирает «Пульс» шесть раз подряд, и платить за
    перебор там, где число не печатается, незачем.
    """
    period = plans.period(conn, period_code)
    plan = plans.plan(conn, period_code, metric)
    categories = plans.plan_category_ids()
    facts = _attribute(
        _facts(conn, period["starts_at"], period["ends_at"], categories), plan,
    )
    elapsed, total_days = _elapsed(period, today)

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
            "rops": row["rop_list"],
            "brokers": _brokers(row, own, elapsed, total_days),
            "stuck": (
                _stuck(conn, categories, row["department_id"]) if with_stuck else None
            ),
            **plans.pace(fact, row["plan"], elapsed, total_days),
        })

    # Простейший прогноз: сколько выйдет, если темп не изменится. Он и
    # подписан именно так. Взвешенный прогноз по стадиям честнее, но требует
    # своей функции и своего покрытия; линейный не притворяется чем-то
    # большим, а до конца квартала отвечает на вопрос «успеваем ли» ровно
    # так же. None до пятого рабочего дня: делить на долю срока размером в
    # три дня значит печатать случайное число крупным шрифтом.
    projection = (
        round(total_fact * total_days / elapsed, 2)
        if elapsed >= PROJECTION_MIN_DAYS else None
    )

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
        "projection": projection,
        "coverage": _coverage(
            conn, period["starts_at"], period["ends_at"], categories,
        ),
        # Воронки, по которым собран факт. Экран обязан их назвать: план
        # считается по «Покупателям», а на «Обзоре» рядом лежат числа по
        # всем воронкам сразу, и два разных факта без подписи читаются как
        # ошибка одного из них.
        "funnels": _funnels(conn, categories),
        # Рубеж безубыточности рядом с планом-планкой. Планка отвечает «к
        # чему тянемся» и держится красной весь квартал; рубеж отвечает
        # «доживём ли» и движется от каждой сделки. None — если расходы и
        # доля не заданы: выдуманный порог хуже отсутствующего.
        "breakeven": plans.breakeven(
            total_fact, projection, period["starts_at"], period["ends_at"],
        ),
        # Деньги, переставшие двигаться. Это и есть ответ на вопрос «куда
        # поднажать»: не отдел с худшим процентом, а сделки, стоящие дольше
        # нормы своей стадии, с суммой на них.
        "stuck": _stuck(conn, categories, None) if with_stuck else None,
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


def _attribute(
    facts: list[dict[str, Any]], plan: dict[str, Any],
) -> list[dict[str, Any]]:
    """Факт приписывается тому отделу, в котором человек несёт план.

    Ростер переносит человека между отделами — так в состав плана попадает
    РОП, числящийся в портале в чужом подразделении. Состав это учитывает, а
    запрос факта видит только карточку в портале. Без этого шага норма
    считалась бы одному отделу, а закрытые тем же человеком деньги — другому,
    и оба отдела показали бы правдоподобную неправду: у одного выполнение
    завышено, у другого занижено, и ни в одном из двух чисел ошибка не видна.

    Кого в составе нет — уволенные в середине квартала, люди из отделов вне
    продаж — остаются при отделе из портала. Деньги заработаны, и терять их
    нельзя; если их отдела нет среди плановых, они и так не попадут никуда.
    """
    home = {
        member["user_id"]: row["department_id"]
        for row in plan["departments"]
        for member in row["members"]
    }
    return [
        {**fact, "department_id": home.get(fact["user_id"], fact["department_id"])}
        for fact in facts
    ]


def _facts(
    conn, since: str, until: str, categories: Sequence[int],
) -> list[dict[str, Any]]:
    """Выигранные деньги периода по людям и отделам.

    Только воронки плана: решение агентства от 07.09 — план несут
    «Покупатели». Сделка собственника или новостройки в выполнение не
    входит, потому что там другой чек и другая работа, и сложить их значило
    бы мерить план фактом, собранным по другому правилу.

    Валюта учитывается так же, как в money(): сделки не в базовой валюте в
    сумму не входят — курса у витрины нет, и сложить их с рублями значит
    напечатать неверное число, а не приблизительное.

    Справочник здесь полный (v_user_all), а не состав: уволенный в середине
    квартала оставил отделу настоящие деньги, и без него факт отдела вышел
    бы меньше, чем есть. Фамилию его таблица не покажет — за это отвечает
    _brokers, — но сумму обязана сохранить.
    """
    where, params = plans.category_filter("d", categories)
    return _rows(
        conn,
        f"""
        SELECT d.assigned_by_id AS user_id,
               u.department_id AS department_id,
               u.name AS name,
               u.is_active AS is_active,
               COUNT(*) AS deals,
               COALESCE(SUM(CASE WHEN {_money_of('d')} THEN d.opportunity ELSE 0 END), 0)
                   AS amount
        FROM v_deal d
        JOIN v_user_all u ON u.user_id = d.assigned_by_id
        WHERE d.is_won = 1 AND d.closedate IS NOT NULL
          AND d.closedate >= :since AND d.closedate < :until
          AND {where}
        GROUP BY d.assigned_by_id, u.department_id, u.name, u.is_active
        """,
        {"since": since, "until": until, "base": base_currency(), **params},
    )


def _brokers(
    row: dict[str, Any],
    own: list[dict[str, Any]],
    elapsed: int,
    total_days: int,
) -> list[dict[str, Any]]:
    """Выполнение плана по людям отдела.

    Деньги уволенного остаются в отделе, а его фамилия из таблицы уходит.
    Это не противоречие, а два разных вопроса. «Сколько отдел заработал» —
    вопрос про деньги, и вычесть из них закрытые ушедшим сделки значит
    напечатать неверную сумму: таблица, не сходящаяся со своим же итогом,
    хуже отсутствующей — её один раз проверят и перестанут верить обеим.
    «С кого спросить» — вопрос про людей, и уволенный на него не отвечает.

    Поэтому ушедшие складываются в одну строку «Уволенные»: итог сходится,
    а строки, которую нельзя ни выполнить, ни обсудить, в таблице нет.

    Порядок: сначала те, кто несёт норму, по выполнению сверху вниз; за ними
    остальные по деньгам. Так первым читается тот, о ком и ставился вопрос.
    """
    by_user = {fact["user_id"]: fact for fact in own}
    rows = [
        _broker_row(
            member["user_id"], member["name"], member.get("role"),
            member.get("plan"), by_user.pop(member["user_id"], None),
            elapsed, total_days,
        )
        for member in row["members"]
    ]
    left = [fact for fact in by_user.values() if fact.get("is_active")]
    gone = [fact for fact in by_user.values() if not fact.get("is_active")]
    rows.extend(
        _broker_row(
            fact["user_id"], fact.get("name") or f"ID {fact['user_id']}",
            None, None, fact, elapsed, total_days,
        )
        for fact in left
    )
    if gone:
        summed = _broker_row(
            0, f"Уволенные · {len(gone)} {_people_word(len(gone))}",
            None, None,
            {"deals": sum(int(fact["deals"]) for fact in gone),
             "amount": sum(float(fact["amount"]) for fact in gone)},
            elapsed, total_days,
        )
        # Не «вне состава»: это не человек, которого забыли вписать, а сумма
        # тех, кого уже нет. Метка нужна, чтобы таблица не предлагала
        # спросить с этой строки.
        summed["gone"] = True
        rows.append(summed)
    with_norm = sorted(
        (item for item in rows if item["plan"] is not None),
        key=lambda item: -(item["plan_share"] or 0),
    )
    rest = sorted(
        (item for item in rows if item["plan"] is None),
        key=lambda item: -item["fact"],
    )
    return with_norm + rest


def _people_word(count: int) -> str:
    """«человек / человека / человек» — форма под число."""
    return wording.form(count, "человек", "человека", "человек")


def _broker_row(
    user_id: int,
    name: str,
    role: str | None,
    plan_amount: float | None,
    fact: dict[str, Any] | None,
    elapsed: int,
    total_days: int,
) -> dict[str, Any]:
    amount = round(float(fact["amount"]), 2) if fact else 0.0
    return {
        "user_id": user_id,
        "name": name,
        "role": role,
        # Человек, которого нет в плановом составе: уволен или переведён.
        # Его деньги отделу засчитаны, и строка обязана это объяснить.
        "in_roster": role is not None,
        "deals": int(fact["deals"]) if fact else 0,
        **plans.pace(amount, plan_amount, elapsed, total_days),
    }


def _stuck(
    conn, categories: Sequence[int], department_id: int | None,
) -> dict[str, Any]:
    """Деньги на зависших сделках воронок плана.

    Зависшая — стоящая на стадии дольше 75-го перцентиля этой же стадии.
    Порог берётся из данных самой воронки, а не из выдуманного числа дней: у
    «Подбора» и «Офера» нормальный срок разный.

    Считается по всем найденным, а не по показанным пятидесяти: сумма по
    обрезанному списку выглядела бы правдоподобно и была бы занижена ровно
    настолько, насколько зависших больше полусотни.
    """
    total = {"amount": 0.0, "deals": 0, "filled": 0, "foreign": 0}
    for category_id in categories:
        part = metrics.stuck_money(conn, category_id, department_id)
        for key in ("amount", "deals", "filled", "foreign"):
            total[key] += part[key]
    return {
        **total,
        "amount": round(total["amount"], 0),
        "coverage": _share(total["filled"], total["deals"] - total["foreign"]),
        "currency": base_currency(),
    }


def _funnels(conn, categories: Sequence[int]) -> list[dict[str, Any]]:
    """Названия воронок плана — чтобы экран мог их назвать, а не номер."""
    known = {row["category_id"]: row["name"] for row in metrics.pipelines(conn)}
    return [
        {"category_id": int(value),
         "name": known.get(int(value)) or f"Воронка {int(value)}"}
        for value in categories
    ]


def _coverage(
    conn, since: str, until: str, categories: Sequence[int],
) -> dict[str, Any]:
    """Покрытие поля суммы у выигранных сделок периода.

    Обязательно рядом с планом: при заполненности в 70 процентов выполнение
    занижено, а не «маленькое», и решение по такому числу принимается другое.

    Считается по тем же воронкам, что и факт. Покрытие по всем сделкам
    подряд отвечало бы на вопрос о данных, которых в этом плане нет, и
    подпись под суммой описывала бы не эту сумму.
    """
    where, params = plans.category_filter("v_deal", categories)
    row = _one(
        conn,
        f"""
        SELECT COUNT(*) AS deals,
               SUM(CASE WHEN opportunity > 0 AND {_money_of()} THEN 1 ELSE 0 END) AS filled,
               SUM(CASE WHEN NOT {_money_of()} THEN 1 ELSE 0 END) AS foreign_deals
        FROM v_deal
        WHERE is_won = 1 AND closedate IS NOT NULL
          AND closedate >= :since AND closedate < :until
          AND {where}
        """,
        {"since": since, "until": until, "base": base_currency(), **params},
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
