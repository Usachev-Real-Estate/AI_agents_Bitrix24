"""Страницы дашборда отрисовываются на настоящих данных.

Тесты на авторизацию проверяют, что без входа ничего не отдаётся. Эти —
что после входа отдаётся именно то, что нужно: цифры, ссылки на карточки
Bitrix и данные для графиков.
"""

import json

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import store
from app import create_app
from config import get_settings
from schema import analytics_session

BASE = "/dashboard"
LOGIN = "chief"
PASSWORD = "correct-horse-battery"


@pytest.fixture
def seeded_app(analytics_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at) "
            "VALUES (18, 'Покупатели', 1, 10, 'x')"
        )
        for stage_id, name, sort, semantic in (
            ("C18:NEW", "Подбор", 10, "in_progress"),
            ("C18:SHOW", "Первый показ", 20, "in_progress"),
            ("C18:WON", "Договор закрыт", 90, "won"),
            ("C18:APOLOGY", "Сделка проиграна", 95, "lost"),
        ):
            conn.execute(
                "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic, "
                "synced_at) VALUES (?, 18, ?, ?, ?, 'x')", (stage_id, name, sort, semantic),
            )
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name, "
            "is_active, synced_at) VALUES (32, 'Иван Петров', 44, 'Отдел Трофимовой', 1, 'x')"
        )
        conn.execute(
            "INSERT INTO dim_source(source_id, name, synced_at) VALUES ('CALL', 'Звонок', 'x')"
        )
        conn.execute(
            "INSERT INTO dim_lead_status(status_id, name, sort, semantic, synced_at) "
            "VALUES ('NEW', 'Не обработан', 10, 'in_progress', 'x')"
        )
        for deal_id, stage, won, lost, amount, closed in (
            (501, "C18:WON", 1, 0, 750000, "2026-08-12T00:00:00+00:00"),
            (502, "C18:NEW", 0, 0, 0, None),
            (503, "C18:APOLOGY", 0, 1, 200000, "2026-08-14T00:00:00+00:00"),
        ):
            conn.execute(
                "INSERT INTO fact_deal(deal_id, title, category_id, stage_id, "
                "assigned_by_id, source_id, opportunity, date_create, closedate, "
                "is_closed, is_won, is_lost, is_deleted, synced_at) "
                "VALUES (?, ?, 18, ?, 32, 'CALL', ?, '2026-08-01T00:00:00+00:00', ?, "
                "?, ?, ?, 0, 'x')",
                (deal_id, f"Квартира по сделке {deal_id}", stage, amount, closed,
                 1 if closed else 0, won, lost),
            )
            conn.execute(
                "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, "
                "stage_id, entered_at, left_at, duration_sec, seq) "
                "VALUES ('deal', ?, 18, 'C18:NEW', '2026-08-01T00:00:00+00:00', "
                "'2026-08-04T00:00:00+00:00', 259200, 0)", (deal_id,),
            )
            conn.execute(
                "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, "
                "stage_id, entered_at, left_at, duration_sec, seq) "
                "VALUES ('deal', ?, 18, ?, '2026-08-04T00:00:00+00:00', NULL, NULL, 1)",
                (deal_id, stage),
            )
        conn.execute(
            "INSERT INTO fact_lead(lead_id, title, status_id, source_id, "
            "assigned_by_id, date_create, is_converted, converted_deal_id, "
            "is_deleted, synced_at) VALUES (901, 'Лид с сайта', 'NEW', 'CALL', 32, "
            "'2026-08-01T00:00:00+00:00', 1, 501, 0, 'x')"
        )
        conn.execute(
            "INSERT INTO etl_run(kind, entity, started_at, finished_at, status, "
            "rows_upserted) VALUES ('incremental', '', '2026-08-20T10:00:00+00:00', "
            "'2026-08-20T10:01:00+00:00', 'ok', 4)"
        )
        conn.execute(
            "INSERT INTO analytics_meta(key, value) VALUES ('window_since', '2025-08-01')"
        )

    application = create_app()
    store.create_user(LOGIN, PASSWORD, "Руководитель", role="admin")
    return application


@pytest.fixture
def client(seeded_app):
    session = TestClient(seeded_app, follow_redirects=False)
    session.get(f"{BASE}/login")
    response = session.post(f"{BASE}/login", data={
        "username": LOGIN, "password": PASSWORD,
        "csrf_token": session.cookies.get("dash_csrf"), "next": "",
    })
    assert response.status_code == 303
    return session


PERIOD = "?start=2026-08-01&end=2026-08-31&category=18"


@pytest.mark.parametrize("path", [
    "/", "/leads", "/deals", "/movement", "/people", "/table", "/quality",
])
def test_every_page_renders(client, path):
    response = client.get(f"{BASE}{path}{PERIOD}")
    assert response.status_code == 200, response.text[:600]
    assert "Traceback" not in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_overview_shows_money_with_coverage(client):
    """Сумма без покрытия вводит в заблуждение — покрытие обязано быть на странице."""
    body = client.get(f"{BASE}/{PERIOD}").text
    assert "заполнено" in body
    # Открытая сделка одна, и сумма у неё пустая.
    assert "0 из 1" in body or "0%" in body


def test_deal_funnel_separates_cohort_from_snapshot(client):
    body = client.get(f"{BASE}/deals{PERIOD}").text
    assert "Дошли" in body and "Стоит сейчас" in body
    assert "когда-либо" in body


def test_table_links_to_bitrix_cards(client):
    body = client.get(f"{BASE}/table{PERIOD}").text
    assert "/crm/deal/details/501/" in body
    assert "Квартира по сделке 501" in body


def test_table_can_switch_to_leads(client):
    body = client.get(f"{BASE}/table{PERIOD}&entity=lead").text
    assert "/crm/lead/details/901/" in body
    assert "Лид с сайта" in body


def test_chart_payloads_are_valid_json_islands(client):
    """Данные графиков едут островками JSON: строгий CSP не пустил бы inline-скрипт."""
    body = client.get(f"{BASE}/deals{PERIOD}").text
    start = body.index('id="deal-funnel-data"')
    payload = body[body.index(">", start) + 1:body.index("</script>", start)]
    data = json.loads(payload)
    assert data["stages"]
    assert {"name", "reached", "conversion_from_start"} <= set(data["stages"][0])


def test_csv_export_returns_rows_with_links(client):
    response = client.get(f"{BASE}/api/export.csv{PERIOD}")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    text = response.content.decode("utf-8-sig")
    assert "Ссылка" in text.splitlines()[0]
    assert "/crm/deal/details/501/" in text


def test_api_returns_the_same_numbers_as_the_page(client):
    payload = client.get(f"{BASE}/api/funnel{PERIOD}").json()
    assert payload["category_id"] == 18
    stages = {row["stage_id"]: row for row in payload["funnel"]["stages"]}
    assert stages["C18:NEW"]["reached"] == 3
    assert payload["funnel"]["cohort_size"] == 3


def test_quality_page_reports_missing_amounts(client):
    body = client.get(f"{BASE}/quality{PERIOD}").text
    assert "Сделок без суммы" in body
    assert "Сходимость с Bitrix" in body


def test_empty_warehouse_explains_itself_instead_of_crashing(analytics_db, monkeypatch):
    """Пустая витрина — рабочее состояние сразу после установки, а не ошибка."""
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "m" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    application = create_app()
    store.create_user(LOGIN, PASSWORD, role="admin")
    session = TestClient(application, follow_redirects=False)
    session.get(f"{BASE}/login")
    session.post(f"{BASE}/login", data={
        "username": LOGIN, "password": PASSWORD,
        "csrf_token": session.cookies.get("dash_csrf"), "next": "",
    })
    for path in ("/", "/deals", "/movement", "/leads", "/quality", "/table"):
        response = session.get(f"{BASE}{path}")
        assert response.status_code == 200, f"{path}: {response.text[:400]}"
    assert "ETL ещё ни разу не отработал" in session.get(f"{BASE}/").text


def test_movement_page_offers_a_department_filter(client):
    body = client.get(f"{BASE}/movement{PERIOD}").text
    assert 'name="department"' in body
    assert "Все отделы" in body
    assert "Отдел Трофимовой" in body


def test_movement_page_applies_the_department_filter(client):
    """Чужой отдел не должен приносить в выборку чужие переходы."""
    theirs = client.get(f"{BASE}/movement{PERIOD}&department=44").text
    nobody = client.get(f"{BASE}/movement{PERIOD}&department=999").text
    assert "Отдел Трофимовой" in theirs
    assert "Квартира по сделке" not in nobody or "Зависших сделок нет" in nobody


def test_movement_page_warns_that_department_is_the_current_assignee(client):
    """Иначе РОП увидит в своём отделе переходы, случившиеся до передачи сделки."""
    body = client.get(f"{BASE}/movement{PERIOD}&department=44").text
    assert "текущему" in body and "истории" in body


def test_movement_table_shows_the_opening_balance(client):
    """Без «было» остаток не с чем сверить — тождество потока не проверяется глазами."""
    body = client.get(f"{BASE}/movement{PERIOD}").text
    assert ">Было<" in body
    assert "осталось = было + вошло − вышло" in body


def test_bad_department_parameter_does_not_break_the_page(client):
    for value in ("abc", "", "all", "-1", "1e9"):
        response = client.get(f"{BASE}/movement{PERIOD}&department={value}")
        assert response.status_code == 200, f"department={value!r}"


def test_department_filter_does_not_leak_into_pages_that_ignore_it(client):
    """Иначе в адресе висит «отдел», страница его не применяет, и цифры спорят с URL."""
    body = client.get(f"{BASE}/movement{PERIOD}&department=44").text
    assert "/movement?" in body
    # Ссылка на «Движение» фильтр сохраняет, ссылки на прочие страницы — нет.
    movement_link = body[body.index('/dashboard/movement?'):][:200]
    people_link = body[body.index('/dashboard/people?'):][:200]
    assert "department=44" in movement_link
    assert "department=44" not in people_link
