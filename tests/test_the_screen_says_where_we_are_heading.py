"""Рубеж безубыточности и деньги, переставшие двигаться.

План агентства — намеренная планка: норма поставлена высоко, чтобы к ней
тянулись. Следствие видно в первой же сводке — выполнение у всех отделов
лежит в диапазоне 0–15% и красное всегда. Число, не меняющее цвет от
работы, перестают читать через месяц, а вместе с ним перестают читать и
остальной экран.

Поэтому рядом с планкой стоит второй рубеж — безубыточность. Он отвечает не
«к чему тянемся», а «доживём ли», и движется от каждой сделки.

Второй вопрос собственника — «куда поднажать». Ответ на него не отдел с
худшим процентом: процент говорит о том, что уже случилось. Отвечают
деньги, стоящие на сделках, которые перестали двигаться, — с ними можно
что-то сделать сегодня.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import plans
import pulse as pulse_module
from schema import analytics_session
from scope import Scope, scoped_session

Q3 = "2026-Q3"
MID = "2026-08-31T12:00:00+03:00"
DEPT = 60
LONG_AGO = "2026-07-01T00:00:00+00:00"

COSTS = "5565208"
SHARE = "0.586"


@pytest.fixture
def costs(monkeypatch):
    """Расходы и доля, остающаяся компании, — из отчёта руководству."""
    from config import get_settings

    monkeypatch.setenv("PULSE_MONTHLY_COSTS", COSTS)
    monkeypatch.setenv("PULSE_NET_SHARE", SHARE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _user(conn, user_id, name, last_name, dept=DEPT, dept_name="Кретов"):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id,"
        " department_name, is_active, synced_at) VALUES (?, ?, ?, ?, ?, 1, 'x')",
        (user_id, name, last_name, dept, dept_name),
    )


def _deal(conn, deal_id, user_id, amount, *, won=1, stage="C18:WON",
          closed="2026-08-10T09:00:00+00:00"):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, 18, ?, ?, 'CALL', ?, 'RUB', ?, ?, ?, ?, ?, 0, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", stage, user_id, amount, LONG_AGO,
         LONG_AGO, closed if won else None, won, won),
    )


def _event(conn, deal_id, entered, left, duration):
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id,"
        " entered_at, left_at, duration_sec, seq)"
        " VALUES ('deal', ?, 18, 'C18:NEW', ?, ?, ?, 0)",
        (deal_id, entered, left, duration),
    )


def _norm(conn, user_id, amount):
    conn.execute(
        "INSERT INTO plan_norm(period_code, scope_kind, scope_id, metric,"
        " basis, amount, source, updated_at)"
        " VALUES (?, 'user', ?, 'commission', 'absolute', ?, 'test', 'x')",
        (Q3, user_id, amount),
    )


@pytest.fixture
def agency(analytics_db):
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort,"
                     " synced_at) VALUES (18, 'Покупатели', 1, 10, 'x')")
        conn.execute("INSERT INTO dim_stage(stage_id, category_id, name, sort,"
                     " semantic, synced_at)"
                     " VALUES ('C18:NEW', 18, 'Подбор', 10, 'in_progress', 'x'),"
                     "        ('C18:WON', 18, 'Успех', 90, 'won', 'x')")
        _user(conn, 1, "Антон Кретов", "Кретов")
        _user(conn, 2, "Иван Броков", "Броков")
        _norm(conn, 2, 4_500_000)
        _deal(conn, 1, 2, 5_000_000)
    return analytics_db


def _pulse(with_stuck=False):
    with scoped_session(Scope.everything()) as conn:
        return pulse_module.pulse(conn, Q3, today=MID, with_stuck=with_stuck)


# --------------------------------------------------------------------------
# рубеж
# --------------------------------------------------------------------------

def test_without_costs_there_is_no_edge(agency):
    """Придуманный порог хуже отсутствующего: по нему решают судьбу людей."""
    assert _pulse()["breakeven"] is None


def test_the_edge_is_three_months_of_costs(agency, costs):
    """Квартал — это три месяца расходов, а не 90 дней, делённые на 30."""
    edge = _pulse()["breakeven"]

    assert edge["months"] == 3
    assert edge["gross"] == round(5_565_208 * 3 / 0.586, 0)
    assert edge["share"] == round(100 * 5_000_000 / edge["gross"], 1)


@pytest.mark.parametrize("since,until,expected", [
    ("2026-07-01T00:00:00+03:00", "2026-10-01T00:00:00+03:00", 3),
    ("2026-01-01T00:00:00+03:00", "2027-01-01T00:00:00+03:00", 12),
    ("2026-02-01T00:00:00+03:00", "2026-03-01T00:00:00+03:00", 1),
])
def test_months_are_counted_not_divided(since, until, expected):
    """В квартале бывает 90 дней и 92, а расходов всегда три месяца."""
    assert plans.months_in(since, until) == expected


def test_the_verdict_says_we_will_not_make_it(agency, costs):
    """Главный вопрос собственника: идём к нулю или под него."""
    edge = _pulse()["breakeven"]

    assert edge["reaches"] is False
    assert edge["gap"] < 0, "минус — это сколько не хватит"
    assert edge["projected_share"] is not None


def test_the_verdict_says_we_will(analytics_db, costs):
    """Тот же расчёт обязан уметь сказать и «пройдём», иначе он не расчёт."""
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort,"
                     " synced_at) VALUES (18, 'Покупатели', 1, 10, 'x')")
        _user(conn, 1, "Антон Кретов", "Кретов")
        _user(conn, 2, "Иван Броков", "Броков")
        _norm(conn, 2, 4_500_000)
        _deal(conn, 1, 2, 40_000_000)

    edge = _pulse()["breakeven"]

    assert edge["reaches"] is True
    assert edge["gap"] > 0, "плюс — это запас"


# --------------------------------------------------------------------------
# где стоят деньги
# --------------------------------------------------------------------------

@pytest.fixture
def frozen(agency):
    """Три сделки прошли стадию за два дня, четвёртая стоит с июля."""
    with analytics_session() as conn:
        for deal_id in (11, 12, 13):
            _deal(conn, deal_id, 2, 0, won=0, stage="C18:NEW")
            _event(conn, deal_id, "2026-08-01T00:00:00+00:00",
                   "2026-08-03T00:00:00+00:00", 172_800)
        _deal(conn, 20, 2, 3_000_000, won=0, stage="C18:NEW")
        _event(conn, 20, LONG_AGO, None, None)
    return agency


def test_stuck_money_is_off_unless_asked(frozen):
    """Дайджест собирает «Пульс» шесть раз — перебор стадий там не нужен."""
    assert _pulse()["stuck"] is None


def test_stuck_money_answers_where_to_push(frozen):
    """Не худший процент, а деньги, которые перестали двигаться."""
    result = _pulse(with_stuck=True)

    assert result["stuck"]["deals"] == 1
    assert result["stuck"]["amount"] == 3_000_000


def test_stuck_money_is_broken_down_by_department(frozen):
    """РОПу нужен свой отдел, а директору — у кого именно встало."""
    row = _pulse(with_stuck=True)["departments"][0]

    assert row["stuck"]["deals"] == 1
    assert row["stuck"]["amount"] == 3_000_000


def test_the_digest_names_the_edge_and_the_frozen_money(frozen, costs):
    """Обе новости обязаны попасть в утреннее сообщение, а не только на экран."""
    import pulse_digest

    deliveries = pulse_digest.build(Q3, "")
    boss = deliveries[0]["text"]

    assert "Безубыточность" in boss
    assert "НЕ дотянем" in boss
    # Впереди количество, а не сумма: сумма открытой сделки — намерение, а не
    # деньги, и «стоят без движения 572,6 млн» на боевых данных читалось как
    # «у нас полмиллиарда на столе».
    assert "Не двигаются 1 сделка" in boss
    assert "в карточках проставлено 3,0 млн ₽" in boss
