"""«План на день»: то же, что приходит утром в Битрикс, но на экране.

Утреннее сообщение и эта страница отвечают на один вопрос — «что делать
сегодня», — и обязаны отвечать одинаково. Поэтому здесь не заведено ни
одного собственного правила: советы собирает advice_rules, отбирает
advice.select, разбор воронки считает events.funnel_events, списки карточек
даёт work — ровно те же вызовы, что в pulse_digest. Второй набор правил
однажды разошёлся бы с первым, и разошёлся бы молча: сообщение говорило бы
одно, экран другое, и оба выглядели бы правдоподобно.

Разница между сообщением и страницей ровно одна, и она сознательная.
Сообщение показывает СЕГОДНЯШНЕЕ: три совета, по одному на место, остальные
придержаны памятью, чтобы не повторяться каждое утро. Страницу открывают,
когда хотят разобраться, поэтому под сегодняшним лежит полный список — все
кандидаты, включая придержанные, с объяснением, почему их сегодня нет.

Страница ничего не запоминает. Память советов пишет только рассылка: если бы
открытие экрана считалось «я об этом сказал», совет исчезал бы из утреннего
сообщения оттого, что кто-то открыл вкладку. По той же причине база памяти
открывается строго на чтение — дашборд не создаёт и не меняет базу агента.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import advice
import advice_rules
import db
import events as funnel
import plans
import pulse as pulse_metrics
import work

logger = logging.getLogger(__name__)


def gather(
    conn,
    settings: Any,
    *,
    period_code: str,
    window: dict[str, Any],
    now=None,
) -> dict[str, Any]:
    """Всё, что нужно странице, с уже суженного соединения.

    Соединение приходит извне и уже ограничено областью видимости
    пользователя: РОП собирает эту страницу из своих отделов, а не получает
    общий расчёт с отфильтрованным хвостом.
    """
    sellers_id = int(settings.sellers_category_id)
    funnels = [sellers_id, *plans.plan_category_ids()]

    company = pulse_metrics.pulse(conn, period_code, with_stuck=True)
    events = funnel.funnel_events(
        conn, window["since"], window["until"],
        on_plan_ids=_on_plan_ids(company),
    )
    # Воронка продавцов разбирается отдельно и в план не входит: там другой
    # чек и другая работа. Вопрос к ней один — где не дорабатывают с
    # собственниками, — и отвечают на него уходы, а не деньги.
    sellers = funnel.funnel_events(
        conn, window["since"], window["until"], categories=[sellers_id],
    )
    sellers_work = work.card_work(conn, [sellers_id])
    promises = work.promises(conn, funnels)
    refusals = work.refused_in_work(conn, funnels)
    vague = work.promises_without_date(conn, funnels)

    candidates = advice.only_named(advice_rules.collect(
        pulse=company, events=events, sellers_work=sellers_work,
        promises=promises, refusals=refusals, vague=vague,
    ))
    selection = _select(candidates, now=now)

    return {
        "window": window,
        "advice": selection,
        # Полный список под сегодняшним, разложенный по причине молчания:
        # «придержан» и «место занято» — разные вещи, и объединять их в
        # «остальное» значит не ответить на вопрос, ради которого список и
        # открывают.
        "held": _held(candidates, selection),
        "outweighed": _outweighed(candidates, selection),
        "reasons": (selection.reasons if selection else {}),
        "events": events,
        "sellers": sellers,
        "sellers_work": sellers_work,
        "promises": promises,
        "refusals": refusals,
        "vague": vague,
    }


def _held(candidates, selection) -> list:
    """Кандидаты, промолчавшие из-за паузы: о них говорили на днях.

    Пауза — не отмена. Совет, повторённый каждое утро, перестают читать
    вместе со всей сводкой, поэтому рассылка выдерживает срок. Но на
    странице придержанное обязано быть видно: «почему сегодня об этом
    молчат» — законный вопрос, и без ответа он читается как пропажа.
    """
    if selection is None:
        return []
    return sorted(selection.muted, key=lambda item: (item.slot, -item.weight))


def _outweighed(candidates, selection) -> list:
    """Кандидаты, проигравшие место более тяжёлому.

    Место — единица сравнения: в нём лежат соизмеримые веса, и второй по
    тяжести совет о деньгах молчит не потому, что он неверен, а потому что
    первый дороже. Здесь он назван.
    """
    if selection is None:
        return sorted(candidates, key=lambda item: (item.slot, -item.weight))
    spoken = {item.key for item in selection.advices}
    spoken |= {item.key for item in selection.muted}
    rest = [item for item in candidates if item.key not in spoken]
    return sorted(rest, key=lambda item: (item.slot, -item.weight))


def _on_plan_ids(company: dict[str, Any]) -> set[int]:
    """Кто несёт норму. Молчание такого человека — совсем другая новость."""
    return {
        member["user_id"]
        for row in company.get("departments", [])
        for member in row.get("brokers", [])
        if member.get("plan") is not None
    }


def _select(candidates, *, now=None):
    """Сегодняшний отбор по памяти рассылки. Ошибка памяти страницу не роняет.

    База памяти — не витрина: она своя, маленькая, её пишет агент, и она
    может быть занята или ещё не создана. Полный список кандидатов при этом
    посчитан и верен, и терять из-за памяти всю страницу неправильно —
    теряется только пометка «сегодня» напротив трёх строк.
    """
    try:
        # Строго на чтение и отдельным подключением: страница не должна ни
        # создавать базу агента, ни держать на ней транзакцию записи, пока
        # рассылка в неё пишет.
        # Путь читается у db в момент вызова, а не запоминается при
        # импорте: иначе подмена базы (в тестах — и не только) молчаливо
        # промахнулась бы мимо этого модуля.
        conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            memory = advice.load(conn)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("Память советов недоступна, отбор пропущен: %s", error)
        return None
    # praise=False: хвалить может только тот, кто говорил. «Сработало»
    # сравнивает память с сегодняшними кандидатами, а память пишет
    # рассылка — по всей компании. Экран РОПа считает на суженном
    # соединении и чужого совета не видит; принимая это за «стало ноль»,
    # он печатал чужой отдел с чужими деньгами.
    return advice.select(candidates, memory, now=now, praise=False)


# Как называется место на экране. В коде места — короткие ключи, потому что
# по ним сравнивают; читателю нужно слово, объясняющее, почему эти советы
# соревнуются между собой, а с соседними — нет.
SLOT_LABEL = {
    advice.SLOT_MONEY: "Деньги",
    advice.SLOT_PROMISE: "Обещания",
    advice.SLOT_FUNNEL: "Воронка",
    advice.SLOT_WORK: "Работа по карточкам",
    advice.SLOT_ACUTE: "Срочное",
}


# Пометка о повторе. Те же слова, что в утреннем сообщении, — иначе одно и
# то же состояние называлось бы на экране и в чате по-разному.
REPEAT = {
    "хуже": "Об этом уже говорил — стало хуже.",
    "вернулось": "Считал закрытым, проблема вернулась.",
    "снова": "Говорил об этом неделю назад.",
}
