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


# --------------------------------------------------------------------------
# 6. скорость первой обработки лида
# --------------------------------------------------------------------------

def test_an_untouched_lead_counts_as_still_waiting(mart):
    """Необработанный лид не выбрасывается: его время считается «до сих пор».

    Пока в расчёт шли только лиды с закрытым первым интервалом, метрика
    отвечала на вопрос «как быстро обрабатывают тех, кого обработали», и была
    тем лучше, чем больше лидов не тронули: девять карточек, лежащих месяц, не
    мешали показать медиану в два часа по единственной десятой.
    """
    with analytics_session() as conn:
        conn.execute("INSERT INTO analytics_meta(key, value)"
                     " VALUES ('lead_history_supported', '1')")
        conn.execute("INSERT INTO dim_lead_status(status_id, name, sort, semantic,"
                     " synced_at) VALUES ('NEW', 'Не обработан', 10, 'in_progress', 'x')")
        for lead_id in range(300, 310):
            conn.execute(
                "INSERT INTO fact_lead(lead_id, title, status_id, source_id, assigned_by_id,"
                " date_create, date_modify, opportunity, currency_id, is_converted,"
                " converted_deal_id, is_deleted, synced_at)"
                " VALUES (?, ?, 'NEW', 'CALL', 32, '2026-08-01T00:00:00+00:00',"
                " '2026-08-01T00:00:00+00:00', 0, 'RUB', 0, NULL, 0, 'x')",
                (lead_id, f"Лид {lead_id}"),
            )
            if lead_id == 300:  # единственный обработанный — за два часа
                _event(conn, "lead", lead_id, "NEW", "2026-08-01T00:00:00+00:00",
                       "2026-08-01T02:00:00+00:00", 7200)
            else:               # остальные лежат с 1 августа и не тронуты
                _event(conn, "lead", lead_id, "NEW", "2026-08-01T00:00:00+00:00", None, None)

    period = metrics.resolve_period("custom", "2026-08-01", "2026-08-31")
    with scoped_session(Scope.everything()) as conn:
        first_move = metrics.lead_first_move_days(conn, period["since"], period["until"])

    assert first_move["count"] == 10, "в расчёт входят все лиды когорты, а не только обработанные"
    assert first_move["waiting"] == 9
    assert first_move["median"] > 1, "медиана — это ожидание девяти лежащих, а не два часа"


# --------------------------------------------------------------------------
# 7. зависшие сделки
# --------------------------------------------------------------------------

def test_a_stage_nobody_ever_left_still_shows_its_stuck_deals(mart):
    """Карточка не прячется только потому, что с её стадии ещё никто не уходил.

    Порог брался исключительно из завершённых интервалов той же стадии. У
    стадии без них порога не было, и условие выбрасывало все стоящие на ней
    карточки — включая те, что стоят дольше всех в воронке.
    """
    with analytics_session() as conn:
        # «Подбор» накопил норму: интервалы по два дня
        for i in (1, 2, 3):
            _deal(conn, i, stage="C18:NEW", created="2026-08-01T00:00:00+00:00")
            _event(conn, "deal", i, "C18:NEW", "2026-08-01T00:00:00+00:00",
                   "2026-08-03T00:00:00+00:00", 172800, cat=18)
        # на «Договоре» никто не завершал интервал, и карточка стоит там с января
        _deal(conn, 9, stage="C18:WON", created="2026-01-01T00:00:00+00:00")
        _event(conn, "deal", 9, "C18:WON", "2026-01-01T00:00:00+00:00", None, None, cat=18)

    with scoped_session(Scope.everything()) as conn:
        stuck = metrics.stuck_deals(conn, 18)

    assert [row["deal_id"] for row in stuck] == [9]
    assert stuck[0]["threshold_source"] == "воронка", "порог обязан назвать, откуда взялся"


def test_a_card_with_two_open_intervals_is_listed_once(mart):
    """Карточка выводится один раз, и дни считаются от входа в её нынешнюю стадию.

    Соединение шло со ВСЕМИ незакрытыми интервалами: карточка, у которой в
    истории осталось два открытых входа, выводилась дважды, и в одной из строк
    дни были от чужой стадии, а стадия — из карточки.
    """
    with analytics_session() as conn:
        for i in (1, 2, 3):
            _deal(conn, i, stage="C18:NEW", created="2026-08-01T00:00:00+00:00")
            _event(conn, "deal", i, "C18:NEW", "2026-08-01T00:00:00+00:00",
                   "2026-08-03T00:00:00+00:00", 172800, cat=18)
        _deal(conn, 31, stage="C18:NEW", created="2026-01-01T00:00:00+00:00")
        _event(conn, "deal", 31, "C18:WON", "2026-01-05T00:00:00+00:00", None, None,
               cat=18, seq=0)
        _event(conn, "deal", 31, "C18:NEW", "2026-02-01T00:00:00+00:00", None, None,
               cat=18, seq=1)

    with scoped_session(Scope.everything()) as conn:
        stuck = metrics.stuck_deals(conn, 18)

    assert [row["deal_id"] for row in stuck] == [31], "одна карточка — одна строка"
    # 1 февраля, а не 5 января: дни считаются от входа именно в «Подбор»
    assert stuck[0]["days_in_stage"] < 240


# --------------------------------------------------------------------------
# 8. выгрузка
# --------------------------------------------------------------------------

def test_the_export_is_not_capped_at_the_page_size(mart):
    """Выгрузка отдаёт столько строк, сколько обещает подпись, а не страницу."""
    with analytics_session() as conn:
        for i in range(1, 601):
            _deal(conn, i, created="2026-08-01T00:00:00+00:00")

    with scoped_session(Scope.everything()) as conn:
        page = metrics.entity_table(conn, entity="deal", category_id=18, page_size=10_000)
        export = metrics.entity_table(conn, entity="deal", category_id=18,
                                      page_size=10_000, max_rows=metrics.MAX_EXPORT_ROWS)

    assert len(page["rows"]) == metrics.MAX_PAGE_SIZE, "страница остаётся лёгкой"
    assert len(export["rows"]) == 600, "выгрузка отдаёт всю выборку"


# --------------------------------------------------------------------------
# 9. мёртвый фильтр
# --------------------------------------------------------------------------

def test_the_open_only_chip_filters_leads_too(mart):
    """Чип «только открытые» на вкладке лидов фильтрует, а не просто подсвечен."""
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_lead_status(status_id, name, sort, semantic,"
                     " synced_at) VALUES ('NEW', 'Не обработан', 10, 'in_progress', 'x')")
        conn.execute("INSERT INTO dim_lead_status(status_id, name, sort, semantic,"
                     " synced_at) VALUES ('JUNK', 'Спам', 30, 'lost', 'x')")
        rows = [(1, "NEW", None), (2, "NEW", None), (3, "JUNK", None), (4, "NEW", 77)]
        for lead_id, status, deal_id in rows:
            conn.execute(
                "INSERT INTO fact_lead(lead_id, title, status_id, source_id, assigned_by_id,"
                " date_create, date_modify, opportunity, currency_id, is_converted,"
                " converted_deal_id, is_deleted, synced_at)"
                " VALUES (?, ?, ?, 'CALL', 32, '2026-08-01T00:00:00+00:00',"
                " '2026-08-01T00:00:00+00:00', 0, 'RUB', ?, ?, 0, 'x')",
                (lead_id, f"Лид {lead_id}", status, 1 if deal_id else 0, deal_id),
            )

    with scoped_session(Scope.everything()) as conn:
        every = metrics.entity_table(conn, entity="lead")
        only_open = metrics.entity_table(conn, entity="lead", only_open=True)

    assert every["total"] == 4
    assert only_open["total"] == 2, "спам и дошедший до сделки лид — уже не открытые"


# --------------------------------------------------------------------------
# 10. валюты
# --------------------------------------------------------------------------

def test_dollars_are_not_added_to_roubles(mart):
    """Сделка в другой валюте не попадает в рублёвую сумму, но и не исчезает.

    Курса у витрины нет: она хранит сумму ровно так, как её ввели. Сложить
    десять тысяч долларов с рублями значит напечатать неверное число со знаком
    рубля — поэтому такие сделки считаются отдельно и названы на экране.
    """
    with analytics_session() as conn:
        _deal(conn, 1, stage="C18:WON", amount=1000000, won=1,
              created="2026-08-01T09:00:00+00:00", closed="2026-08-20T09:00:00+00:00")
        conn.execute(
            "INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,"
            " source_id, opportunity, currency_id, date_create, date_modify, closedate,"
            " is_closed, is_won, is_lost, is_deleted, synced_at)"
            " VALUES (5, 'Сделка в долларах', 18, 'C18:WON', 32, 'CALL', 10000, 'USD',"
            " '2026-08-05T09:00:00+00:00', '2026-08-05T09:00:00+00:00',"
            " '2026-08-26T09:00:00+00:00', 1, 1, 0, 0, 'x')"
        )

    period = metrics.resolve_period("custom", "2026-08-01", "2026-08-31")
    with scoped_session(Scope.everything()) as conn:
        wins = metrics.win_rate(conn, 18, period["since"], period["until"])
        quality = metrics.data_quality(conn, 18)

    assert wins["won_amount"] == 1000000, "десять тысяч долларов не стали рублями"
    assert wins["won"] == 2, "но сама сделка из счёта выигранных не пропала"
    assert wins["won_foreign"] == 1
    assert quality["deals"]["foreign_currency"] == 1
