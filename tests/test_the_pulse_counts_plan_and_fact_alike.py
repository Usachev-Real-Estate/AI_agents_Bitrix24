"""Сделка засчитывается отделу, в котором она закрыта.

Решение агентства от 07.09. План третьего квартала несут 29 названных
человек; в тех же отделах работают ещё двадцать — новички без нормы и
руководители. Их сделки приносят отделу настоящие деньги, и вычитать их из
выполнения значит недосчитывать работу отдела.

Цена решения не спрятана: ``fact_on_plan`` показывает, сколько из факта
сделали те, кто норму несёт. Отдел, закрывший план чужими руками,
отличается от отдела, где сработали плановые люди, — на процент это не
влияет, но на экране разница видна.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import plans
import pulse as pulse_module
from schema import analytics_session
from scope import Scope, scoped_session

Q3 = "2026-Q3"
# Середина квартала: 44 рабочих дня из 66 позади. Фиксируем момент, иначе
# тест на темп начинал бы врать в первый же день следующего квартала.
MID = "2026-08-31T12:00:00+03:00"


def _user(conn, user_id, name, last_name, dept_id, dept_name, active=1):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id,"
        " department_name, is_active, synced_at) VALUES (?, ?, ?, ?, ?, ?, 'x')",
        (user_id, name, last_name, dept_id, dept_name, active),
    )


def _won(conn, deal_id, user_id, amount, *, closed="2026-08-10T09:00:00+00:00",
         currency="RUB"):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, 18, 'C18:WON', ?, 'CALL', ?, ?, '2026-07-01T00:00:00+00:00',
                ?, ?, 1, 1, 0, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", user_id, amount, currency, closed, closed),
    )


def _norm(conn, user_id, amount, period=Q3):
    conn.execute(
        "INSERT INTO plan_norm(period_code, scope_kind, scope_id, metric,"
        " basis, amount, source, updated_at)"
        " VALUES (?, 'user', ?, 'commission', 'absolute', ?, 'test', 'x')",
        (period, user_id, amount),
    )


@pytest.fixture
def agency(analytics_db):
    """Отдел продаж: РОП, два брокера с нормой и новичок без неё."""
    with analytics_session() as conn:
        _user(conn, 1, "Антон Кретов", "Кретов", 60, "Кретов")
        _user(conn, 2, "Брокер Первый", "Первый", 60, "Кретов")
        _user(conn, 3, "Брокер Второй", "Второй", 60, "Кретов")
        _user(conn, 4, "Новичок Без Нормы", "Новичков", 60, "Кретов")
        _norm(conn, 2, 3_500_000)
        _norm(conn, 3, 5_500_000)
    return analytics_db


def _pulse(scope=None):
    with scoped_session(scope or Scope.everything()) as conn:
        return pulse_module.pulse(conn, Q3, today=MID)


# --------------------------------------------------------------------------

def test_a_newcomers_deal_counts_for_the_department(agency):
    """Сделка новичка без нормы — деньги отдела, и в выполнение она входит."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000)      # брокер с нормой
        _won(conn, 2, 4, 4_000_000)      # новичок без нормы

    result = _pulse()

    assert result["fact"] == 5_000_000, "сделка засчитана отделу, где закрыта"
    assert result["fact_on_plan"] == 1_000_000, "сколько сделали люди с нормой"
    assert result["others"]["fact"] == 4_000_000


def test_the_split_shows_who_actually_earned_it(agency):
    """Два отдела с одинаковым процентом — разные отделы, и это видно.

    Процент выполнения от разделения не зависит, но вопрос «сам отдел
    вытянул или за счёт тех, кому плана не ставили» иначе не задать.
    """
    with analytics_session() as conn:
        for deal_id, user_id, amount in (
            (1, 2, 1_500_000), (2, 3, 2_000_000), (3, 4, 700_000), (4, 1, 300_000),
        ):
            _won(conn, deal_id, user_id, amount)

    result = _pulse()
    dept = result["departments"][0]

    assert dept["fact"] == 4_500_000, "все сделки отдела"
    assert dept["fact_on_plan"] == 3_500_000, "из них у двух брокеров с нормой"
    assert dept["others_fact"] == 1_000_000, "новичок и РОП"
    assert dept["fact_on_plan"] + dept["others_fact"] == dept["fact"]


def test_a_rops_own_deal_counts_for_the_department(agency):
    """РОП плана не несёт, но закрытая им сделка — деньги его отдела."""
    with analytics_session() as conn:
        _won(conn, 1, 1, 2_500_000)

    result = _pulse()

    assert result["fact"] == 2_500_000
    assert result["fact_on_plan"] == 0, "норму он не несёт"
    assert result["others"]["deals"] == 1


def test_the_plan_is_the_sum_of_named_norms(agency):
    """9 млн — это 3,5 плюс 5,5, а не норма, умноженная на состав."""
    result = _pulse()

    assert result["plan"] == 9_000_000
    assert result["brokers_on_plan"] == 2
    assert result["without_norm"] == 2, "РОП и новичок"


def test_being_behind_is_measured_against_working_days(agency):
    """К 31 августа позади 44 рабочих дня из 66 — две трети квартала."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 3_000_000)

    result = _pulse()

    assert result["total_days"] == 66
    assert result["elapsed_days"] == 44
    assert result["time_share"] == 66.7
    assert result["plan_share"] == 33.3
    assert result["behind"] is True


def test_a_deal_outside_the_quarter_is_not_counted(agency):
    """Сделка июня в третий квартал не попадает: границы исключающие."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 9_000_000, closed="2026-06-30T20:00:00+00:00")
        _won(conn, 2, 2, 1_000_000, closed="2026-07-01T05:00:00+00:00")

    result = _pulse()

    assert result["fact"] == 1_000_000, "июньская сделка осталась во втором квартале"


def test_a_foreign_currency_deal_is_not_added_to_roubles(agency):
    """Долларовая сделка в сумму не входит и видна в покрытии."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000)
        _won(conn, 2, 3, 50_000, currency="USD")

    result = _pulse()

    assert result["fact"] == 1_000_000
    assert result["coverage"]["foreign"] == 1
    assert result["coverage"]["deals"] == 2


def test_an_unfilled_commission_lowers_the_coverage(agency):
    """Покрытие рядом с планом обязательно: неполное поле занижает выполнение."""
    with analytics_session() as conn:
        _won(conn, 1, 2, 1_000_000)
        _won(conn, 2, 3, 0)

    result = _pulse()

    assert result["coverage"]["deals"] == 2
    assert result["coverage"]["filled"] == 1
    assert result["coverage"]["share"] == 50.0


def test_a_rop_sees_only_their_own_department(agency):
    """Ограниченное соединение сужает и план, и факт, и итог компании."""
    with analytics_session() as conn:
        _user(conn, 9, "Чужой Брокер", "Чужой", 50, "Волкова")
        _norm(conn, 9, 4_500_000)
        _won(conn, 1, 2, 1_000_000)
        _won(conn, 2, 9, 8_000_000)

    result = _pulse(Scope.departments([60]))

    assert result["plan"] == 9_000_000, "чужая норма в план не попала"
    assert result["fact"] == 1_000_000, "чужой факт тоже"
    assert result["fact_on_plan"] == 1_000_000
    assert [row["department_id"] for row in result["departments"]] == [60]


def test_no_plan_gives_no_verdict(analytics_db):
    """Без норм период не отчитывается о выполнении, а говорит, что плана нет."""
    with analytics_session() as conn:
        _user(conn, 1, "Брокер", "Брокеров", 60, "Кретов")
        _won(conn, 1, 1, 5_000_000)

    result = _pulse()

    assert result["plan"] == 0
    assert result["plan_share"] is None
    assert result["behind"] is None
    assert result["reason"] == "план не задан"
    assert result["fact"] == 5_000_000, "деньги никуда не делись"
    assert result["fact_on_plan"] == 0


def test_the_period_bounds_come_from_the_quarter_not_from_today(agency):
    """Границы квартала фиксированные, а не «по сегодня».

    Пресет ``quarter`` в metrics даёт квартал по текущий день, и планом быть
    не может: в первый день квартала выполнение вышло бы стопроцентным.
    """
    result = _pulse()

    assert result["period"]["starts_at"] == plans.quarter_bounds(Q3)[0]
    assert result["period"]["ends_at"] == plans.quarter_bounds(Q3)[1]
    assert result["period"]["declared"] is False
