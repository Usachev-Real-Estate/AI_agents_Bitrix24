"""Портфель читается из витрины целиком и ничего по дороге не теряет.

Витрина — единственный источник карточек, и всё, что из неё не приехало,
для клиентского слоя не существует. Поэтому тут проверяется не «запрос
работает», а те три места, где запрос может молча отдать не всё: отсечка по
дате, владелец дела и размер пачки.
"""

from datetime import datetime, timedelta, timezone

import pytest

from analytics.schema import analytics_session
from clients import mart

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
SYNCED = NOW.isoformat()


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _deal(conn, deal_id, *, category_id=18, contact_id=77, created=None, deleted=0):
    conn.execute(
        "INSERT INTO fact_deal(deal_id, title, category_id, stage_id,"
        " assigned_by_id, contact_id, date_create, is_deleted, synced_at)"
        " VALUES (?, ?, ?, 'C18:NEW', 10, ?, ?, ?, ?)",
        (deal_id, f"сделка {deal_id}", category_id, contact_id,
         created or _at(30), deleted, SYNCED),
    )


def _activity(conn, activity_id, owner_type_id, owner_id):
    conn.execute(
        "INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,"
        " provider_type_id, created_at, synced_at) VALUES (?, ?, ?, 'CALL', ?, ?)",
        (activity_id, owner_type_id, owner_id, _at(1), SYNCED),
    )


@pytest.fixture
def mart_db(analytics_db):
    with analytics_session() as conn:
        _deal(conn, 7)
        _deal(conn, 8, category_id=0, contact_id=88)
        _deal(conn, 9, category_id=26, contact_id=99)
        _deal(conn, 10, deleted=1, contact_id=111)
    return analytics_db


def test_only_the_wanted_funnels_come_back(mart_db):
    """Воронки задаёт вызывающий, а не витрина: «Общая база» сюда не входит."""
    with analytics_session(readonly=True) as conn:
        deals = mart.read_deals(conn, (0, 18))

    assert sorted(deals) == [7, 8]


def test_a_deleted_deal_does_not_come_back(mart_db):
    """Удалённая в CRM сделка клиента не составляет."""
    with analytics_session(readonly=True) as conn:
        assert 10 not in mart.read_deals(conn, (0, 18))


def test_a_long_running_old_deal_is_still_in_the_portfolio(mart_db):
    """Сделка, заведённая до окна витрины, но живая, обязана остаться.

    Соблазн отсечь портфель по `date_create` велик и неверен: витрина и так
    грузит только окно, а вторая отсечка спрятала бы клиента, с которым
    работают четырнадцатый месяц. Это не старьё, это работа.
    """
    with analytics_session() as conn:
        _deal(conn, 11, created=_at(420))

    with analytics_session(readonly=True) as conn:
        deals = mart.read_deals(conn, (0, 18))

    assert 11 in deals, "старая живая сделка не должна выпадать из портфеля"


def test_activities_come_from_both_the_deal_and_its_contact(mart_db):
    """Дела берутся по сделке И по её контакту — иначе теряется большинство.

    Замер портала, записанный в work.py: 13 277 дел на контактах против
    7 131 на сделках.
    """
    with analytics_session() as conn:
        _activity(conn, 901, mart.OWNER_TYPE_DEAL, 7)
        _activity(conn, 902, mart.OWNER_TYPE_CONTACT, 77)
        _activity(conn, 903, mart.OWNER_TYPE_CONTACT, 999)

    with analytics_session(readonly=True) as conn:
        rows = mart.read_activities(conn, [7], [77])

    assert sorted(row["activity_id"] for row in rows) == [901, 902]


def test_a_portfolio_bigger_than_one_query_still_comes_whole(analytics_db, monkeypatch):
    """Портфель, не влезающий в один запрос, приезжает целиком.

    У SQLite есть потолок числа параметров, и портфель однажды в него
    упрётся — упрётся ночью на боевом прогоне, а не здесь. Пачка сбита до
    трёх нарочно: с боевыми пятьюстами тест на четырёх сделках не проверял
    бы ничего.
    """
    monkeypatch.setattr(mart, "CHUNK", 3)
    with analytics_session() as conn:
        for deal_id in range(1, 11):
            _deal(conn, deal_id, contact_id=None)
            _activity(conn, 900 + deal_id, mart.OWNER_TYPE_DEAL, deal_id)

    with analytics_session(readonly=True) as conn:
        rows = mart.read_activities(conn, list(range(1, 11)), [])

    assert len(rows) == 10, "пачки обязаны склеиться без потерь и без повторов"


def test_the_stage_name_is_looked_up_by_funnel_too(analytics_db):
    """Один и тот же stage_id в разных воронках — разные стадии."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_stage(stage_id, category_id, name, synced_at) VALUES"
            " ('NEW', 18, 'Подбор', ?), ('NEW', 0, 'Назначение встречи', ?)",
            (SYNCED, SYNCED),
        )

    with analytics_session(readonly=True) as conn:
        stages = mart.read_stages(conn)

    assert stages[("NEW", 18)] == "Подбор"
    assert stages[("NEW", 0)] == "Назначение встречи"


def test_the_last_full_sync_is_the_last_successful_full_one(analytics_db):
    """Отметка полной сверки не берётся ни у инкремента, ни у падения.

    По ней читается, насколько полна история закрытых сделок: их
    комментарии обновляет только полная сверка.
    """
    with analytics_session() as conn:
        for kind, finished, status in (
            ("full", _at(3), "ok"),
            ("full", _at(1), "error"),
            ("incremental", _at(0.1), "ok"),
        ):
            conn.execute(
                "INSERT INTO etl_run(kind, started_at, finished_at, status)"
                " VALUES (?, ?, ?, ?)", (kind, finished, finished, status),
            )

    with analytics_session(readonly=True) as conn:
        assert mart.last_full_sync(conn) == _at(3)


def test_a_mart_that_never_synced_says_so(analytics_db):
    """Пустой журнал прогонов — это None, а не выдуманная дата."""
    with analytics_session(readonly=True) as conn:
        assert mart.last_full_sync(conn) is None


def test_the_portfolio_names_the_contacts_of_its_own_cards(mart_db):
    """Контакты портфеля — те, что стоят на его карточках, и только они."""
    with analytics_session(readonly=True) as conn:
        portfolio = mart.read_portfolio(conn, (0, 18))

    assert portfolio.contact_ids == (77, 88)
    assert [card.deal_id for card in portfolio.cards] == [7, 8]


def test_the_mart_is_read_without_the_right_to_write(mart_db):
    """Весь портфель читается соединением, которое не умеет писать.

    У витрины один писатель — ETL. Случайная запись отсюда стоила бы
    «database is locked» обеим сторонам, и цена уже известна.
    """
    with analytics_session(readonly=True) as conn:
        portfolio = mart.read_portfolio(conn, (0, 18))

    assert len(portfolio.deals) == 2
