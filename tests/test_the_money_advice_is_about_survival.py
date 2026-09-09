"""Денежный совет говорит о рубеже безубыточности, а не об отставании плана.

Два числа отвечают на разные вопросы, и перепутать их — значит каждое утро
советовать то, с чем сегодня нечего делать.

План агентства — намеренная планка (решение от 07.09): норма поставлена
высоко, чтобы к ней тянулись, и выполнение по ней держится в диапазоне
0–15% весь квартал. «Отдел отстаёт от темпа на 33,9 млн» верно каждое утро
и не меняется от работы. Совет по такому числу — это ежедневное напоминание
о недостижимом.

Рубеж безубыточности отвечает на вопрос «доживём ли квартал без убытка», и
он движется от каждой закрытой сделки. Разрыв до него на живых данных —
8,5 млн против 33,9 млн отставания от плана: вчетверо меньше и вчетверо
осмысленнее.

Поэтому у рубежа вес выше, и он занимает денежное место первым. Отставание
отдела остаётся — оно нужно, когда рубеж уже взят или расходы не заданы.
"""

import pytest

import advice_rules
from advice import SLOT_MONEY

PULSE = {
    "fact": 15_200_000,
    "projection": 20_000_000,
    "coverage": {"deals": 19, "share": 94.7},
    "breakeven": {"gross": 28_500_000, "share": 53.3, "left": 13_300_000,
                  "projected_share": 70.2, "reaches": False, "gap": -8_500_000},
    "departments": [
        {"department_id": 60, "name": "Кретов", "plan": 51_500_000,
         "fact": 5_200_000, "plan_share": 10.1, "time_share": 75.8,
         "rops": [{"user_id": 1, "name": "Антон Кретов"}],
         "stuck": {"amount": 190_400_000, "deals": 101}},
        {"department_id": 61, "name": "Волкова", "plan": 31_000_000,
         "fact": 5_100_000, "plan_share": 16.3, "time_share": 75.8,
         "rops": [{"user_id": 2, "name": "Ирина Волкова"}],
         "stuck": {"amount": 111_600_000, "deals": 41}},
    ],
}


def _money_advices(pulse):
    return [a for a in advice_rules.collect(pulse=pulse) if a.slot == SLOT_MONEY]


def _by_rule(pulse, rule):
    return next(a for a in _money_advices(pulse) if a.rule == rule)


def test_the_gap_to_survival_outweighs_the_gap_to_the_stretch_plan():
    """8,5 млн до нуля важнее 33,9 млн до планки, которую и не ждут взятой."""
    advices = sorted(_money_advices(PULSE), key=lambda a: -a.weight)

    assert advices[0].rule == "breakeven_gap"
    assert advices[0].value == 8_500_000


def test_the_advice_says_where_the_nearest_money_is():
    """Число без «с чего начать» — это не совет, а сводка из одной строки."""
    action = _by_rule(PULSE, "breakeven_gap").action

    assert "Кретов" in action, "назван отдел с наибольшей суммой на незакрытых"
    assert "190,4 млн ₽" in action and "101" in action


def test_a_quarter_that_pays_for_itself_needs_no_advice():
    """Рубеж взят — денежное место освобождается для отставания отдела."""
    safe = dict(PULSE, breakeven=dict(PULSE["breakeven"],
                                      reaches=True, gap=1_500_000))

    assert [a.rule for a in _money_advices(safe)] == [
        "dept_behind_pace", "dept_behind_pace",
    ]


def test_without_costs_the_rule_stays_silent():
    """Расходы не заданы — рубежа нет. Выдуманный порог хуже отсутствующего."""
    blind = dict(PULSE, breakeven=None)

    assert all(a.rule != "breakeven_gap" for a in _money_advices(blind))


def test_a_hole_in_the_amount_field_silences_the_whole_money_slot():
    """Совет по числу с дырявым покрытием отправляет разбираться не туда.

    Так уже случалось: в сводку уехали 572,6 млн, потому что порядок
    величины никто не сверил. Молчат ОБА денежных правила: рубеж и
    отставание считают по одному и тому же факту, и охрана, накрывшая одно,
    оставила бы второе советовать по тому же дырявому числу.
    """
    patchy = dict(PULSE, coverage={"deals": 19, "share": 31.6})

    assert _money_advices(patchy) == []


def test_a_quarter_without_closed_deals_is_not_a_coverage_problem():
    """Ноль закрытых — это отсутствие сведений о покрытии, а не дыра в нём."""
    fresh = dict(PULSE, coverage={"deals": 0, "share": 0})

    assert _money_advices(fresh), "молчать тут — ровно наоборот тому, что нужно"


@pytest.mark.parametrize("field", ["plan", "time_share"])
def test_a_department_without_a_plan_is_not_measured(field):
    """Без плана или без срока темпа нет, и отставание считать не из чего."""
    blank = dict(PULSE, departments=[dict(PULSE["departments"][0], **{field: None})])

    assert all(a.rule != "dept_behind_pace" for a in _money_advices(blank))


def test_the_count_of_deals_agrees_with_the_number():
    """«На 101 незакрытых сделках» — ошибка согласования в отчёте о деньгах.

    Сообщение, которое не согласует слова, выглядит машинным, а машинному
    отчёту верят меньше, чем он заслуживает — и это отчёт, по которому
    собирают РОПов.
    """
    action = _by_rule(PULSE, "breakeven_gap").action

    assert "незакрытых сделок 101" in action
    assert "101 незакрытых сделках" not in action


# --------------------------------------------------------------------------
# совет по брокеру

_WORK = {"by_user": [{
    "key": 11, "name": "Иван Мамонтов", "cards": 36, "cold": 30,
    "nothing": 9, "silent": 21, "cold_share": 83.3,
}]}


def _broker(nothing, silent, **extra):
    row = dict(_WORK["by_user"][0], nothing=nothing, silent=silent,
               cold=nothing + silent, **extra)
    return advice_rules.broker_cold({"by_user": [row]})[0]


def test_the_action_addresses_the_bigger_half():
    """Холодная карточка бывает двух видов, и делать с ними надо разное.

    До одной не дошли руки вовсе, с другой поговорили и бросили. После
    заливки комментариев вторых стало втрое больше первых, и совет, зовущий
    «разобрать 9 нетронутых» при 21 брошенной, отвечал бы на треть вопроса.
    """
    assert "их 21" in _broker(9, 21).action
    assert "разговор был давно" in _broker(9, 21).action

    assert "их 30" in _broker(30, 2).action
    assert "без единого следа" in _broker(30, 2).action


def test_an_action_never_asks_for_zero_cards():
    """«Разберите 0 карточек» — то, во что превращался совет, когда
    нетронутых не осталось, а брошенные никуда не делись."""
    action = _broker(0, 21).action

    assert "0" not in action
    assert "их 21" in action


def test_the_number_needs_no_declension():
    """«Вернитесь к 21 карточкам» требует дательного падежа.

    Число стоит отдельным сказуемым — «их 21», — и склонять числительное в
    коде ради одной строки не приходится.
    """
    for nothing, silent in ((1, 0), (2, 0), (5, 0), (0, 1), (0, 11), (0, 22)):
        action = _broker(nothing, silent).action
        assert "их " in action, action
