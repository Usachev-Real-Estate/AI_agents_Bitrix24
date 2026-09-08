"""Разрез по источнику: канал судят по одной совокупности.

Вопрос, ради которого разрез существует, звучит так: «реклама стоит
1,48 млн в месяц — она окупается?». Ответ на него складывается из двух
чисел, и оба легко испортить.

Первое — сколько канал ПРИВЁЛ. Считать по закрытым за период значит
сравнивать канал, включённый в марте, с включённым в августе: у первого
сделки успели дойти до конца, у второго нет, и второй выглядит хуже при
том же трафике. Поэтому когорта — по дате СОЗДАНИЯ.

Второе — сколько канал ПРИНЁС. Делить комиссию на выигранные сделки
нельзя: канал с одной сделкой из ста показал бы прекрасный средний чек.
Делить надо на все приведённые — это и есть цена лида в деньгах.

Отдельно проверяется, что в воронке продавцов денег нет вовсе. Там
комиссию в карточке не ведут, и ноль в колонке сумм читается как «канал не
принёс ничего» — утверждение, которого никто не делал.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import metrics
from schema import analytics_session
from scope import Scope, scoped_session

BUYERS = 18
SELLERS = 0
SINCE = "2026-07-01T00:00:00+03:00"
UNTIL = "2026-10-01T00:00:00+03:00"


def _deal(conn, deal_id, source, *, category=BUYERS, created="2026-07-05T09:00:00+00:00",
          stage="C18:NEW", won=0, lost=0, amount=0, closed=None, currency="RUB"):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, ?, ?, 32, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", category, stage, source, amount, currency,
         created, created, closed, 1 if (won or lost) else 0, won, lost),
    )


def _event(conn, entity_id, stage, entered, left, duration, cat=BUYERS, seq=0):
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id,"
        " entered_at, left_at, duration_sec, seq) VALUES ('deal', ?, ?, ?, ?, ?, ?, ?)",
        (entity_id, cat, stage, entered, left, duration, seq),
    )


@pytest.fixture
def agency(analytics_db):
    """Два канала: дорогой приводит много и не доводит, дешёвый — наоборот."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (18, 'Покупатели', 1, 10, 'x'), (0, 'Продавцы', 1, 20, 'x')"
        )
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name,"
            " is_active, synced_at) VALUES (32, 'Иван Петров', 44, 'Кретов', 1, 'x')"
        )
        conn.execute(
            "INSERT INTO dim_source(source_id, name, synced_at)"
            " VALUES ('ADV', 'Реклама', 'x'), ('CALL', 'Звонок', 'x')"
        )
        # Реклама: четыре сделки, одна выиграна на 500 тыс.
        _deal(conn, 601, "ADV", won=1, amount=500000,
              closed="2026-08-20T09:00:00+00:00")
        for deal_id in (602, 603, 604):
            _deal(conn, deal_id, "ADV")
        # Звонок: две сделки, одна выиграна на 900 тыс., одна проиграна.
        _deal(conn, 611, "CALL", won=1, amount=900000,
              closed="2026-08-25T09:00:00+00:00")
        _deal(conn, 612, "CALL", lost=1, amount=100000,
              closed="2026-08-26T09:00:00+00:00")
    return analytics_db


def _sources(category=BUYERS, since=SINCE, until=UNTIL):
    with scoped_session(Scope.everything()) as conn:
        return metrics.deal_sources(conn, category, since, until)


def _by_name(data, name):
    return next(row for row in data["rows"] if row["name"] == name)


def test_the_cohort_follows_the_creation_date(agency):
    """Сделка, созданная до окна, в когорту не входит — даже выигранная в нём."""
    with analytics_session() as conn:
        _deal(conn, 620, "ADV", created="2026-05-01T09:00:00+00:00", won=1,
              amount=3000000, closed="2026-08-30T09:00:00+00:00")

    adv = _by_name(_sources(), "Реклама")
    assert adv["deals"] == 4
    assert adv["won"] == 1
    assert adv["won_amount"] == 500000


def test_money_is_divided_by_every_deal_the_channel_brought(agency):
    """«На сделку» — комиссия на все приведённые, а не на выигранные."""
    data = _sources()
    adv, call = _by_name(data, "Реклама"), _by_name(data, "Звонок")

    # Средний чек выигранной у рекламы — те же 500 тыс., что и вся её
    # комиссия. Число, по которому принимают решение, вчетверо меньше.
    assert adv["amount_per_deal"] == 125000
    assert call["amount_per_deal"] == 450000
    assert adv["conversion"] == 25.0
    assert call["conversion"] == 50.0


def test_the_biggest_channel_stands_first(agency):
    """Крупные каналы вверх: хвост из одной сделки читают последним."""
    assert [row["name"] for row in _sources()["rows"]] == ["Реклама", "Звонок"]


def test_the_total_adds_up_from_the_rows(agency):
    total = _sources()["total"]
    assert total["deals"] == 6
    assert total["won"] == 2
    assert total["lost"] == 1
    assert total["won_amount"] == 1400000


def test_a_deal_without_a_source_is_named_not_hidden(agency):
    """Пустой источник — своя строка. Молча вычесть её значит потерять сделки."""
    with analytics_session() as conn:
        _deal(conn, 630, "")

    data = _sources()
    assert _by_name(data, "Не указан")["deals"] == 1
    assert data["total"]["deals"] == 7


def test_a_foreign_currency_deal_is_counted_but_not_summed(agency):
    """Сделку в долларах в рублёвую сумму не кладут: курса у витрины нет."""
    with analytics_session() as conn:
        _deal(conn, 640, "CALL", won=1, amount=10000, currency="USD",
              closed="2026-08-27T09:00:00+00:00")

    call = _by_name(_sources(), "Звонок")
    assert call["won"] == 2
    assert call["won_amount"] == 900000
    assert call["won_foreign"] == 1


def test_the_sellers_funnel_carries_no_money(agency):
    """У продавцов комиссии нет — колонки сумм не пустеют, а исчезают."""
    with analytics_session() as conn:
        _deal(conn, 650, "ADV", category=SELLERS, stage="NEW")

    data = _sources(category=SELLERS)
    assert data["with_money"] is False
    row = _by_name(data, "Реклама")
    assert row["deals"] == 1
    assert "won_amount" not in row
    assert "amount_per_deal" not in row
    assert "won_amount" not in data["total"]


def test_a_window_shorter_than_the_cycle_says_so(agency):
    """Окно короче цикла занижает выигранных у всех каналов сразу."""
    short = _sources(since="2026-07-01T00:00:00+03:00",
                     until="2026-07-08T00:00:00+03:00")
    assert short["matured"] is False

    # Цикл в фикстуре — 46 дней от создания до закрытия, окно квартала длиннее.
    full = _sources()
    assert full["cycle_days"] and full["matured"] is True


def test_a_stalled_card_is_counted_against_its_own_channel(agency):
    """«Зависло» разбирается по источникам той же когорты, а не по всем открытым."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
            " synced_at) VALUES ('C18:NEW', 18, 'Подбор', 10, 'in_progress', 'x')"
        )
        # Норма стадии: три завершённых интервала по два дня.
        for deal_id in (701, 702, 703):
            _deal(conn, deal_id, "CALL")
            _event(conn, deal_id, "C18:NEW", "2026-07-01T00:00:00+00:00",
                   "2026-07-03T00:00:00+00:00", 172800)
        # Две сделки рекламы стоят на той же стадии с самого создания.
        for deal_id in (602, 603):
            _event(conn, deal_id, "C18:NEW", "2026-07-05T09:00:00+00:00", None, None)

    data = _sources()
    assert _by_name(data, "Реклама")["stalled"] == 2
    assert _by_name(data, "Звонок")["stalled"] == 0
    assert data["total"]["stalled"] == 2


def test_a_stalled_card_outside_the_window_stays_outside(agency):
    """Строка описывает одну совокупность: снимок всех открытых в неё не входит."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
            " synced_at) VALUES ('C18:NEW', 18, 'Подбор', 10, 'in_progress', 'x')"
        )
        for deal_id in (701, 702, 703):
            _deal(conn, deal_id, "CALL")
            _event(conn, deal_id, "C18:NEW", "2026-07-01T00:00:00+00:00",
                   "2026-07-03T00:00:00+00:00", 172800)
        # Сделка рекламы создана до окна и стоит с тех пор.
        _deal(conn, 710, "ADV", created="2026-05-01T09:00:00+00:00")
        _event(conn, 710, "C18:NEW", "2026-05-01T09:00:00+00:00", None, None)

    assert _by_name(_sources(), "Реклама")["stalled"] == 0
