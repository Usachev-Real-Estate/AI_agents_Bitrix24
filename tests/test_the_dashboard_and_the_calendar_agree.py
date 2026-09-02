"""Пять правок по итогам аудита: каждая закреплена случаем, на котором ломалась.

Тесты названы по тому, что защищают, а не по функции: править их придётся
тому, кто соберётся вернуть старое поведение, и он должен сразу увидеть цену.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import etl
import metrics
from schema import analytics_session
from scope import Scope, scoped_session


def _deal(conn, deal_id, *, stage="C18:NEW", amount=0, created, closed=None,
          won=0, lost=0, is_closed=None, user=32, cat=18):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, ?, ?, ?, 'CALL', ?, 'RUB', ?, ?, ?, ?, ?, ?, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", cat, stage, user, amount, created, created,
         closed, (1 if closed else 0) if is_closed is None else is_closed, won, lost),
    )


def _event(conn, entity_type, entity_id, stage, entered, left, duration, cat=0, seq=0):
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id,"
        " entered_at, left_at, duration_sec, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entity_type, entity_id, cat, stage, entered, left, duration, seq),
    )


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
                     " VALUES (0, 'Продавцы', 1, 10, 'x')")
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
                     " VALUES (18, 'Покупатели', 1, 20, 'x')")
        for stage_id, cat, name, sort, semantic in (
            ("NEW", 0, "Назначение встречи", 10, "in_progress"),
            ("WON", 0, "Договор закрыт", 90, "won"),
            ("C18:NEW", 18, "Подбор", 10, "in_progress"),
            ("C18:WON", 18, "Договор закрыт", 90, "won"),
            ("C18:APOLOGY", 18, "Проиграна", 95, "lost"),
        ):
            conn.execute("INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
                         " synced_at) VALUES (?, ?, ?, ?, ?, 'x')",
                         (stage_id, cat, name, sort, semantic))
        conn.execute("INSERT INTO dim_user(user_id, name, last_name, department_id,"
                     " department_name, is_active, synced_at)"
                     " VALUES (32, 'Иван Петров', 'Петров', 44, 'Отдел', 1, 'x')")
    return analytics_db


# --------------------------------------------------------------------------
# 1. норма стадии
# --------------------------------------------------------------------------

def test_a_stage_norm_is_measured_on_deals_not_leads(mart):
    """Норму стадии «Продавцов» задают сделки, а не лиды с тем же кодом статуса.

    ETL пишет события статусов лидов с category_id = 0 — тем же номером, что у
    воронки «Продавцы», — и код NEW есть у обеих. Пока норма считалась по всем
    событиям подряд, сорок лидов по часу перевешивали сделки: норма падала до
    часа, и «зависшей» становилась каждая живая карточка воронки.
    """
    with analytics_session() as conn:
        _event(conn, "deal", 1, "NEW", "2026-08-01T00:00:00+00:00",
               "2026-08-11T00:00:00+00:00", 864000)
        for lead_id in range(200, 240):
            _event(conn, "lead", lead_id, "NEW", "2026-08-01T00:00:00+00:00",
                   "2026-08-01T01:00:00+00:00", 3600)

    with scoped_session(Scope.everything()) as conn:
        norms = metrics.stage_norms(conn, 0)

    assert norms["NEW"] == pytest.approx(10.0), "норма стадии — десять дней сделки, а не час лида"


# --------------------------------------------------------------------------
# 2. журнал прогонов
# --------------------------------------------------------------------------

def test_a_failed_run_stays_in_the_journal(mart):
    """Упавший прогон обязан остаться в журнале со своей ошибкой.

    Журнал писался тем же соединением, что и данные, поэтому откат забирал с
    собой и строку об ошибке: на «Качестве данных» оставались одни успехи и лаг
    в ноль минут. Ночная сверка могла падать неделями, а страница, отвечающая
    за доверие к цифрам, показывала, что всё в порядке.
    """
    with analytics_session() as conn:
        with etl.etl_run(conn, "incremental") as counters:
            counters["rows"] = 100

    with pytest.raises(RuntimeError):
        with analytics_session() as conn:
            with etl.etl_run(conn, "full") as counters:
                counters["rows"] = 50
                _deal(conn, 777, created="2026-08-01T00:00:00+00:00")
                raise RuntimeError("портал отдал 500 на crm.deal.list")

    with scoped_session(Scope.everything()) as conn:
        status = metrics.etl_status(conn)
        rows = conn.execute("SELECT deal_id FROM fact_deal WHERE deal_id = 777").fetchall()

    kinds = [(r["kind"], r["status"]) for r in status["runs"]]
    assert ("full", "error") in kinds, "падение должно быть видно в журнале"
    assert ("incremental", "ok") in kinds
    assert "500" in [r["error"] for r in status["runs"] if r["status"] == "error"][0]
    assert rows == [], "данные упавшего прогона по-прежнему откатываются целиком"


# --------------------------------------------------------------------------
# 3. московский календарь
# --------------------------------------------------------------------------

def test_a_deal_closed_after_midnight_stays_in_its_own_month(mart):
    """Сделка, закрытая 1 сентября в 01:00 МСК, попадает в сентябрь.

    Границы суток считались по UTC, а даты на экране печатались по Москве:
    всё закрытое между полуночью и тремя часами ночи уезжало в предыдущий
    период. Ошибка вылезала ровно на стыке месяца — когда и делают отчётность.
    """
    with analytics_session() as conn:
        _deal(conn, 1, stage="C18:WON", amount=900000, won=1,
              created="2026-08-06T09:00:00+00:00",
              closed="2026-08-31T22:00:00+00:00")  # 1 сентября 01:00 МСК

    september = metrics.resolve_period("custom", "2026-09-01", "2026-09-30")
    august = metrics.resolve_period("custom", "2026-08-01", "2026-08-31")
    with scoped_session(Scope.everything()) as conn:
        sep = metrics.win_rate(conn, 18, september["since"], september["until"])
        aug = metrics.win_rate(conn, 18, august["since"], august["until"])

    assert sep["won"] == 1 and sep["won_amount"] == 900000
    assert aug["won"] == 0, "в августе этой сделки нет — по московскому календарю она сентябрьская"


def test_the_period_starts_at_moscow_midnight(mart):
    """Начало периода — полночь по Москве, то есть 21:00 UTC предыдущих суток."""
    period = metrics.resolve_period("custom", "2026-09-01", "2026-09-30")
    assert period["since"] == "2026-08-31T21:00:00+00:00"
    assert period["until"] == "2026-09-30T21:00:00+00:00"


# --------------------------------------------------------------------------
# 4. одна подпись — одно число
# --------------------------------------------------------------------------

def test_won_money_is_the_same_number_on_people_and_on_overview(mart):
    """«Выиграно денег» у сотрудников и в итоге компании — одно определение.

    На «Людях» деньги считались по сделкам, СОЗДАННЫМ в периоде, и без
    требования быть закрытой; на «Обзоре» — по закрытым, по дате закрытия.
    Руководитель сравнивал сотрудников по одному числу, а итог смотрел по
    другому, и сумма по строкам не сходилась с итогом.
    """
    with analytics_session() as conn:
        # закрыта и выиграна внутри периода — идёт в обе цифры
        _deal(conn, 1, stage="C18:WON", amount=1000000, won=1,
              created="2026-08-01T09:00:00+00:00", closed="2026-08-20T09:00:00+00:00")
        # стоит на успешной стадии, но не закрыта — не деньги ни там, ни там
        _deal(conn, 2, stage="C18:WON", amount=700000, won=1, is_closed=0,
              created="2026-08-04T09:00:00+00:00", closed="2026-08-25T09:00:00+00:00")
        # проиграна
        _deal(conn, 3, stage="C18:APOLOGY", amount=500000, lost=1,
              created="2026-08-02T09:00:00+00:00", closed="2026-08-21T09:00:00+00:00")

    period = metrics.resolve_period("custom", "2026-08-01", "2026-08-31")
    with scoped_session(Scope.everything()) as conn:
        rows = metrics.people(conn, period["since"], period["until"], 18)
        money = metrics.money(conn, 18, period["since"], period["until"])

    assert sum(r["won_amount"] for r in rows) == money["won_amount"] == 1000000
    assert sum(r["won"] for r in rows) == money["won_deals"] == 1


def test_a_deal_without_an_owner_is_named_not_left_blank(mart):
    """Сделка без ответственного даёт строку «Без ответственного», а не «None»."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,"
            " source_id, opportunity, currency_id, date_create, date_modify, closedate,"
            " is_closed, is_won, is_lost, is_deleted, synced_at)"
            " VALUES (7, 'Ничья сделка', 18, 'C18:NEW', NULL, 'CALL', 150000, 'RUB',"
            " '2026-08-07T09:00:00+00:00', '2026-08-07T09:00:00+00:00', NULL, 0, 0, 0, 0, 'x')"
        )
    period = metrics.resolve_period("custom", "2026-08-01", "2026-08-31")
    with scoped_session(Scope.everything()) as conn:
        rows = metrics.people(conn, period["since"], period["until"], 18)

    assert [r["name"] for r in rows] == ["Без ответственного"]


# --------------------------------------------------------------------------
# 5. средний чек
# --------------------------------------------------------------------------

def test_the_average_deal_counts_only_deals_that_have_a_sum(mart):
    """Средний чек делится на сделки с заполненной суммой и говорит, на сколько.

    Выигранная сделка с пустой комиссией входила в делитель штукой, а в делимое
    нулём: чек падал тем сильнее, чем хуже заполнены карточки, и выглядел как
    падение цены сделки.
    """
    with analytics_session() as conn:
        _deal(conn, 1, stage="C18:WON", amount=1000000, won=1,
              created="2026-08-01T09:00:00+00:00", closed="2026-08-20T09:00:00+00:00")
        _deal(conn, 2, stage="C18:WON", amount=0, won=1,
              created="2026-08-02T09:00:00+00:00", closed="2026-08-21T09:00:00+00:00")

    period = metrics.resolve_period("custom", "2026-08-01", "2026-08-31")
    with scoped_session(Scope.everything()) as conn:
        wins = metrics.win_rate(conn, 18, period["since"], period["until"])
        money = metrics.money(conn, 18, period["since"], period["until"])

    assert wins["avg_check"] == 1000000, "чек считается по одной заполненной сделке"
    assert wins["avg_check_base"] == 1
    assert money["won_filled"] == 1 and money["won_deals"] == 2
    assert money["won_coverage"] == 50.0, "покрытие больше не может превысить сто процентов"
