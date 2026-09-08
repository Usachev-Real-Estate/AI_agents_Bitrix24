"""Совет помнит, что о нём уже говорили, и знает, сработал ли он.

Память здесь важнее самих правил. Три одинаковые строки каждое утро — и
сводку перестают открывать через неделю, раньше, чем в ней появится
важное. Проверка простая и злая: прогнать один и тот же набор кандидатов
дважды подряд и убедиться, что второй раз сводка молчит.

Молчание при этом не вечное. Совет возвращается через неделю; раньше —
только если стало заметно хуже, и тогда обязан сказать, когда о нём шла
речь и каким число было тогда. Повтор с тем же числом — признание, что
сводка не следит за результатом.

Обратная сторона — «сработало». У каждого совета есть число, и на
следующем прогоне оно пересчитывается. Стало лучше — совет закрывается и
об этом говорится вслух. Без этого система советует в пустоту и никогда не
узнаёт, слушают её или нет.

Отдельно проверяется направление числа: больше значит хуже, одинаково для
всех правил. Правило, считающее наоборот, сломало бы и паузу, и похвалу —
причём молча, потому что «стало лучше» выглядит правдоподобно всегда.
"""

from datetime import datetime, timedelta, timezone

import pytest

import advice as advice_module
from advice import Advice, load, remember, select
from db import db_session, init_db

DAY = timedelta(days=1)
NOW = datetime(2026, 9, 9, 6, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Своя база на тест: память советов переживает прогон по определению."""
    monkeypatch.setattr("db.DB_PATH", tmp_path / "violations.db")
    init_db()
    yield


def _advice(rule="broker_cold", subject="user:11", slot="work", value=30.0,
            title="Абзалилов не начинал", weight=None, who="Марат Абзалилов"):
    return Advice(
        rule=rule, subject=subject, slot=slot, value=value, who=who,
        title=title, action="Разберите 12 карточек",
        proof=f"{value:.0f} карточек без следа", check="Завтра скажу, сколько сдвинулось",
        weight=value if weight is None else weight,
    )


def _run(candidates, when):
    with db_session() as conn:
        memory = load(conn)
        chosen = select(candidates, memory, now=when)
        remember(conn, chosen, now=when)
    return chosen


# --------------------------------------------------------------------------
# память

def test_the_same_advice_is_silent_the_next_morning():
    """Главная проверка: два прогона подряд — во второй раз тишина."""
    first = _run([_advice()], NOW)
    second = _run([_advice()], NOW + DAY)

    assert [a.rule for a in first.advices] == ["broker_cold"]
    assert second.advices == [], "совет повторился на следующее утро"
    assert [a.rule for a in second.muted] == ["broker_cold"]


def test_a_week_later_it_speaks_again():
    """Пауза не вечная: через неделю проблема снова заслуживает слов."""
    _run([_advice()], NOW)
    later = _run([_advice()], NOW + 7 * DAY)

    assert [a.rule for a in later.advices] == ["broker_cold"]
    assert later.reasons[("broker_cold", "user:11")] == "снова"


def test_getting_worse_breaks_the_silence():
    """Стало хуже на пятую часть — молчать нельзя, и это отдельная причина."""
    _run([_advice(value=30)], NOW)
    worse = _run([_advice(value=37)], NOW + DAY)

    assert [a.rule for a in worse.advices] == ["broker_cold"]
    assert worse.reasons[("broker_cold", "user:11")] == "хуже"


def test_a_small_drift_is_not_worse():
    """Одна карточка туда-сюда — это дрожание числа, а не ухудшение."""
    _run([_advice(value=30)], NOW)
    drift = _run([_advice(value=32)], NOW + DAY)

    assert drift.advices == []


def test_one_thought_is_not_repeated_under_a_new_name():
    """Правило отдыхает целиком, а не только про названного человека.

    Отделов пять, и все отстают от плана. Пауза, устроенная только по
    адресату, выпустила бы «отдел отстаёт» пять утр подряд — формально
    каждый раз про нового, на вид пять одинаковых сообщений. Именно так
    сводку и перестают читать, и по адресату этого не видно.
    """
    _run([_advice(subject="user:11")], NOW)
    other = _run([_advice(subject="user:11"), _advice(subject="user:12")],
                 NOW + DAY)

    assert other.advices == [], "та же мысль под другим именем — тот же повтор"


def test_the_rule_speaks_again_about_the_next_person_after_its_rest():
    """Отдых короткий: через два дня очередь следующего."""
    _run([_advice(subject="user:11")], NOW)
    later = _run([_advice(subject="user:11"), _advice(subject="user:12")],
                 NOW + 2 * DAY)

    assert [a.subject for a in later.advices] == ["user:12"]


def test_a_rested_rule_never_blocks_a_worsening():
    """Отдых уступает ухудшению — ради этого исключение и заводилось."""
    _run([_advice(subject="user:11", value=30)], NOW)
    worse = _run([_advice(subject="user:11", value=40)], NOW + DAY)

    assert worse.reasons[("broker_cold", "user:11")] == "хуже"


# --------------------------------------------------------------------------
# проверяемость

def test_an_advice_that_worked_is_named_and_closed():
    """Стало лучше — сводка говорит это вслух, а совет закрывается."""
    _run([_advice(value=30)], NOW)
    better = _run([_advice(value=18)], NOW + 3 * DAY)

    assert len(better.resolved) == 1
    row = better.resolved[0]
    assert (row["was"], row["now"]) == (30.0, 18.0)
    assert row["days"] == 3

    with db_session() as conn:
        assert load(conn)[("broker_cold", "user:11")].closed_at


def test_a_problem_that_vanished_counts_as_solved():
    """Правило перестало видеть проблему — это тоже результат, и он ноль."""
    _run([_advice(value=30)], NOW)
    gone = _run([], NOW + 2 * DAY)

    assert gone.resolved[0]["now"] == 0.0


def test_a_solved_advice_is_not_praised_twice():
    """Похвала звучит один раз: закрытый совет из наблюдения выбывает."""
    _run([_advice(value=30)], NOW)
    _run([_advice(value=10)], NOW + 2 * DAY)
    again = _run([_advice(value=10)], NOW + 3 * DAY)

    assert again.resolved == []


def test_a_returning_problem_starts_its_history_anew():
    """Вернулась после закрытия — это новая история, а не продолжение старой."""
    _run([_advice(value=30)], NOW)
    _run([_advice(value=5)], NOW + 2 * DAY)
    back = _run([_advice(value=40)], NOW + 4 * DAY)

    assert [a.rule for a in back.advices] == ["broker_cold"]
    assert back.reasons[("broker_cold", "user:11")] == "вернулось"
    with db_session() as conn:
        row = load(conn)[("broker_cold", "user:11")]
    assert row.closed_at is None
    assert row.first_value == 40.0, "первое значение — от новой истории"


def test_praise_today_is_not_advice_tomorrow():
    """Закрытый совет не выходит назавтра как новый.

    Иначе получается качель: «сработало, было 30, стало 10» сегодня и «у
    него 10 карточек без следа» завтра — про одно и то же, в
    противоположных тонах, и так на каждом шаге медленного улучшения.
    """
    _run([_advice(value=30)], NOW)
    praised = _run([_advice(value=10)], NOW + 2 * DAY)
    assert praised.resolved and praised.advices == []

    tomorrow = _run([_advice(value=10)], NOW + 3 * DAY)
    assert tomorrow.advices == [], "похвалил вчера — сегодня не советуй то же"
    assert tomorrow.resolved == []


def test_a_slow_improvement_is_praised_once_not_every_step():
    """Медленное улучшение — одна похвала, а не по одной на каждый шаг."""
    _run([_advice(value=40)], NOW)
    praised = [
        _run([_advice(value=value)], NOW + day * DAY).resolved
        for value, day in ((30, 2), (24, 3), (19, 4), (15, 5))
    ]

    assert sum(len(row) for row in praised) == 1


def test_an_old_advice_stops_being_watched():
    """Дольше месяца — это уже не «сделай сегодня», а другой разговор."""
    _run([_advice(value=30)], NOW)
    late = _run([_advice(value=1)], NOW + 40 * DAY)

    assert late.resolved == []


def test_solving_it_frees_the_slot_for_the_next_problem():
    """Закрытый совет не занимает место: его отдают следующей проблеме."""
    _run([_advice(subject="user:11", value=30)], NOW)
    day = _run(
        [_advice(subject="user:11", value=5, weight=99),
         _advice(subject="user:12", value=20, weight=20)],
        NOW + 2 * DAY,
    )

    assert [a.subject for a in day.advices] == ["user:12"]
    assert [r["subject"] for r in day.resolved] == ["user:11"]


# --------------------------------------------------------------------------
# места

def test_each_slot_gets_exactly_one_advice():
    """Три места, по одному совету. Иначе сводка превращается в список."""
    chosen = _run([
        _advice(rule="money_a", subject="deal:1", slot="money", value=9, weight=9),
        _advice(rule="money_b", subject="deal:2", slot="money", value=5, weight=5),
        _advice(rule="work_a", subject="user:11", slot="work", value=30, weight=30),
        _advice(rule="acute_a", subject="deal:3", slot="acute", value=2, weight=2),
    ], NOW)

    assert [a.slot for a in chosen.advices] == ["money", "work", "acute"]
    assert [a.rule for a in chosen.advices] == ["money_a", "work_a", "acute_a"]


def test_a_quiet_slot_yields_to_the_next_candidate():
    """Лучший на месте молчит из-за паузы — место занимает следующий."""
    _run([_advice(rule="work_a", subject="user:11", value=30, weight=30)], NOW)
    day = _run([
        _advice(rule="work_a", subject="user:11", value=30, weight=30),
        _advice(rule="work_b", subject="user:12", value=20, weight=20),
    ], NOW + DAY)

    assert [a.rule for a in day.advices] == ["work_b"]


def test_an_empty_slot_prints_nothing():
    """Пустое место — это пустое место, а не строка «всё хорошо»."""
    chosen = _run([_advice(slot="work")], NOW)

    assert len(chosen.advices) == 1


# --------------------------------------------------------------------------
# рубеж

def test_an_advice_without_a_name_never_reaches_the_summary():
    """«Сделайте что-нибудь с воронкой» — наблюдение, а не совет."""
    kept = advice_module.only_named([
        _advice(),
        Advice(rule="vague", subject="", slot="work", value=10, who="",
               title="Воронка", action="Поработайте", proof="", check=""),
        Advice(rule="zero", subject="user:9", slot="work", value=0, who="Кто-то",
               title="Ничего", action="Ничего", proof="", check=""),
    ])

    assert [a.rule for a in kept] == ["broker_cold"]


# --------------------------------------------------------------------------
# уровень против события

def test_a_vanished_event_is_not_a_victory():
    """Событие ушло из окна — это ход календаря, а не заслуга.

    Совет об уровне («30 карточек холодные») улучшается, и падение числа
    стоит назвать. Совет о событии («сделка откатилась вчера») назавтра
    просто не попадает в суточное окно. Похвалить за это значит записать
    себе в актив течение времени — и соврать убедительно, потому что строка
    «сработало» выглядит одинаково в обоих случаях.
    """
    event = Advice(
        rule="deal_returned", subject="deal:8123", slot="acute", value=2_100_000,
        who="«Ленинский 45»",
        title="«Ленинский 45» откатилась", action="Спросите, что произошло",
        proof="2,1 млн ₽", check="Скажу, если откатится снова",
        weight=2_100_000, durable=False,
    )
    _run([event], NOW)
    tomorrow = _run([], NOW + DAY)

    assert tomorrow.resolved == [], "сводка похвалила себя за вчерашний день"


def test_a_level_that_fell_is_still_a_victory():
    """Обратная проверка: уровень падает по делу, и об этом говорят."""
    _run([_advice(value=30)], NOW)
    better = _run([], NOW + DAY)

    assert [row["subject"] for row in better.resolved] == ["user:11"]


def test_a_repeated_event_is_named_a_repeat():
    """Событие судят по повтору — и повтор память называет вслух."""
    def event(value):
        return Advice(
            rule="deal_returned", subject="deal:8123", slot="acute", value=value,
            who="«Ленинский 45»",
            title="«Ленинский 45» откатилась", action="Спросите, что произошло",
            proof="", check="Скажу, если откатится снова", weight=value,
            durable=False,
        )

    _run([event(2_100_000)], NOW)
    again = _run([event(2_100_000)], NOW + 8 * DAY)

    assert again.reasons[("deal_returned", "deal:8123")] == "снова"


def test_the_praise_names_what_improved():
    """«Было 30, стало 18» без имени — не строка отчёта.

    К моменту похвалы правило проблемы уже не видит, и взять имя неоткуда:
    оно лежит в памяти с того дня, когда совет прозвучал. Заголовок совета
    для этого не годится — в нём стоит число, и рядом с «было 30, стало 18»
    оно читается третьим, противоречащим обоим.
    """
    _run([_advice(value=30, who="Марат Абзалилов")], NOW)
    better = _run([], NOW + 2 * DAY)

    assert better.resolved[0]["who"] == "Марат Абзалилов"


# --------------------------------------------------------------------------
# слова

def test_a_deal_name_is_quoted_so_the_verb_has_something_to_agree_with():
    """«Ленинградское ш. 12 ушла» читается как ошибка. В кавычках — как имя.

    Род произвольного названия коду неизвестен, и кавычки — единственный
    способ не спорить с ним в каждой строке. Название со своими кавычками в
    чужие не заворачивается: матрёшка читается хуже отсутствия внешней пары.
    """
    import wording

    assert wording.name("Ленинградское ш. 12", 44) == "«Ленинградское ш. 12»"
    assert wording.name("ЖК «Снегири Эко»", 44) == "ЖК «Снегири Эко»"


def test_a_clipped_name_never_leaves_a_hanging_quote():
    """Обрезанное название теряет закрывающую кавычку — строка выглядит битой."""
    import wording

    clipped = wording.name("Катерина Прайм + ЖК «Victory Park Residences»", 34)

    assert clipped.count("«") == clipped.count("»")
    assert clipped.endswith("…»")
