"""Разговор, случившийся до конвертации лида, — часть истории человека.

Книга читала дела только по сделкам и контактам. До конвертации звонки
висят на лиде и после неё там же и остаются, так что клиент, с которым
разговаривали трижды, пока он был лидом, выглядел не звонившим ни разу.

Замер боевой витрины: 7 326 дел на лидах — почти четверть портала. Из них
1 936 звонков принадлежат людям, уже ставшим клиентами, и **497 таких
клиентов книга считала молчащими**. Цена ошибки не в числе: молчащий
клиент, которому звонили, получает неверное состояние, пустой разбор и не
то место в очереди у РОПа.

Здесь закреплены обе половины — что лиды и их дела приезжают из витрины, и
что событие лида находит своего клиента, — плюс решение, которого в ТЗ не
было: сделка решает раньше контакта.
"""

from datetime import datetime, timedelta, timezone

import pytest

from analytics.schema import analytics_session
from clients import mart
from clients.events import build_events
from clients.keys import Card
from clients.mart import Portfolio
from clients.schema import EVENT_CALL, OWNER_CONTACT, OWNER_DEAL, OWNER_LEAD

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
SYNCED = NOW.isoformat()


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


# ── витрина отдаёт лиды и их дела ────────────────────────────────────

def _deal(conn, deal_id, *, contact_id=77):
    conn.execute(
        "INSERT INTO fact_deal(deal_id, title, category_id, stage_id,"
        " assigned_by_id, contact_id, date_create, is_deleted, synced_at)"
        " VALUES (?, 'сделка', 18, 'C18:NEW', 10, ?, ?, 0, ?)",
        (deal_id, contact_id, _at(30), SYNCED),
    )


def _lead(conn, lead_id, *, deal_id=None, contact_id=None, deleted=0):
    conn.execute(
        "INSERT INTO fact_lead(lead_id, title, status_id, converted_deal_id,"
        " contact_id, date_create, is_converted, is_deleted, synced_at)"
        " VALUES (?, 'лид', 'NEW', ?, ?, ?, ?, ?, ?)",
        (lead_id, deal_id, contact_id, _at(90), int(bool(deal_id)), deleted, SYNCED),
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
        _deal(conn, 7, contact_id=77)
        _lead(conn, 100, deal_id=7)            # превратился в нашу сделку
        _lead(conn, 101, contact_id=77)        # конвертирован не к нам, контакт наш
        _lead(conn, 102, deal_id=999)          # чужая сделка
        _lead(conn, 103, deal_id=7, deleted=1)  # удалённый
        for number, lead_id in enumerate((100, 101, 102, 103), start=900):
            _activity(conn, number, mart.OWNER_TYPE_LEAD, lead_id)
    return analytics_db


def test_a_lead_is_found_both_by_its_deal_and_by_its_contact(mart_db):
    """Два пути к одному человеку: через сделку и через контакт."""
    with analytics_session(readonly=True) as conn:
        found = mart.read_leads(conn, [7], [77])

    assert set(found) == {100, 101}, "чужая сделка и удалённый лид сюда не входят"


def test_a_lead_found_by_both_paths_is_read_once(analytics_db):
    """Иначе его звонки легли бы в ленту дважды.

    Лид, который и конвертирован в нашу сделку, и держит наш контакт,
    приезжает двумя запросами. Сведение по ключу словаря — единственное,
    что стоит между этим и удвоенной историей клиента.
    """
    with analytics_session() as conn:
        _deal(conn, 7, contact_id=77)
        _lead(conn, 100, deal_id=7, contact_id=77)

    with analytics_session(readonly=True) as conn:
        found = mart.read_leads(conn, [7], [77])

    assert list(found) == [100]


def test_the_deleted_lead_stays_deleted(mart_db):
    with analytics_session(readonly=True) as conn:
        assert 103 not in mart.read_leads(conn, [7], [77])


def test_the_activities_of_a_lead_come_out_of_the_mart(mart_db):
    """Дела лида читаются тем же запросом, что дела сделок и контактов."""
    with analytics_session(readonly=True) as conn:
        rows = mart.read_activities(conn, [7], [77], [100, 101])

    assert {int(row["activity_id"]) for row in rows} == {900, 901}


def test_the_whole_portfolio_carries_the_lead_activities(mart_db):
    """Проводка целиком, а не по частям.

    Чтение лидов и чтение их дел проверены выше поштучно, и оба работали
    бы, даже если сборка портфеля перестала бы передавать одно другому.
    Стоило бы это ровно того, ради чего всё делалось: 497 клиентов молча
    вернулись бы в молчащие, и никакой тест этого не заметил.
    """
    with analytics_session(readonly=True) as conn:
        portfolio = mart.read_portfolio(conn, [18])

    assert set(portfolio.leads) == {100, 101}
    assert {int(row["activity_id"]) for row in portfolio.activities} == {900, 901}


def test_without_lead_ids_nothing_changes(mart_db):
    """Умолчание пустое: вызов без лидов ведёт себя как прежде."""
    with analytics_session(readonly=True) as conn:
        assert mart.read_activities(conn, [7], [77]) == []


# ── событие лида находит своего клиента ──────────────────────────────

KEY = "p:+79001112233"
OTHER = "p:+79004445566"


def _portfolio(leads, activities):
    return Portfolio(
        cards=(Card(7, "2-к", "C18:NEW", 77),),
        deals={7: {"deal_id": 7, "contact_id": 77, "date_create": _at(60)}},
        leads=leads,
        promises={},
        comments=(),
        activities=activities,
        moves=(),
        users={},
        stages={},
        mart_full_sync_at=_at(0.4),
    )


def _row(activity_id, owner_type_id, owner_id):
    return {
        "activity_id": activity_id, "owner_type_id": owner_type_id,
        "owner_id": owner_id, "provider_type_id": "CALL", "direction": 1,
        "subject": "Входящий", "description": "", "author_id": 10,
        "responsible_id": 10, "created_at": _at(2), "start_time": _at(2),
        "end_time": _at(2), "deadline": None, "completed": 1,
    }


def _feed(leads, rows, *, by_deal=None, by_contact=None):
    return build_events(
        _portfolio(leads, tuple(rows)),
        by_deal if by_deal is not None else {7: KEY},
        by_contact if by_contact is not None else {77: KEY},
    )


def test_a_call_on_a_converted_lead_reaches_the_client():
    """Ровно тот случай, ради которого всё это: 497 молчащих клиентов."""
    feed = _feed({100: {"converted_deal_id": 7, "contact_id": None}},
                 [_row(900, mart.OWNER_TYPE_LEAD, 100)])

    assert [event.client_key for event in feed] == [KEY]
    assert feed[0].kind == EVENT_CALL
    assert feed[0].entity_type == OWNER_LEAD
    assert feed[0].entity_id == 100, "событие помнит лид, а не сделку"


def test_a_lead_that_only_shares_the_contact_still_counts():
    """Конвертировали в другую воронку — человек от этого не изменился."""
    feed = _feed({101: {"converted_deal_id": None, "contact_id": 77}},
                 [_row(901, mart.OWNER_TYPE_LEAD, 101)])

    assert [event.client_key for event in feed] == [KEY]


def test_the_deal_decides_before_the_contact():
    """Контакт бывает общим, сделка — нет.

    Агентский или служебный контакт ведёт к тому клиенту, который на нём
    оказался первым, и звонок уехал бы к чужому человеку. Сделка такой
    двусмысленности не знает: она одна, и портал сам проставил на неё
    ссылку при конвертации.
    """
    feed = _feed({100: {"converted_deal_id": 7, "contact_id": 77}},
                 [_row(900, mart.OWNER_TYPE_LEAD, 100)],
                 by_deal={7: KEY}, by_contact={77: OTHER})

    assert [event.client_key for event in feed] == [KEY]


def test_a_lead_whose_deal_is_not_ours_falls_back_to_the_contact():
    """Сделка чужая, контакт наш — человек всё ещё наш."""
    feed = _feed({102: {"converted_deal_id": 999, "contact_id": 77}},
                 [_row(902, mart.OWNER_TYPE_LEAD, 102)])

    assert [event.client_key for event in feed] == [KEY]


def test_a_lead_nobody_knows_brings_nothing():
    """Лида нет в справочнике — дело брать некуда, и гадать нельзя."""
    assert _feed({}, [_row(900, mart.OWNER_TYPE_LEAD, 100)]) == []


def test_a_foreign_lead_does_not_reach_us():
    """Ни сделка, ни контакт не наши — событие чужое."""
    feed = _feed({102: {"converted_deal_id": 999, "contact_id": 555}},
                 [_row(902, mart.OWNER_TYPE_LEAD, 102)])

    assert feed == []


def test_deals_and_contacts_keep_working_as_before():
    """Третий путь не должен был тронуть два прежних."""
    feed = _feed({}, [_row(910, mart.OWNER_TYPE_DEAL, 7),
                      _row(911, mart.OWNER_TYPE_CONTACT, 77)])

    assert {event.entity_type for event in feed} == {OWNER_DEAL, OWNER_CONTACT}
