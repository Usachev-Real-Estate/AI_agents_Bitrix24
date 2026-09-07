"""Отдел раскрывается в поимённое выполнение.

Процент отдела отвечает на вопрос «как дела», но не на вопрос «с кем
говорить». 13% отдела — это пятеро по 13% или четверо по нулю и один за
всех; на экране это одно и то же число, а решения за ним стоят разные.

Главное требование к этой таблице — она обязана сходиться со своим же
итогом. Строки, не дающие в сумме факт отдела, проверят один раз и
перестанут верить обеим цифрам. Поэтому в список попадают и те, кого в
плановом составе уже нет: уволенный в середине квартала оставил отделу
настоящие деньги.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import pulse as pulse_module
from schema import analytics_session
from scope import Scope, scoped_session

Q3 = "2026-Q3"
MID = "2026-08-31T12:00:00+03:00"
DEPT = 60


def _user(conn, user_id, name, last_name, active=1):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id,"
        " department_name, is_active, synced_at)"
        " VALUES (?, ?, ?, ?, 'Кретов', ?, 'x')",
        (user_id, name, last_name, DEPT, active),
    )


def _won(conn, deal_id, user_id, amount):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, 18, 'C18:WON', ?, 'CALL', ?, 'RUB', '2026-07-01T00:00:00+00:00',
                '2026-08-10T09:00:00+00:00', '2026-08-10T09:00:00+00:00', 1, 1, 0, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", user_id, amount),
    )


def _norm(conn, user_id, amount):
    conn.execute(
        "INSERT INTO plan_norm(period_code, scope_kind, scope_id, metric,"
        " basis, amount, source, updated_at)"
        " VALUES (?, 'user', ?, 'commission', 'absolute', ?, 'test', 'x')",
        (Q3, user_id, amount),
    )


@pytest.fixture
def department(analytics_db):
    """РОП, двое с нормой, новичок без нормы и уволенный с деньгами."""
    with analytics_session() as conn:
        _user(conn, 1, "Антон Кретов", "Кретов")
        _user(conn, 2, "Первый Брокер", "Первый")
        _user(conn, 3, "Второй Брокер", "Второй")
        _user(conn, 4, "Новичок Безнормы", "Новичков")
        _user(conn, 9, "Ушедший Брокер", "Ушедший", active=0)
        _norm(conn, 2, 4_000_000)
        _norm(conn, 3, 4_000_000)
        _won(conn, 1, 2, 2_000_000)      # 50% нормы
        _won(conn, 2, 3, 400_000)        # 10% нормы
        _won(conn, 3, 4, 700_000)        # новичок
        _won(conn, 4, 1, 300_000)        # РОП
        _won(conn, 5, 9, 600_000)        # уволенный
    return analytics_db


def _brokers():
    with scoped_session(Scope.everything()) as conn:
        result = pulse_module.pulse(conn, Q3, today=MID)
    row = result["departments"][0]
    return row, {man["user_id"]: man for man in row["brokers"]}


# --------------------------------------------------------------------------

def test_the_rows_add_up_to_the_department(department):
    """Сумма по людям равна факту отдела. Иначе таблице незачем верить."""
    row, people = _brokers()

    assert sum(man["fact"] for man in people.values()) == row["fact"]
    assert sum(man["deals"] for man in people.values()) == row["deals"]


def test_completion_is_shown_per_person(department):
    """Тот же процент у отдела складывается из очень разных людей."""
    _, people = _brokers()

    assert people[2]["plan_share"] == 50.0
    assert people[3]["plan_share"] == 10.0
    assert people[2]["ratio"] is not None and people[3]["ratio"] is not None


def test_a_broker_without_a_norm_has_no_percent(department):
    """У новичка процента нет, а деньги есть — ноль соврал бы про обоих."""
    _, people = _brokers()

    assert people[4]["plan"] is None
    assert people[4]["plan_share"] is None
    assert people[4]["fact"] == 700_000


def test_the_departed_broker_is_listed_and_marked(department):
    """Уволенного нет в составе, но его деньги в отделе — значит, он в списке."""
    _, people = _brokers()

    assert people[9]["fact"] == 600_000
    assert people[9]["in_roster"] is False, "строка обязана объяснить, кто это"
    assert people[9]["name"] == "Ушедший Брокер"


def test_the_rop_is_listed_as_a_rop(department):
    """РОП норму не несёт, но сделку закрыл — и это видно."""
    _, people = _brokers()

    assert people[1]["role"] == "rop"
    assert people[1]["plan"] is None
    assert people[1]["fact"] == 300_000


def test_those_who_promised_come_first(department):
    """Сначала люди с нормой по выполнению, потом остальные по деньгам."""
    row, _ = _brokers()
    order = [man["user_id"] for man in row["brokers"]]

    assert order[:2] == [2, 3], "с нормой — сверху вниз по выполнению"
    assert set(order[2:]) == {1, 4, 9}
    rest = [man["fact"] for man in row["brokers"][2:]]
    assert rest == sorted(rest, reverse=True), "остальные — по деньгам"


def test_a_broker_with_a_norm_and_no_deals_is_still_shown(analytics_db):
    """Ноль при живой норме — главная строка этой таблицы, её нельзя прятать."""
    with analytics_session() as conn:
        _user(conn, 1, "Антон Кретов", "Кретов")
        _user(conn, 2, "Молчащий Брокер", "Молчащий")
        _norm(conn, 2, 4_000_000)

    _, people = _brokers()

    assert people[2]["fact"] == 0
    assert people[2]["plan_share"] == 0.0
    assert people[2]["deals"] == 0
