"""Отделы сравниваются между собой, а не по одному под фильтром.

Отдел с 13% выполнения — это пятеро, доводящих сделки понемногу, или один,
доводящий всех, при четверых, теряющих клиентов на первом показе. На экране
плана эти случаи неразличимы, а лечение у них разное: первому нужен поток,
второму — разговор с людьми.

Главное требование к таблице — она обязана называть ту же конверсию, что и
воронка выше на той же странице. Две таблицы одного экрана, спорящие о
конверсии одной воронки, оставляют правым того, кто громче.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import metrics
from schema import analytics_session
from scope import Scope, scoped_session

SINCE = "2026-08-01T00:00:00+00:00"
UNTIL = "2026-09-01T00:00:00+00:00"
KRETOV, VOLKOVA = 60, 50

STAGES = (
    ("C18:NEW", "Подбор", 10, "in_progress"),
    ("C18:SHOW", "Показ", 20, "in_progress"),
    ("C18:WON", "Успех", 90, "won"),
)


def _user(conn, user_id, name, dept, dept_name):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id,"
        " department_name, is_active, synced_at) VALUES (?, ?, ?, ?, ?, 1, 'x')",
        (user_id, name, name.split()[-1], dept, dept_name),
    )


def _deal(conn, deal_id, user_id, stage, *, created="2026-08-05T00:00:00+00:00"):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, 18, ?, ?, 'CALL', 100000, 'RUB', ?, ?, NULL, 0, 0, 0, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", stage, user_id, created, created),
    )


def _walk(conn, deal_id, path, *, days=2):
    """Провести сделку по стадиям. Последний интервал остаётся открытым."""
    day = 5
    for seq, stage in enumerate(path):
        last = seq == len(path) - 1
        entered = f"2026-08-{day:02d}T00:00:00+00:00"
        day += days
        left = None if last else f"2026-08-{day:02d}T00:00:00+00:00"
        conn.execute(
            "INSERT INTO fact_stage_event(entity_type, entity_id, category_id,"
            " stage_id, entered_at, left_at, duration_sec, seq)"
            " VALUES ('deal', ?, 18, ?, ?, ?, ?, ?)",
            (deal_id, stage, entered, left, None if last else days * 86400, seq),
        )


@pytest.fixture
def two_departments(analytics_db):
    """Кретов теряет всех на показе, Волкова доводит до конца.

    По деньгам и проценту плана они могут выглядеть одинаково; по столбцам
    этой таблицы — совершенно по-разному.
    """
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort,"
                     " synced_at) VALUES (18, 'Покупатели', 1, 10, 'x')")
        for stage_id, name, sort, semantic in STAGES:
            conn.execute(
                "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
                " synced_at) VALUES (?, 18, ?, ?, ?, 'x')",
                (stage_id, name, sort, semantic),
            )
        _user(conn, 1, "Антон Кретов", KRETOV, "Кретов")
        _user(conn, 2, "Вера Волкова", VOLKOVA, "Волкова")

        # Кретов: четыре сделки, до показа дошли две, до успеха ни одной.
        for deal_id in (101, 102):
            _deal(conn, deal_id, 1, "C18:SHOW")
            _walk(conn, deal_id, ["C18:NEW", "C18:SHOW"])
        for deal_id in (103, 104):
            _deal(conn, deal_id, 1, "C18:NEW")
            _walk(conn, deal_id, ["C18:NEW"])

        # Волкова: две сделки, обе дошли до конца.
        for deal_id in (201, 202):
            _deal(conn, deal_id, 2, "C18:WON")
            _walk(conn, deal_id, ["C18:NEW", "C18:SHOW", "C18:WON"])
    return analytics_db


def _funnel(scope=None):
    with scoped_session(scope or Scope.everything()) as conn:
        data = metrics.funnel_by_department(conn, 18, SINCE, UNTIL)
    return {row["name"]: row for row in data["departments"]}, data


# --------------------------------------------------------------------------

def test_the_columns_show_where_each_one_loses_people(two_departments):
    """Кретов обрывается на показе, Волкова доходит до успеха."""
    by_name, _ = _funnel()
    kretov, volkova = by_name["Кретов"]["stages"], by_name["Волкова"]["stages"]

    assert kretov["C18:NEW"]["reached"] == 4
    assert kretov["C18:SHOW"]["reached"] == 2
    assert kretov["C18:WON"]["reached"] == 0

    assert volkova["C18:NEW"]["reached"] == 2
    assert volkova["C18:SHOW"]["reached"] == 2
    assert volkova["C18:WON"]["reached"] == 2


def test_step_conversion_is_the_share_of_the_previous_stage(two_departments):
    """Половина против всех — это и есть разница, ради которой таблица нужна."""
    by_name, _ = _funnel()
    kretov, volkova = by_name["Кретов"]["stages"], by_name["Волкова"]["stages"]

    assert kretov["C18:SHOW"]["conversion_step"] == 50.0
    assert volkova["C18:SHOW"]["conversion_step"] == 100.0
    assert volkova["C18:WON"]["conversion_step"] == 100.0


def test_it_says_the_same_as_the_funnel_above_it(two_departments):
    """Одна страница не должна называть две разные конверсии одной воронки."""
    with scoped_session(Scope.everything()) as conn:
        whole = metrics.deal_funnel(conn, 18, SINCE, UNTIL)
        by_dept = metrics.funnel_by_department(conn, 18, SINCE, UNTIL)

    total = {row["stage_id"]: row["reached"] for row in whole["stages"]}
    summed: dict[str, int] = {}
    for dept in by_dept["departments"]:
        for stage_id, cell in dept["stages"].items():
            summed[stage_id] = summed.get(stage_id, 0) + cell["reached"]

    assert summed == total, "сумма по отделам обязана дать воронку целиком"
    assert sum(d["cohort_size"] for d in by_dept["departments"]) == whole["cohort_size"]


def test_a_stage_nobody_left_has_no_median(two_departments):
    """Стадия, с которой ещё не ушли, времени не имеет — там прочерк, не ноль."""
    by_name, _ = _funnel()
    volkova = by_name["Волкова"]["stages"]

    assert volkova["C18:NEW"]["median_days"] == 2.0
    assert volkova["C18:WON"]["median_days"] is None, "с успеха никто не уходит"


def test_the_cohort_is_deals_created_in_the_period(two_departments):
    """Сделка прошлого месяца в когорту не входит, даже если двигалась в этом."""
    with analytics_session() as conn:
        _deal(conn, 301, 1, "C18:SHOW", created="2026-07-10T00:00:00+00:00")
        _walk(conn, 301, ["C18:NEW", "C18:SHOW"])

    by_name, _ = _funnel()

    assert by_name["Кретов"]["cohort_size"] == 4


def test_a_rop_sees_only_his_own_column(two_departments):
    """Область видимости приходит соединением — фильтровать в шаблоне нечего."""
    by_name, _ = _funnel(Scope.departments([VOLKOVA]))

    assert list(by_name) == ["Волкова"]


def test_the_roster_moves_the_column_too(two_departments):
    """Тот же ответ на вопрос «чей человек», что и в «Пульсе»."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO plan_roster(period_code, user_id, department_id, plan_role,"
            " note, updated_at) VALUES ('*', 2, ?, 'rop', '', 'x')", (KRETOV,),
        )

    by_name, _ = _funnel()

    assert "Волкова" not in by_name, "ростер перенёс её к Кретову"
    assert by_name["Кретов"]["cohort_size"] == 6


def test_the_cells_are_keyed_by_stage_not_by_position(two_departments):
    """Клетка достаётся по стадии, а не по порядковому номеру.

    Шаблон обходит стадии внешним циклом, а отделы внутренним, и счётчик
    вложенного цикла принадлежит внутреннему. Со списком клеток таблица
    один раз уже отрисовалась с одинаковыми числами во всех строках —
    ошиблась и ничем себя не выдала.
    """
    by_name, data = _funnel()

    for dept in data["departments"]:
        assert isinstance(dept["stages"], dict)
        assert set(dept["stages"]) == {s["stage_id"] for s in data["stages"]}

    kretov = by_name["Кретов"]["stages"]
    assert len({cell["reached"] for cell in kretov.values()}) > 1, (
        "разные стадии обязаны давать разные числа"
    )
