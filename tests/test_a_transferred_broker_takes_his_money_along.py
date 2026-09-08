"""План и факт человека всегда в одном отделе.

Ростер существует потому, что портал ошибается: руководитель отдела
«Волкова» числится в служебном подразделении «Битрикс», а работает на свой
отдел. Строка ростера — это решение администратора о том, чей человек.

Опасность такого решения в том, что оно может примениться наполовину.
Состав отдела считается по ростеру, а сделки отбираются по карточке в
Битриксе — и тогда норма человека попадает в план одного отдела, а
закрытые им деньги в факт другого. У нового отдела выполнение занижено, у
прежнего завышено, и оба числа выглядят совершенно правдоподобно: ни одно
не выбивается настолько, чтобы кто-то полез проверять.

Здесь закреплено, что половины не бывает: куда ушёл план, туда ушли и
деньги, и видимость — в каждой области видимости сразу.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import plans
import pulse as pulse_module
from schema import analytics_session
from scope import Scope, scoped_session

Q3 = "2026-Q3"
Q2 = "2026-Q2"
MID = "2026-08-31T12:00:00+03:00"

KRETOV = 60      # отдел, в который перевели
SHPYRNAYA = 66   # отдел, из которого перевели


def _user(conn, user_id, name, last_name, dept_id, dept_name, active=1):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id,"
        " department_name, is_active, synced_at) VALUES (?, ?, ?, ?, ?, ?, 'x')",
        (user_id, name, last_name, dept_id, dept_name, active),
    )


def _won(conn, deal_id, user_id, amount, closed="2026-08-10T09:00:00+00:00"):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, 18, 'C18:WON', ?, 'CALL', ?, 'RUB', '2026-07-01T00:00:00+00:00',
                ?, ?, 1, 1, 0, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", user_id, amount, closed, closed),
    )


def _norm(conn, user_id, amount, period=Q3):
    conn.execute(
        "INSERT INTO plan_norm(period_code, scope_kind, scope_id, metric,"
        " basis, amount, source, updated_at)"
        " VALUES (?, 'user', ?, 'commission', 'absolute', ?, 'test', 'x')",
        (period, user_id, amount),
    )


def _roster(conn, user_id, *, dept=None, role=plans.ROLE_BROKER, period=Q3):
    conn.execute(
        "INSERT INTO plan_roster(period_code, user_id, department_id, plan_role,"
        " note, updated_at) VALUES (?, ?, ?, ?, '', 'x')",
        (period, user_id, dept, role),
    )


@pytest.fixture
def transfer(analytics_db):
    """Брокер числится у Шпырной, а план несёт у Кретова.

    Так выглядит перевод в середине квартала: карточку в портале поправят
    когда-нибудь, а работает человек уже на новый отдел.
    """
    with analytics_session() as conn:
        _user(conn, 1, "Антон Кретов", "Кретов", KRETOV, "Кретов")
        _user(conn, 2, "Пере Ведённый", "Ведённый", SHPYRNAYA, "Шпырная")
        _user(conn, 3, "Ос Тавшийся", "Тавшийся", SHPYRNAYA, "Шпырная")
        _norm(conn, 2, 3_500_000)
        _norm(conn, 3, 4_500_000)
        _roster(conn, 2, dept=KRETOV)
        _won(conn, 1, 2, 2_000_000)
        _won(conn, 2, 3, 1_000_000)
    return analytics_db


def _pulse(scope=None):
    with scoped_session(scope or Scope.everything()) as conn:
        result = pulse_module.pulse(conn, Q3, today=MID)
    return {row["department_id"]: row for row in result["departments"]}, result


# --------------------------------------------------------------------------
# взгляд директора
# --------------------------------------------------------------------------

def test_the_money_moves_with_the_norm(transfer):
    """Норма ушла к Кретову — и деньги там же, а не у Шпырной."""
    by_id, _ = _pulse()

    assert by_id[KRETOV]["plan"] == 3_500_000
    assert by_id[KRETOV]["fact"] == 2_000_000, "деньги там, где норма"
    assert by_id[SHPYRNAYA]["plan"] == 4_500_000
    assert by_id[SHPYRNAYA]["fact"] == 1_000_000, "в прежнем отделе только свои"


def test_the_company_total_does_not_change(transfer):
    """Перевод перекладывает деньги между отделами, а не создаёт их."""
    _, result = _pulse()

    assert result["fact"] == 3_000_000
    assert result["plan"] == 8_000_000
    assert sum(row["fact"] for row in result["departments"]) == result["fact"]


# --------------------------------------------------------------------------
# взгляд РОПа
# --------------------------------------------------------------------------

def test_the_new_rop_sees_both_halves(transfer):
    """У нового отдела и норма переведённого, и его сделка."""
    by_id, result = _pulse(Scope.departments([KRETOV]))

    assert by_id[KRETOV]["plan"] == 3_500_000
    assert by_id[KRETOV]["fact"] == 2_000_000
    assert result["fact"] == 2_000_000, "чужого отдела в своей сводке нет"


def test_the_old_rop_is_not_left_with_a_plan_without_money(transfer):
    """Прежний отдел не ждёт от переведённого ни нормы, ни выручки.

    Это и есть половинчатое применение, ради которого написан файл: если
    прежний отдел продолжает считать человека своим, он несёт его норму и
    никогда не увидит его денег.
    """
    by_id, result = _pulse(Scope.departments([SHPYRNAYA]))

    assert by_id[SHPYRNAYA]["plan"] == 4_500_000, "только норма оставшегося"
    assert by_id[SHPYRNAYA]["fact"] == 1_000_000
    assert result["without_norm"] == 0, "переведённого нет и в составе"
    assert KRETOV not in by_id, "чужой отдел в сводке РОПа не появляется"


def test_the_transferred_deal_is_invisible_to_the_old_rop(transfer):
    """Сделка ушла вместе с человеком — прежний РОП её не видит."""
    with scoped_session(Scope.departments([SHPYRNAYA])) as conn:
        rows = conn.execute("SELECT deal_id FROM v_deal ORDER BY deal_id").fetchall()

    assert [row["deal_id"] for row in rows] == [2]


# --------------------------------------------------------------------------
# края
# --------------------------------------------------------------------------

def test_a_departed_broker_still_counts_where_he_worked(transfer):
    """Уволенного в составе нет, а деньги его квартала есть.

    Ростер про него молчит, значит отдел берётся из карточки. Вычесть эти
    деньги значило бы недосчитать квартал отдела на реальную сделку.
    """
    with analytics_session() as conn:
        _user(conn, 9, "У Шедший", "Шедший", SHPYRNAYA, "Шпырная", active=0)
        _won(conn, 3, 9, 800_000)

    by_id, result = _pulse()

    assert by_id[SHPYRNAYA]["fact"] == 1_800_000
    assert result["fact_on_plan"] == 3_000_000, "нормы у него нет"


def test_the_later_period_wins_over_the_open_ended_row(analytics_db):
    """Две строки ростера — берётся поздняя, а не любая.

    Без порядка SQLite вернул бы ту, которая окажется удобнее плану
    запроса, и область видимости РОПа менялась бы сама по себе.
    """
    with analytics_session() as conn:
        _user(conn, 1, "Антон Кретов", "Кретов", KRETOV, "Кретов")
        _user(conn, 2, "Пере Ведённый", "Ведённый", SHPYRNAYA, "Шпырная")
        _roster(conn, 2, dept=SHPYRNAYA, period=Q2)
        _roster(conn, 2, dept=KRETOV, period=Q3)
        _won(conn, 1, 2, 2_000_000)

    with scoped_session(Scope.departments([KRETOV])) as conn:
        home = conn.execute(
            "SELECT department_id FROM user_home WHERE user_id = 2"
        ).fetchone()
        deals = conn.execute("SELECT deal_id FROM v_deal").fetchall()

    assert home["department_id"] == KRETOV
    assert [row["deal_id"] for row in deals] == [1]


# --------------------------------------------------------------------------
# витрина старее ростера
# --------------------------------------------------------------------------

def test_an_older_mart_still_opens(analytics_db):
    """Витрина без plan_roster открывается, а не падает на подключении.

    Правило «чей человек» считается сразу при открытии соединения, а не
    лениво в представлении. Значит, отсутствующая таблица роняет уже
    apply_scope — и сообщение про plan_roster получил бы тот, кто всего лишь
    открыл старую витрину, чтобы прочитать сделки. Отсутствие ростера — это
    «переносов нет», а не «работать нельзя».
    """
    with analytics_session() as conn:
        _user(conn, 1, "Антон Кретов", "Кретов", KRETOV, "Кретов")
        _won(conn, 1, 1, 2_000_000)
        conn.execute("DROP TABLE plan_roster")

    with scoped_session(Scope.departments([KRETOV])) as conn:
        home = conn.execute(
            "SELECT department_id FROM user_home WHERE user_id = 1"
        ).fetchone()
        deals = conn.execute("SELECT deal_id FROM v_deal").fetchall()

    assert home["department_id"] == KRETOV, "отдел берётся из карточки"
    assert [row["deal_id"] for row in deals] == [1]


def test_an_empty_mart_opens_too(analytics_db):
    """Пустой файл витрины — тоже не повод падать на подключении.

    Так выглядит первый запуск до ETL и промах мимо пути к базе. Ошибку
    должен выдать первый же запрос метрики, назвав нехватку данных, а не
    apply_scope с именем служебной таблицы.
    """
    with analytics_session() as conn:
        for table in ("plan_roster", "fact_stage_event", "fact_deal", "dim_user"):
            conn.execute(f"DROP TABLE {table}")

    with scoped_session(Scope.everything()) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM user_home").fetchone()["n"] == 0


# --------------------------------------------------------------------------
# путь к витрине
# --------------------------------------------------------------------------

def test_the_named_database_wins_over_the_default(tmp_path, monkeypatch):
    """Указанный путь к витрине не теряется, когда настройки не грузятся.

    Ловушка, на которую я попался сам: скрипту сказали работать с временной
    базой через ANALYTICS_DB_PATH, настройки молча упали на нехватке ключа
    Битрикса, и он сходил в боевую data/analytics.db — прочитал чужие данные
    и записал туда свои, ничем этого не показав.

    Умолчание остаётся для тестов, которым путь не важен; названный путь
    сильнее умолчания.
    """
    import schema
    from config import get_settings

    named = tmp_path / "named.db"
    monkeypatch.setenv("ANALYTICS_DB_PATH", str(named))
    # Настройки не грузятся: снято обязательное поле.
    monkeypatch.delenv("B24_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    with pytest.raises(Exception):
        get_settings()

    assert schema.resolve_db_path() == named

    monkeypatch.delenv("ANALYTICS_DB_PATH", raising=False)
    get_settings.cache_clear()
    assert schema.resolve_db_path() == schema.DEFAULT_DB_PATH
