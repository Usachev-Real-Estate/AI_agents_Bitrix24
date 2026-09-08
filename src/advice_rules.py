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

from advice import SLOT_ACUTE, SLOT_MONEY, SLOT_WORK, Advice

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
) -> list[Advice]:
    """Все кандидаты от всех правил. Порядок здесь ни на что не влияет."""
    out: list[Advice] = []
    out += behind_pace(pulse)
    out += broker_cold(sellers_work or work)
    out += went_backwards(events)
    out += left_from_late_stage(events)
    return out


def behind_pace(pulse: dict[str, Any] | None) -> list[Advice]:
    """Отдел, отстающий от темпа квартала сильнее прочих.

    Значение — рубли отставания, а не проценты: отдел с планом в 40 млн и
    отставанием на 10% теряет вчетверо больше отдела с планом в 10 млн и
    отставанием на 40%, и разговор с ними разный.

    Правило молчит, если сумма заполнена меньше чем у половины сделок.
    Совет, выросший из числа с дырявым покрытием, отправляет руководителя
    разбираться не туда — а такое уже случалось.
    """
    if not pulse:
        return []
    coverage = pulse.get("coverage") or {}
    # Ноль закрытых сделок — это отсутствие сведений о покрытии, а не плохое
    # покрытие. Отдел бывает позади темпа именно потому, что не закрыл
    # ничего, и молчать об этом было бы ровно наоборот тому, что нужно.
    if coverage.get("deals") and coverage.get("share", 100) < MIN_MONEY_COVERAGE:
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
            title=(f"{row['name']}: {row['cold']} карточек из {row['cards']} "
                   "лежат без работы"),
            action=(f"Разберите {min(row['nothing'], 12)} карточек, где нет "
                    "ни звонка, ни отметки"),
            proof=(f"холодных карточек {row['cold_share']:.0f}%; "
                   f"не трогали вовсе {row['nothing']}, "
                   f"звонили и бросили {row['silent']}"),
            check="Завтра скажу, по скольким появился звонок",
            link="/deals",
        ))
    return out


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
            who=f"«{row['title'][:34]}»",
            # Сделка без суммы — не бесплатная, а неоценённая. Единица
            # держит её в кандидатах: иначе откат по карточке с пустой
            # комиссией не попал бы в сводку никогда.
            value=amount or 1,
            weight=amount or 1,
            title=(f"«{row['title'][:44]}» откатилась "
                   f"из «{row['from_name']}» в «{row['to_name']}»"),
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
            who=f"«{row['title'][:34]}»",
            value=amount or 1,
            weight=amount or 1,
            title=(f"«{row['title'][:44]}» ушла из работы "
                   f"с «{row['from_name']}» в «{row['to_name']}»"),
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
