"""План несёт одна воронка — «Покупатели».

Решение агентства от 07.09. У продавцов другой чек: 87,5 тыс. против
1,09 млн, разница в двенадцать раз. Сложить их в одно выполнение значит
мерить план фактом, собранным по другому правилу, — и отдел, закрывший
полсотни мелких сделок собственников, выглядел бы наравне с отделом,
закрывшим четыре крупных.

Проверяется не только сама сумма. Всё, что печатается рядом с ней, обязано
считаться по тем же воронкам: покрытие поля суммы, вчерашний день в сводке,
число сделок. Одна строка сообщения, посчитанная по всем воронкам над
кварталом, посчитанным по одной, — это два отчёта в одном письме.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import plans
import pulse as pulse_module
from schema import analytics_session
from scope import Scope, scoped_session

Q3 = "2026-Q3"
MID = "2026-08-31T12:00:00+03:00"
BUYERS = 18
SELLERS = 0
DEPT = 60


def _user(conn, user_id, name, last_name, dept_id=DEPT, dept_name="Кретов"):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id,"
        " department_name, is_active, synced_at) VALUES (?, ?, ?, ?, ?, 1, 'x')",
        (user_id, name, last_name, dept_id, dept_name),
    )


def _won(conn, deal_id, user_id, amount, category, *, currency="RUB",
         closed="2026-08-10T09:00:00+00:00"):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, ?, 'WON', ?, 'CALL', ?, ?, '2026-07-01T00:00:00+00:00',
                ?, ?, 1, 1, 0, 0, 'x')
        """,
        (deal_id, f"Сделка {deal_id}", category, user_id, amount, currency,
         closed, closed),
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
    """Один брокер, две сделки: крупная у покупателей, мелкая у продавцов."""
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (18, 'Покупатели', 1, 10, 'x'), (0, 'Продавцы', 1, 20, 'x')"
        )
        _user(conn, 1, "Антон Кретов", "Кретов")
        _user(conn, 2, "Иван Броков", "Броков")
        _norm(conn, 2, 4_500_000)
        _won(conn, 1, 2, 1_000_000, BUYERS)
        _won(conn, 2, 2, 90_000, SELLERS)
    return analytics_db


def _pulse():
    with scoped_session(Scope.everything()) as conn:
        return pulse_module.pulse(conn, Q3, today=MID)


# --------------------------------------------------------------------------

def test_a_sellers_deal_does_not_count(agency):
    """90 тысяч от собственника в выполнение не входят."""
    result = _pulse()

    assert result["fact"] == 1_000_000
    assert result["departments"][0]["deals"] == 1, "сделка продавца не считается"


def test_the_screen_says_which_funnel_it_counted(agency):
    """Число, посчитанное по одной воронке, обязано её назвать.

    Рядом на «Сделках» лежат числа по другой воронке, и два разных факта
    без подписи читаются как ошибка одного из них.
    """
    result = _pulse()

    assert [row["category_id"] for row in result["funnels"]] == [BUYERS]
    assert result["funnels"][0]["name"] == "Покупатели", "названа словом, а не номером"


def test_coverage_is_measured_on_the_same_funnel(agency):
    """Покрытие описывает ту сумму, под которой стоит, а не соседнюю."""
    with analytics_session() as conn:
        _won(conn, 3, 2, 0, SELLERS)      # без суммы, но это продавцы

    result = _pulse()

    assert result["coverage"]["deals"] == 1, "в покрытие вошли только покупатели"
    assert result["coverage"]["share"] == 100.0


def test_a_second_funnel_is_a_setting_not_a_release(agency, monkeypatch):
    """День, когда план начнут считать по двум воронкам, наступит раньше выкатки."""
    from config import get_settings

    monkeypatch.setenv("PULSE_CATEGORY_IDS_JSON", "[0, 18]")
    get_settings.cache_clear()

    result = _pulse()

    assert result["fact"] == 1_090_000
    assert {row["category_id"] for row in result["funnels"]} == {SELLERS, BUYERS}


def test_an_empty_setting_does_not_silently_widen_the_plan(agency, monkeypatch):
    """Пустой список — это не «все воронки»: так план вырос бы от опечатки."""
    from config import get_settings

    monkeypatch.setenv("PULSE_CATEGORY_IDS_JSON", "[]")
    get_settings.cache_clear()

    assert plans.plan_category_ids() == (BUYERS,)
    assert _pulse()["fact"] == 1_000_000


def test_the_digest_yesterday_line_counts_the_same_funnel(agency):
    """Первая строка сообщения и вторая обязаны считать одно и то же."""
    import pulse_digest

    window = {"since": "2026-08-10T00:00:00+00:00",
              "until": "2026-08-11T00:00:00+00:00", "label": "10.08"}
    with scoped_session(Scope.everything()) as conn:
        closed = pulse_digest.closed_in(conn, window)

    assert closed["deals"] == 1, "сделка продавца во вчерашний день не попала"
    assert closed["amount"] == 1_000_000
