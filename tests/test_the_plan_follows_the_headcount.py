"""План вычисляется из штата, и каждый спорный случай назван, а не проглочен.

Все проверки ниже воспроизводят то, что нашлось на боевом портале, а не
выдуманные ситуации:

* РОП отдела «Волкова» административно числится в служебном подразделении
  «Битрикс»: её отдел остаётся без РОПа, а служебный получает лишнюю норму;
* учётка РОПа «Каратевский» отключена, а отдел с этим именем живой;
* у одного РОПа две карточки, старая отключена;
* ``lower()`` в SQLite не трогает кириллицу, поэтому опознание фамилий,
  сделанное запросом, молча не находит никого.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import plans
from schema import analytics_session
from scope import Scope, scoped_session

SALES = (42, 44, 46, 50, 60, 66)
Q3 = "2026-Q3"


def _user(conn, user_id, name, last_name, dept_id, dept_name, active=1):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id,"
        " department_name, is_active, synced_at) VALUES (?, ?, ?, ?, ?, ?, 'x')",
        (user_id, name, last_name, dept_id, dept_name, active),
    )


def _norm(conn, amount, *, scope_kind="company", scope_id=0,
          basis=plans.BASIS_PER_BROKER, period=Q3, metric="commission"):
    conn.execute(
        "INSERT INTO plan_norm(period_code, scope_kind, scope_id, metric,"
        " basis, amount, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'test', 'x')",
        (period, scope_kind, scope_id, metric, basis, amount),
    )


def _roster(conn, user_id, *, role, dept=None, period=Q3, note=""):
    conn.execute(
        "INSERT INTO plan_roster(period_code, user_id, department_id, plan_role,"
        " note, updated_at) VALUES (?, ?, ?, ?, ?, 'x')",
        (period, user_id, dept, role, note),
    )


@pytest.fixture
def portal(analytics_db):
    """Слепок боевого портала: шесть отделов продаж и два служебных."""
    with analytics_session() as conn:
        # Отдел Кретова: РОП на месте, плюс две карточки одного человека —
        # старая отключена и в состав попасть не должна.
        _user(conn, 1, "Антон Кретов", "Кретов", 60, "Кретов")
        _user(conn, 2, "Антон Кретов", "Кретов", 1, "Битрикс", active=0)
        _user(conn, 3, "Брокер Первый", "Первый", 60, "Кретов")
        _user(conn, 4, "Брокер Второй", "Второй", 60, "Кретов")
        # Отдел Волковой: брокеры есть, РОПа в отделе нет — она в «Битриксе».
        _user(conn, 5, "Брокер Третий", "Третий", 50, "Волкова")
        _user(conn, 6, "Брокер Четвёртый", "Четвёртый", 50, "Волкова")
        _user(conn, 7, "Вера Волкова", "Волкова", 1, "Битрикс")
        # Отдел Каратевского: РОП отключён, отдел живой.
        _user(conn, 8, "Брокер Пятый", "Пятый", 42, "Каратевский")
        _user(conn, 9, "Дмитрий Каратевский", "Каратевский", 42, "Каратевский", active=0)
        # Служебный отдел — в плане ему не место.
        _user(conn, 10, "Бухгалтер", "Бухгалтеров", 77, "Бэк-офис")
    return analytics_db


# --------------------------------------------------------------------------
# состав
# --------------------------------------------------------------------------

def test_a_service_department_carries_no_plan(portal):
    """Бэк-офис в план не входит: он не продаёт, а норму получил бы наравне."""
    with scoped_session(Scope.everything()) as conn:
        staff = plans.headcount(conn, Q3, SALES)

    names = {row["name"] for row in staff["departments"]}
    assert "Бэк-офис" not in names
    assert "Битрикс" not in names, "служебный отдел не должен получать норму"


def test_a_rop_is_not_counted_as_a_broker(portal):
    """РОП план не несёт — иначе отдел получает лишнюю норму на человека."""
    with scoped_session(Scope.everything()) as conn:
        staff = plans.headcount(conn, Q3, SALES)

    kretov = next(r for r in staff["departments"] if r["name"] == "Кретов")
    assert kretov["people"] == 3, "три активных карточки, отключённая не в счёт"
    assert kretov["rops"] == 1
    assert kretov["brokers"] == 2


def test_a_surname_in_cyrillic_is_recognised(portal):
    """Фамилия РОПа опознаётся, хотя SQLite не умеет lower() для кириллицы.

    Сравнение вынесено в Python. Сделай его запросом с
    ``lower(last_name) IN ('кретов', ...)`` — и не найдётся никто, молча и
    без ошибки: все РОПы станут брокерами, а план вырастет на их норму.
    """
    with scoped_session(Scope.everything()) as conn:
        staff = plans.headcount(conn, Q3, SALES)

    assert sum(row["rops"] for row in staff["departments"]) >= 1, (
        "ни один РОП не опознан — похоже, сравнение уехало в SQL"
    )


def test_a_department_whose_rop_sits_elsewhere_says_so(portal):
    """Отдел Волковой остаётся без РОПа, и это видно, а не подразумевается."""
    with scoped_session(Scope.everything()) as conn:
        staff = plans.headcount(conn, Q3, SALES)

    volkova = next(r for r in staff["departments"] if r["name"] == "Волкова")
    assert volkova["rop_known"] is False
    assert volkova["brokers"] == 2, "без вычета РОПа план отдела завышен на норму"
    assert "Волкова" in staff["departments_without_rop"]


def test_a_disabled_rop_leaves_the_department_without_one(portal):
    """Отключённая учётка РОПа не исчезает бесследно: отдел назван в списке."""
    with scoped_session(Scope.everything()) as conn:
        staff = plans.headcount(conn, Q3, SALES)

    karat = next(r for r in staff["departments"] if r["name"] == "Каратевский")
    assert karat["people"] == 1, "отключённый РОП в состав не входит"
    assert karat["rop_known"] is False
    assert "Каратевский" in staff["departments_without_rop"]


def test_the_roster_moves_a_rop_back_to_their_own_department(portal):
    """Ручное исключение чинит то, чего в портале не поправить.

    Волкова числится в «Битриксе». Строка ростера возвращает её в отдел 50
    ролью РОПа: отдел перестаёт быть без РОПа, а его план уменьшается на
    норму — при том, что карточку в Битриксе никто не трогал.
    """
    with analytics_session() as conn:
        _roster(conn, 7, role=plans.ROLE_ROP, dept=50, note="сидит в «Битриксе»")

    with scoped_session(Scope.everything()) as conn:
        staff = plans.headcount(conn, Q3, SALES)

    volkova = next(r for r in staff["departments"] if r["department_id"] == 50)
    assert volkova["rop_known"] is True
    assert volkova["brokers"] == 2, "РОП добавился в отдел, но брокером не стал"
    assert volkova["overridden"] == 1
    assert "Волкова" not in staff["departments_without_rop"]


def test_a_trainee_can_be_excluded_without_touching_bitrix(portal):
    """Роль «исключён» убирает человека из плана, но не из отдела."""
    with analytics_session() as conn:
        _roster(conn, 3, role=plans.ROLE_EXCLUDED, note="стажёр")

    with scoped_session(Scope.everything()) as conn:
        staff = plans.headcount(conn, Q3, SALES)

    kretov = next(r for r in staff["departments"] if r["name"] == "Кретов")
    assert kretov["brokers"] == 1
    assert kretov["excluded"] == 1
    assert kretov["people"] == 3, "человек остался в отделе, вышел только из плана"


def test_a_period_row_beats_the_always_row(portal):
    """Правило на конкретный квартал перекрывает правило «на все периоды»."""
    with analytics_session() as conn:
        _roster(conn, 3, role=plans.ROLE_EXCLUDED, period=plans.ANY_PERIOD)
        _roster(conn, 3, role=plans.ROLE_BROKER, period=Q3, note="вышел на план")

    with scoped_session(Scope.everything()) as conn:
        staff = plans.headcount(conn, Q3, SALES)

    kretov = next(r for r in staff["departments"] if r["name"] == "Кретов")
    assert kretov["brokers"] == 2
    assert kretov["excluded"] == 0


# --------------------------------------------------------------------------
# план
# --------------------------------------------------------------------------

def test_the_plan_is_computed_from_the_norm_not_stored(portal):
    """4,5 млн на брокера × состав. Наняли человека — цель выросла сама."""
    with analytics_session() as conn:
        _norm(conn, 4_500_000)

    with scoped_session(Scope.everything()) as conn:
        before = plans.plan(conn, Q3)

    with analytics_session() as conn:
        _user(conn, 20, "Новый Брокер", "Новичок", 60, "Кретов")

    # Новое соединение, а не то же самое: страница открывается заново, и
    # именно так наём доходит до плана.
    with scoped_session(Scope.everything()) as conn:
        after = plans.plan(conn, Q3)

    assert before["brokers"] == 5, "2 у Кретова + 2 у Волковой + 1 у Каратевского"
    assert before["plan"] == 5 * 4_500_000
    assert after["brokers"] == 6
    assert after["plan"] == 6 * 4_500_000, "план обязан следовать за штатом"


def test_a_department_norm_beats_the_company_norm(portal):
    """Своя норма отдела важнее общей: отделы бывают разной силы."""
    with analytics_session() as conn:
        _norm(conn, 4_500_000)
        _norm(conn, 1_000_000, scope_kind="department", scope_id=60)

    with scoped_session(Scope.everything()) as conn:
        result = plans.plan(conn, Q3)

    kretov = next(r for r in result["departments"] if r["department_id"] == 60)
    assert kretov["plan"] == 2 * 1_000_000
    assert result["plan"] == 2 * 1_000_000 + 3 * 4_500_000


def test_a_gap_between_the_company_goal_and_its_parts_is_named(portal):
    """Цель компании выше суммы отделов — зазор показывается числом.

    Директор вправе поставить цель с запасом. Молча подогнать её под сумму
    отделов или молча заменить сумму целью — два разных способа соврать.
    """
    with analytics_session() as conn:
        _norm(conn, 4_500_000, scope_kind="department", scope_id=60)
        _norm(conn, 4_500_000, scope_kind="department", scope_id=50)
        _norm(conn, 4_500_000, scope_kind="department", scope_id=42)
        _norm(conn, 30_000_000, basis=plans.BASIS_ABSOLUTE)

    with scoped_session(Scope.everything()) as conn:
        result = plans.plan(conn, Q3)

    assert result["plan_from_departments"] == 5 * 4_500_000
    assert result["plan_declared"] == 30_000_000
    assert result["unallocated"] == 30_000_000 - 22_500_000


def test_a_missing_norm_is_not_a_zero_plan(portal):
    """Нет нормы — план None с причиной, а не ноль.

    Ноль на экране неотличим от честно невыполненного плана, и полоса
    выполнения от него рисуется одинаково.
    """
    with scoped_session(Scope.everything()) as conn:
        result = plans.plan(conn, Q3)

    assert result["norms_found"] == 0
    assert all(row["plan"] is None for row in result["departments"])
    assert all(row["reason"] == "норма не задана" for row in result["departments"])


# --------------------------------------------------------------------------
# область видимости
# --------------------------------------------------------------------------

def test_a_rop_never_sees_the_company_goal(portal):
    """Норма компании ограниченному соединению не видна ни при каких условиях."""
    with analytics_session() as conn:
        _norm(conn, 30_000_000, basis=plans.BASIS_ABSOLUTE)
        _norm(conn, 4_500_000, scope_kind="department", scope_id=60)
        _norm(conn, 9_000_000, scope_kind="department", scope_id=50)

    with scoped_session(Scope.departments([60])) as conn:
        result = plans.plan(conn, Q3)

    assert result["plan_declared"] is None, "цель компании РОПу не показывается"
    assert [row["department_id"] for row in result["departments"]] == [60]
    assert result["plan"] == 2 * 4_500_000, "чужая норма в сумму не попала"


def test_a_roster_row_follows_the_department_it_names(portal):
    """Строка ростера видна РОПу того отдела, за который человек отвечает.

    Волкова числится в «Битриксе», а отвечает за отдел 50. Её строка обязана
    быть видна РОПу пятидесятого — иначе он не увидит, почему из его состава
    кто-то вычтен.
    """
    with analytics_session() as conn:
        _roster(conn, 7, role=plans.ROLE_ROP, dept=50)

    with scoped_session(Scope.departments([50])) as conn:
        staff = plans.headcount(conn, Q3, SALES)

    volkova = next(r for r in staff["departments"] if r["department_id"] == 50)
    assert volkova["rop_known"] is True
    assert volkova["overridden"] == 1


# --------------------------------------------------------------------------
# темп
# --------------------------------------------------------------------------

def test_the_pace_counts_working_days_not_calendar_days():
    """Июль 2026: 31 календарный день и 23 рабочих."""
    since, ends = plans.quarter_bounds("2026-Q3")
    july_end = plans._utc(__import__("datetime").date(2026, 8, 1))

    assert plans.working_days(since, july_end) == 23
    assert plans.working_days(since, ends) == 66, "весь третий квартал"


def test_a_holiday_is_not_a_working_day():
    """Праздник в будни вычитается — когда календарь появится."""
    from datetime import date

    since, ends = plans.quarter_bounds("2026-Q3")
    assert plans.working_days(since, ends, holidays=[date(2026, 7, 1)]) == 65


def test_being_behind_is_measured_against_time_not_against_zero():
    """47% плана при 59% срока — отставание, хотя половина плана сделана."""
    result = plans.pace(14_200_000, 30_000_000, elapsed_days=13, total_days=22)

    assert result["plan_share"] == 47.3
    assert result["time_share"] == 59.1
    assert result["ratio"] == 0.8
    assert result["behind"] is True
    assert result["calendar_source"] == "пн-пт"


def test_no_plan_gives_no_verdict(portal):
    """Без плана темпа нет: None, а не ноль и не «отстаём»."""
    result = plans.pace(14_200_000, None, elapsed_days=13, total_days=22)

    assert result["plan_share"] is None
    assert result["ratio"] is None
    assert result["behind"] is None
    assert result["reason"] == "план не задан"


# --------------------------------------------------------------------------
# именной план
# --------------------------------------------------------------------------

def test_a_named_norm_beats_the_general_one(portal):
    """Норма конкретного человека важнее общей: ступени у брокеров разные."""
    with analytics_session() as conn:
        _norm(conn, 4_500_000)
        _norm(conn, 5_500_000, scope_kind=plans.SCOPE_USER, scope_id=3,
              basis=plans.BASIS_ABSOLUTE)

    with scoped_session(Scope.everything()) as conn:
        result = plans.plan(conn, Q3)

    kretov = next(r for r in result["departments"] if r["department_id"] == 60)
    named = next(m for m in kretov["members"] if m["user_id"] == 3)
    assert named["plan"] == 5_500_000


def test_a_newcomer_outside_the_list_carries_no_plan(portal):
    """«Остальные без плана» — значит без плана, а не по общей норме.

    Как только у периода появилась хоть одна именная норма, общая перестаёт
    подставляться. Иначе новичок, которому норму сознательно не ставили,
    молча получил бы её и завысил план отдела.
    """
    with analytics_session() as conn:
        _norm(conn, 4_500_000)
        _norm(conn, 3_500_000, scope_kind=plans.SCOPE_USER, scope_id=3,
              basis=plans.BASIS_ABSOLUTE)

    with scoped_session(Scope.everything()) as conn:
        result = plans.plan(conn, Q3)

    kretov = next(r for r in result["departments"] if r["department_id"] == 60)
    assert kretov["on_plan"] == 1
    assert kretov["without_norm"] == 2, "РОП и второй брокер нормы не получили"
    assert kretov["plan"] == 3_500_000, "общая норма новичку не подставилась"


def test_a_rop_named_in_the_list_does_carry_a_plan(portal):
    """Юлия Шпырная — РОП и при этом в плане. Роль не запрещает нести норму.

    Правило «РОП не идёт в план» описывает умолчание, а не запрет: когда
    агентство назвало руководителя в списке поимённо, оно так и решило.
    """
    with analytics_session() as conn:
        _norm(conn, 3_500_000, scope_kind=plans.SCOPE_USER, scope_id=1,
              basis=plans.BASIS_ABSOLUTE)

    with scoped_session(Scope.everything()) as conn:
        result = plans.plan(conn, Q3)

    kretov = next(r for r in result["departments"] if r["department_id"] == 60)
    rop = next(m for m in kretov["members"] if m["user_id"] == 1)
    assert rop["role"] == plans.ROLE_ROP
    assert rop["plan"] == 3_500_000
    assert kretov["plan"] == 3_500_000


def test_the_department_plan_is_the_sum_of_its_named_people(portal):
    """План отдела складывается снизу, из людей, а не задаётся сверху."""
    with analytics_session() as conn:
        for user_id, amount in ((3, 3_500_000), (4, 5_500_000)):
            _norm(conn, amount, scope_kind=plans.SCOPE_USER, scope_id=user_id,
                  basis=plans.BASIS_ABSOLUTE)
        _norm(conn, 4_500_000, scope_kind=plans.SCOPE_USER, scope_id=5,
              basis=plans.BASIS_ABSOLUTE)

    with scoped_session(Scope.everything()) as conn:
        result = plans.plan(conn, Q3)

    kretov = next(r for r in result["departments"] if r["department_id"] == 60)
    assert kretov["plan"] == 9_000_000
    assert result["plan_from_departments"] == 13_500_000


def test_a_rop_sees_only_the_norms_of_their_own_people(portal):
    """Именная норма чужого отдела ограниченному соединению не видна."""
    with analytics_session() as conn:
        _norm(conn, 3_500_000, scope_kind=plans.SCOPE_USER, scope_id=3,
              basis=plans.BASIS_ABSOLUTE)
        _norm(conn, 5_500_000, scope_kind=plans.SCOPE_USER, scope_id=5,
              basis=plans.BASIS_ABSOLUTE)

    with scoped_session(Scope.departments([60])) as conn:
        result = plans.plan(conn, Q3)

    assert result["plan_from_departments"] == 3_500_000, "чужая норма в сумму не попала"
    assert [r["department_id"] for r in result["departments"]] == [60]
