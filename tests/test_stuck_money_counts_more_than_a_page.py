"""Деньги на зависших сделках считаются по всем зависшим, а не по показанным.

stuck_deals() отдаёт список для страницы и обрезает его по limit — иначе
воронка с тысячей простоев выгружала бы тысячу строк в шаблон. Сложить
сумму по этому списку и подписать «деньги на зависших» — значит
показать деньги пятидесяти самых долгих сделок под видом итога.

Число при этом выглядит правдоподобно: оно того же порядка, растёт и падает
вместе с настоящим, и заметить подмену можно только сверкой вручную. Поэтому
итог считает отдельная функция, а список остаётся списком.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import metrics
from schema import analytics_session
from scope import Scope, scoped_session

NOW_LONG_AGO = "2026-01-01T00:00:00+00:00"


def _deal(conn, deal_id, *, amount=1000, currency="RUB", cat=18, stage="C18:NEW"):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, ?, ?, 32, 'CALL', ?, ?, ?, ?, NULL, 0, 0, 0, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", cat, stage, amount, currency,
         NOW_LONG_AGO, NOW_LONG_AGO),
    )


def _event(conn, entity_id, stage, entered, left, duration, cat=18, seq=0):
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id,"
        " entered_at, left_at, duration_sec, seq) VALUES ('deal', ?, ?, ?, ?, ?, ?, ?)",
        (entity_id, cat, stage, entered, left, duration, seq),
    )


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort,"
                     " synced_at) VALUES (18, 'Покупатели', 1, 10, 'x')")
        conn.execute("INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
                     " synced_at) VALUES ('C18:NEW', 18, 'Подбор', 10, 'in_progress', 'x')")
        conn.execute("INSERT INTO dim_user(user_id, name, last_name, department_id,"
                     " department_name, is_active, synced_at)"
                     " VALUES (32, 'Иван Петров', 'Петров', 44, 'Отдел', 1, 'x')")
        # Норма стадии: три завершённых интервала по два дня.
        for i in range(9001, 9004):
            _deal(conn, i, amount=0)
            _event(conn, i, "C18:NEW", "2026-08-01T00:00:00+00:00",
                   "2026-08-03T00:00:00+00:00", 172800)
    return analytics_db


def test_the_total_does_not_stop_at_the_page(mart):
    """60 зависших сделок по 1000 ₽ дают 60 000 ₽, а не 50 000 ₽ по длине списка."""
    with analytics_session() as conn:
        for deal_id in range(1, 61):
            _deal(conn, deal_id, amount=1000)
            _event(conn, deal_id, "C18:NEW", NOW_LONG_AGO, None, None)

    with scoped_session(Scope.everything()) as conn:
        shown = metrics.stuck_deals(conn, 18)
        total = metrics.stuck_money(conn, 18)

    assert len(shown) == 50, "список для страницы остаётся обрезанным"
    assert total["deals"] == 60
    assert total["amount"] == 60000, "итог обязан считать все зависшие, а не показанные"


def test_an_unfilled_amount_is_not_a_deal_without_risk(mart):
    """Зависшая сделка с пустой комиссией входит в счёт, но снижает покрытие.

    Иначе «2 сделки на 5000 ₽» и «20 сделок на 5000 ₽, у 18 сумма не
    заполнена» выглядят одинаково, хотя это разный размер проблемы.
    """
    with analytics_session() as conn:
        _deal(conn, 1, amount=5000)
        _event(conn, 1, "C18:NEW", NOW_LONG_AGO, None, None)
        _deal(conn, 2, amount=0)
        _event(conn, 2, "C18:NEW", NOW_LONG_AGO, None, None)

    with scoped_session(Scope.everything()) as conn:
        total = metrics.stuck_money(conn, 18)

    assert total["deals"] == 2
    assert total["filled"] == 1
    assert total["amount"] == 5000
    assert total["coverage"] == 50.0


def test_a_dollar_deal_is_counted_apart_not_added(mart):
    """Сделка в чужой валюте не складывается с рублями — курса у витрины нет."""
    with analytics_session() as conn:
        _deal(conn, 1, amount=3000, currency="RUB")
        _event(conn, 1, "C18:NEW", NOW_LONG_AGO, None, None)
        _deal(conn, 2, amount=100, currency="USD")
        _event(conn, 2, "C18:NEW", NOW_LONG_AGO, None, None)

    with scoped_session(Scope.everything()) as conn:
        total = metrics.stuck_money(conn, 18)

    assert total["amount"] == 3000, "доллары в рублёвую сумму попасть не должны"
    assert total["foreign"] == 1
    assert total["deals"] == 2
    assert total["coverage"] == 100.0, "покрытие считается без сделок в чужой валюте"


def test_a_rop_sees_only_the_money_of_their_own_department(mart):
    """Итог сужается областью видимости так же, как список.

    Сумма собирается в Python из строк, а не одним SQL-запросом, поэтому
    ограничение легко было бы потерять — оно держится на том, что строки
    берутся из v_deal.
    """
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_user(user_id, name, last_name, department_id,"
                     " department_name, is_active, synced_at)"
                     " VALUES (77, 'Пётр Сидоров', 'Сидоров', 99, 'Чужой', 1, 'x')")
        _deal(conn, 1, amount=1000)
        _event(conn, 1, "C18:NEW", NOW_LONG_AGO, None, None)
        conn.execute("UPDATE fact_deal SET assigned_by_id = 77 WHERE deal_id = 1")
        _deal(conn, 2, amount=4000)
        _event(conn, 2, "C18:NEW", NOW_LONG_AGO, None, None)

    with scoped_session(Scope.departments([44])) as conn:
        total = metrics.stuck_money(conn, 18)

    assert total["deals"] == 1
    assert total["amount"] == 4000, "чужой отдел в сумму попасть не должен"
