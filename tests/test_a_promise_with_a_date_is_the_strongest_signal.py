"""Брокер написал, что сделает, и не сделал.

Самый сильный сигнал из всех, что есть в витрине, и сильный он не
величиной, а происхождением: не мы решили, что должно было произойти, — так
написал сам брокер. «Позвонить в пятницу» — пятница прошла, записи нет,
разговор короткий и спорить не о чем.

На живых данных четыре таких обещания оказались у одного человека, все
датированы одним днём и все по одному дому: «позвонить в пятницу»,
«согласовать просмотр на среду в 16:30», «согласовать просмотр на среду в
16:00», «позвонить в пятницу». Это не четыре забытые карточки, а один
брошенный день работы — и совет обязан собрать их в один разговор, а не
разложить на четыре.

Главное здесь, однако, не обвинение, а защита от него. «Договорились
созвониться в конце осени» — карточка молчит сорок дней, и так и надо.
Отчёт, упрекнувший за это, теряет доверие целиком, а вместе с ним теряют
силу и те его строки, которые верны: ровно так вышло со встречами, которые
в агентстве не заводят.
"""

from datetime import date, timedelta

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import advice_rules
import work
from schema import analytics_session
from scope import Scope, scoped_session

SELLERS = 0
TODAY = date(2026, 9, 9)
PAST = (TODAY - timedelta(days=30)).isoformat()
SOON = (TODAY + timedelta(days=60)).isoformat()


def _deal(conn, deal_id, *, user=10, title=None):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, contact_id, is_deleted, synced_at)
        VALUES (?, ?, 0, 'UC_FADPBF', ?, 'ADV', 0, 'RUB',
                '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00',
                NULL, 0, 0, 0, 5000, 0, 'x')
        """,
        (deal_id, title or f"Воробьевы горы {deal_id}", user),
    )


def _read(conn, deal_id, *, promised="Позвонить в пятницу, согласовать фотосессию",
          promised_at=PAST, wait_until=None, terms="", refused=0):
    conn.execute(
        """
        INSERT INTO fact_comment_read(entity_type, entity_id, source_hash,
            promised, promised_at, wait_until, refused, refused_why, ready,
            terms, read_at)
        VALUES ('deal', ?, 'h', ?, ?, ?, ?, '', '', ?, 'x')
        """,
        (deal_id, promised, promised_at, wait_until, refused, terms),
    )


@pytest.fixture
def agency(analytics_db):
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (0, 'Продавцы', 1, 10, 'x')"
        )
        conn.execute(
            "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
            " synced_at) VALUES ('UC_FADPBF', 0, 'Поиск клиента', 50,"
            " 'in_progress', 'x')"
        )
        for user_id, name in ((10, "Марат Абзалилов"), (11, "Ольга Лобанова")):
            conn.execute(
                "INSERT INTO dim_user(user_id, name, department_id, department_name,"
                " is_active, synced_at) VALUES (?, ?, 44, 'Кретов', 1, 'x')",
                (user_id, name),
            )
    return analytics_db


def _promises():
    with scoped_session(Scope.everything()) as conn:
        return work.promises(conn, [SELLERS], today=TODAY.isoformat())


# --------------------------------------------------------------------------
# защита от ложного обвинения

def test_an_agreed_wait_is_not_a_broken_promise(agency):
    """«Созвонимся в конце осени» — карточка молчит законно.

    Отчёт, упрекнувший за правильную работу, теряет доверие целиком.
    """
    with analytics_session() as conn:
        _deal(conn, 1, title="Терентьева Елена")
        _read(conn, 1, promised="Созвониться", promised_at=PAST, wait_until=SOON)

    assert _promises() == []


def test_a_wait_that_has_itself_expired_counts_again(agency):
    """Срок ожидания прошёл — молчание перестало быть договорённым."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _read(conn, 1, promised_at=PAST,
              wait_until=(TODAY - timedelta(days=5)).isoformat())

    assert len(_promises()) == 1


def test_a_promise_still_ahead_is_not_overdue(agency):
    """Срок не наступил — спрашивать не о чем."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _read(conn, 1, promised_at=SOON)

    assert _promises() == []


def test_a_card_without_a_promise_is_not_dragged_in(agency):
    """Пустое обещание — это отсутствие обещания, а не обещание пустоты."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _read(conn, 1, promised="", promised_at=PAST)

    assert _promises() == []


def test_a_closed_card_keeps_its_promises_to_itself(agency):
    """По закрытой сделке спрашивать нечего."""
    with analytics_session() as conn:
        _deal(conn, 1)
        conn.execute("UPDATE fact_deal SET is_closed = 1 WHERE deal_id = 1")
        _read(conn, 1)

    assert _promises() == []


# --------------------------------------------------------------------------
# сам сигнал

def test_the_oldest_promise_comes_first(agency):
    """Разговор начинают с того, что просрочено дольше всех."""
    with analytics_session() as conn:
        _deal(conn, 1, title="ВГ 747")
        _read(conn, 1, promised_at=(TODAY - timedelta(days=5)).isoformat())
        _deal(conn, 2, title="ВГ 762")
        _read(conn, 2, promised_at=(TODAY - timedelta(days=40)).isoformat())

    assert [row["title"] for row in _promises()] == ["ВГ 762", "ВГ 747"]


def test_four_promises_of_one_broker_are_one_conversation(agency):
    """Один брошенный день работы — один разговор, а не четыре совета.

    На живых данных все четыре просроченных обещания оказались у одного
    человека, за один день и по одному дому.
    """
    with analytics_session() as conn:
        for deal_id, title in ((1, "ВГ 747"), (2, "ВГ 762"),
                               (3, "ВГ 727"), (4, "ВГ 896")):
            _deal(conn, deal_id, title=title, user=10)
            _read(conn, deal_id)

    advices = advice_rules.promise_overdue(_promises())

    assert len(advices) == 1
    assert advices[0].value == 4
    assert "4 обещания" in advices[0].title
    assert "ВГ 747" in advices[0].action and "и ещё 1" in advices[0].action


def test_two_brokers_are_two_conversations(agency):
    """Разговор с каждым свой: ключ совета — человек."""
    with analytics_session() as conn:
        _deal(conn, 1, user=10)
        _read(conn, 1)
        _deal(conn, 2, user=11)
        _read(conn, 2)

    assert len(advice_rules.promise_overdue(_promises())) == 2


def test_the_advice_quotes_the_broker_and_the_terms(agency):
    """Совет цитирует брокера: спорить со своими же словами не выйдет."""
    with analytics_session() as conn:
        _deal(conn, 1, title="ВГ 747")
        _read(conn, 1, promised="Позвонить в пятницу, согласовать фотосессию",
              terms="комиссия 3%")

    proof = advice_rules.promise_overdue(_promises())[0].proof

    assert "Позвонить в пятницу" in proof
    assert "комиссия 3%" in proof
    assert "30 дн назад" in proof
    assert "10.08" in proof, "дата по-человечески, а не ISO"


def test_a_single_promise_names_its_card(agency):
    """Одно обещание — один вопрос, и карточка названа прямо в действии."""
    with analytics_session() as conn:
        _deal(conn, 1, title="Воробьевы горы 747")
        _read(conn, 1)

    action = advice_rules.promise_overdue(_promises())[0].action

    assert "«Воробьевы горы 747»" in action
    assert "Разберите вместе" not in action
