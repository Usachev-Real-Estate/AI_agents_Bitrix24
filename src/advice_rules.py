"""Правила, из которых рождаются советы.

Каждое правило — условие на числах, которые уже считаются где-то ещё, плюс
слова, которыми оно объясняет себя руководителю. Ни одно не считает
собственных метрик: разошедшееся определение «зависшей сделки» между
советом и экраном означало бы, что сводка зовёт разбирать карточки, которых
на дашборде нет.

Правила отдают кандидатов ЦЕЛИКОМ, без отбора по порогу. Отбор делает
advice.select(), и делает не только по величине: значение нужно и тем
кандидатам, о ком речь уже шла, — иначе проверить «стало лучше» было бы не
с чем, и сработавший совет молча исчез бы вместо похвалы.

Значение у всех правил в одну сторону: БОЛЬШЕ ЗНАЧИТ ХУЖЕ.

Вес — для сортировки внутри места. У денег это рубли, у работы — карточки.
Между местами веса не сравниваются никогда, и общего рейтинга нет: 315
нетронутых карточек вытеснили бы деньги из сводки каждое утро на месяцы.
"""

from __future__ import annotations

from typing import Any, Sequence

import wording
from advice import SLOT_ACUTE, SLOT_MONEY, SLOT_PROMISE, SLOT_WORK, Advice

# Сколько карточек должно быть у брокера, чтобы говорить о его доле.
# Двое из трёх — это 67% и ничего не значит.
MIN_CARDS = 10

# Доля холодных карточек, с которой начинается разговор.
COLD_SHARE = 50.0

# Покрытие поля суммы, ниже которого денежные правила молчат. Совет,
# выросший из числа, которому нельзя верить, хуже отсутствия совета: он
# отправляет руководителя разбираться не туда и тратит доверие к сводке.
MIN_MONEY_COVERAGE = 50.0


def _money(value: float) -> str:
    """Рубли словами, коротко: сводку читают с телефона."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн ₽".replace(".", ",")
    if value >= 1_000:
        return f"{value / 1000:.0f} тыс ₽"
    return f"{value:.0f} ₽"


def collect(
    *,
    pulse: dict[str, Any] | None = None,
    work: dict[str, Any] | None = None,
    events: dict[str, Any] | None = None,
    sellers_work: dict[str, Any] | None = None,
    promises: list[dict[str, Any]] | None = None,
) -> list[Advice]:
    """Все кандидаты от всех правил. Порядок здесь ни на что не влияет."""
    out: list[Advice] = []
    out += breakeven_gap(pulse)
    out += behind_pace(pulse)
    out += broker_cold(sellers_work or work)
    out += promise_overdue(promises)
    out += went_backwards(events)
    out += left_from_late_stage(events)
    return out


def _money_is_trustworthy(pulse: dict[str, Any]) -> bool:
    """Можно ли советовать по деньгам этого пульса.

    Одно определение на все денежные правила: и рубеж, и отставание считают
    по одному и тому же факту, и охрана, накрывшая одно из них, оставила бы
    второе советовать по тому же дырявому числу.

    Ноль закрытых сделок — это отсутствие сведений о покрытии, а не плохое
    покрытие. Отдел бывает позади именно потому, что не закрыл ничего, и
    молчать об этом было бы ровно наоборот тому, что нужно.
    """
    coverage = pulse.get("coverage") or {}
    if not coverage.get("deals"):
        return True
    return coverage.get("share", 100) >= MIN_MONEY_COVERAGE


def breakeven_gap(pulse: dict[str, Any] | None) -> list[Advice]:
    """Сколько не хватает до нуля по прибыли при нынешнем темпе.

    Главное денежное правило, и оно выше отставания от плана по весу.
    Причина в природе двух чисел. План агентства — намеренная планка
    (решение от 07.09): выполнение по ней держится в диапазоне 0–15% весь
    квартал, и «отстаём на 33,9 млн» верно каждое утро, а сделать с ним
    сегодня нечего. Рубеж безубыточности отвечает на другой вопрос — не «к
    чему тянемся», а «доживём ли», — и он движется от каждой сделки.

    Совет обязан назвать, ГДЕ ближайшие деньги, иначе он остаётся числом.
    Называется отдел с наибольшей суммой на незакрытых сделках: это не
    обещание, что деньги придут оттуда, а ответ на вопрос «с чего начать».
    """
    if not pulse or not _money_is_trustworthy(pulse):
        return []
    mark = pulse.get("breakeven")
    # None — расходы не заданы. Выдуманный рубеж хуже отсутствующего: по
    # нему принимают решения о людях.
    if not mark or mark.get("gap") is None or mark.get("reaches"):
        return []
    gap = abs(mark["gap"])
    if gap <= 0:
        return []
    where = _richest_stuck(pulse)
    return [Advice(
        rule="breakeven_gap",
        subject="company",
        slot=SLOT_MONEY,
        who="Квартал",
        # Вес выше любого отставания от плана: плановая планка намеренно
        # высока и от работы не меняется, а рубеж решает прибыль квартала.
        value=round(gap),
        weight=gap * 10,
        title=f"До безубыточности не хватает {_money(gap)} при нынешнем темпе",
        action=(f"Соберите РОПов и разберите, что закрывается до конца "
                f"квартала{where}"),
        proof=(f"порог {_money(mark['gross'])}, сделано {_money(pulse['fact'])} "
               f"({mark['share']:.0f}%), прогноз по темпу "
               f"{_money(pulse.get('projection') or 0)}"),
        check="Завтра скажу, сократился ли разрыв",
        link="/pulse",
    )]


def _richest_stuck(pulse: dict[str, Any]) -> str:
    """Отдел с наибольшей суммой на незакрытых сделках — с чего начинать."""
    best, amount = None, 0.0
    for row in pulse.get("departments") or []:
        stuck = row.get("stuck") or {}
        value = float(stuck.get("amount") or 0)
        if value > amount:
            best, amount = row, value
    if best is None or amount <= 0:
        return ""
    # Каждое слово в форме, не зависящей от числа: «на 101 незакрытых
    # сделках» — ошибка согласования, а склонять числительное в коде ради
    # одной строки незачем.
    return (f". Больше всего денег стоит у отдела {best['name']} — "
            f"{_money(amount)}, незакрытых сделок {best['stuck']['deals']}")


def behind_pace(pulse: dict[str, Any] | None) -> list[Advice]:
    """Отдел, отстающий от темпа квартала сильнее прочих.

    Значение — рубли отставания, а не проценты: отдел с планом в 40 млн и
    отставанием на 10% теряет вчетверо больше отдела с планом в 10 млн и
    отставанием на 40%, и разговор с ними разный.

    Правило молчит, если сумма заполнена меньше чем у половины сделок.
    Совет, выросший из числа с дырявым покрытием, отправляет руководителя
    разбираться не туда — а такое уже случалось.
    """
    if not pulse or not _money_is_trustworthy(pulse):
        return []
    out = []
    for row in pulse.get("departments") or []:
        plan, share, pace = row.get("plan"), row.get("plan_share"), row.get("time_share")
        if not plan or share is None or not pace:
            continue
        expected = plan * pace / 100.0
        gap = expected - (row.get("fact") or 0)
        if gap <= 0:
            continue
        out.append(Advice(
            rule="dept_behind_pace",
            subject=f"dept:{row['department_id']}",
            slot=SLOT_MONEY,
            who=f"Отдел {row['name']}",
            value=round(gap),
            weight=gap,
            title=f"Отдел {row['name']} отстаёт от темпа на {_money(gap)}",
            action=(f"Разговор с РОПом ({_rop(row)}): какие сделки закроются "
                    "до конца квартала и чего для них не хватает"),
            proof=(f"прошло {pace:.0f}% срока, сделано {share:.0f}% плана "
                   f"({_money(row.get('fact') or 0)} из {_money(plan)})"),
            check="Завтра скажу, сдвинулся ли темп",
            link="/pulse",
        ))
    return out


def _rop(row: dict[str, Any]) -> str:
    """Имя РОПа, а не запись о нём.

    В плане РОП лежит словарём с идентификатором и именем. Подставленный
    целиком, он печатался в совете как «Спросите {'user_id': 1, ...}» —
    сообщение при этом выглядело работающим и уходило руководителю.
    """
    for rop in row.get("rops") or []:
        name = rop.get("name") if isinstance(rop, dict) else str(rop)
        if name:
            return str(name)
    return f"РОПа отдела {row['name']}"


def broker_cold(work: dict[str, Any] | None) -> list[Advice]:
    """Брокер, у которого больше всего карточек лежит без работы.

    Доля, а не количество: у одного в работе 52 карточки, у другого 11, и
    «двадцать холодных» значит у них разное. Но значением идёт количество —
    оно и есть размер проблемы, и по нему видно, стало лучше или нет.

    Порог по числу карточек обязателен: двое холодных из трёх дают 67% и
    возглавили бы список, ничего при этом не значив.

    Действие называет ту половину проблемы, которая больше. Холодная
    карточка бывает двух видов, и делать с ними надо разное: до одной не
    дошли руки вовсе, а с другой поговорили и бросили. После заливки
    комментариев вторых стало втрое больше первых, и совет, зовущий
    «разобрать 9 нетронутых» при 21 брошенной, отвечал бы на треть вопроса.
    """
    if not work:
        return []
    out = []
    for row in work.get("by_user") or []:
        if row["cards"] < MIN_CARDS or row["cold_share"] < COLD_SHARE:
            continue
        out.append(Advice(
            rule="broker_cold",
            subject=f"user:{row['key']}",
            slot=SLOT_WORK,
            who=row["name"],
            value=row["cold"],
            weight=row["cold"],
            title=(f"{row['name']}: {row['cold']} "
                   f"{wording.cards(row['cold'])} из {row['cards']} "
                   f"{wording.verb(row['cold'], 'лежит', 'лежат')} без работы"),
            action=_cold_action(row),
            proof=(f"холодных карточек {row['cold_share']:.0f}%; "
                   f"не трогали вовсе {row['nothing']}, "
                   f"звонили и бросили {row['silent']}"),
            check="Завтра скажу, по скольким появился звонок",
            link="/deals",
        ))
    return out


def _cold_action(row: dict[str, Any]) -> str:
    """Что делать с холодными карточками этого брокера.

    Две беды требуют разного. «Не трогали вовсе» — это карточка, до которой
    не дошли руки: её надо взять в работу. «Звонили и бросили» — разговор
    был, но давно: туда надо вернуться, и разговор с брокером о ней другой,
    потому что он про эту карточку что-то знает.

    Названо большее из двух. Число стоит отдельным сказуемым — «их 21», — а
    не при существительном: «вернитесь к 21 карточкам» требует дательного
    падежа, и склонять числительное в коде ради одной строки незачем.
    """
    nothing, silent = row["nothing"], row["silent"]
    if nothing >= silent and nothing:
        return ("Возьмите в работу карточки без единого следа — "
                f"их {nothing}")
    if silent:
        return ("Вернитесь к тем, где разговор был давно — "
                f"их {silent}")
    return "Разберите холодные карточки вместе с ним"


def promise_overdue(rows: list[dict[str, Any]] | None) -> list[Advice]:
    """Брокер написал, что сделает, и не сделал.

    Самый сильный совет из всех, потому что не мы решили, что должно было
    произойти, — так написал сам брокер. Спорить с этим нельзя, и разговор
    получается короткий: «ты написал «позвонить в пятницу», пятница прошла».

    Группируется по человеку, а не по карточке. На живых данных четыре
    просроченных обещания оказались у одного брокера, все за один день и
    все по одному дому: это не четыре забытые карточки, а один брошенный
    день работы, и разговор о нём один.
    """
    if not rows:
        return []
    by_user: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        by_user.setdefault(row["user_id"], []).append(row)

    out = []
    for user_id, items in by_user.items():
        items.sort(key=lambda item: -(item["overdue_days"] or 0))
        oldest = items[0]
        name = oldest["broker"] or f"id {user_id}"
        days = int(oldest["overdue_days"] or 0)
        out.append(Advice(
            rule="promise_overdue",
            subject=f"user:{user_id}",
            # Своё место, а не общее с событиями по сделкам. Там вес —
            # сумма на карточке, здесь число обещаний, и в одном ряду
            # рубли побеждают штуки всегда: девяносто два невыполненных
            # обещания по агентству молчали, пока хоть одна сделка уходила
            # из работы. Соревноваться должны сопоставимые вещи.
            slot=SLOT_PROMISE,
            who=name,
            value=len(items),
            # Вес — число обещаний, а не давность: пять просроченных дел
            # важнее одного очень старого, и разговор о них один.
            weight=len(items) * 100 + days,
            title=(f"{name}: {len(items)} "
                   f"{wording.form(len(items), 'обещание', 'обещания', 'обещаний')} "
                   f"без выполнения"),
            action=_promise_action(items),
            proof=(f"дольше всех — {wording.name(oldest['title'], 34)}: "
                   f"«{_text(oldest['promised'], 80)}», срок был "
                   f"{_day(oldest['promised_at'])}, {days} дн назад"
                   + (f"; {_text(oldest['terms'], 60)}" if oldest["terms"] else "")),
            check="Скажу, если появится новая запись",
            link="/deals",
        ))
    return out


def _promise_action(items: list[dict[str, Any]]) -> str:
    """Что сделать. При нескольких обещаниях называются первые три карточки."""
    if len(items) == 1:
        return f"Спросите про {wording.name(items[0]['title'], 40)} — срок прошёл"
    names = ", ".join(wording.name(item["title"], 26) for item in items[:3])
    tail = "" if len(items) <= 3 else f" и ещё {len(items) - 3}"
    return f"Разберите вместе: {names}{tail}"


def _text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _day(stamp: str | None) -> str:
    """Дата по-человечески: 12.08 вместо 2026-08-12."""
    text = (stamp or "").strip()[:10]
    return f"{text[8:10]}.{text[5:7]}" if len(text) == 10 else text


def went_backwards(events: dict[str, Any] | None) -> list[Advice]:
    """Сделка откатилась назад по стадиям за окно.

    Самое острое из того, что бывает за сутки: назад с поздней стадии
    уходят редко, и каждый такой откат — разговор, который стоит провести
    сегодня, пока помнят почему.
    """
    if not events:
        return []
    returned = events.get("returned") or {}
    out = []
    for row in returned.get("top") or []:
        amount = float(row.get("opportunity") or 0)
        out.append(Advice(
            rule="deal_returned",
            subject=f"deal:{row['deal_id']}",
            slot=SLOT_ACUTE,
            who=wording.clip(row["title"], 34),
            # Сделка без суммы — не бесплатная, а неоценённая. Единица
            # держит её в кандидатах: иначе откат по карточке с пустой
            # комиссией не попал бы в сводку никогда.
            value=amount or 1,
            weight=amount or 1,
            # Стрелка вместо «из … в …»: названия стадий остаются в
            # именительном, склонять их в коде незачем.
            title=(f"{wording.name(row['title'], 44)} откатилась: "
                   f"«{row['from_name']}» → «{row['to_name']}»"),
            action=(f"Спросите, что произошло, пока помнят "
                    f"({row.get('assignee') or 'ответственный не указан'})"),
            proof=(f"{_money(amount)} на карточке" if amount
                   else "сумма на карточке не заполнена"),
            check="Скажу, если откатится снова",
            durable=False,
            link=f"/table?q={row['deal_id']}",
        ))
    return out


def left_from_late_stage(events: dict[str, Any] | None) -> list[Advice]:
    """Карточка ушла из работы: проиграна или отложена.

    Стоит рядом с откатом на том же месте и конкурирует с ним по сумме.
    Уход с поздней стадии дороже отката с ранней, и сортировка по деньгам
    сама выбирает, о чём говорить.
    """
    if not events:
        return []
    left = events.get("left_work") or {}
    out = []
    for row in left.get("top") or []:
        amount = float(row.get("opportunity") or 0)
        out.append(Advice(
            rule="deal_left_work",
            subject=f"deal:{row['deal_id']}",
            slot=SLOT_ACUTE,
            who=wording.clip(row["title"], 34),
            value=amount or 1,
            weight=amount or 1,
            title=(f"{wording.name(row['title'], 44)} ушла из работы: "
                   f"«{row['from_name']}» → «{row['to_name']}»"),
            action=("Разберите, почему потеряли на этой стадии "
                    f"({row.get('assignee') or 'ответственный не указан'})"),
            proof=(f"{_money(amount)} на карточке" if amount
                   else "сумма на карточке не заполнена"),
            check="Скажу, если уйдёт снова",
            durable=False,
            link=f"/table?q={row['deal_id']}",
        ))
    return out


def slots_of(candidates: Sequence[Advice]) -> dict[str, int]:
    """Сколько кандидатов на каждом месте. Для диагностики пустой сводки."""
    counts = {SLOT_MONEY: 0, SLOT_WORK: 0, SLOT_ACUTE: 0}
    for advice in candidates:
        counts[advice.slot] = counts.get(advice.slot, 0) + 1
    return counts
