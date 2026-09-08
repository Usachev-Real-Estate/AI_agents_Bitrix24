"""Воронка продавцов на «Сделках» не показывает денег.

Комиссию в карточке собственника не ведут: там держат объект, звонят,
готовят в рекламу — работа, а не выручка. Пока страница рисовала денежные
блоки для любой воронки, у продавцов в них стояли нули: «выиграно 0 ₽»,
«средний чек 0 ₽», прогноз на ноль рублей.

Ноль в денежной колонке — это утверждение «сделок на ноль рублей», а не
«поле не заполняют». Оно неотличимо на вид от настоящего провала и ровно
так и читается: отдел продавцов выглядит отделом, не заработавшим ничего.

Поэтому колонки не пустеют, а исчезают целиком, и страница один раз
говорит почему.
"""

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
BUYERS = "?start=2026-08-01&end=2026-08-31&category=18"
SELLERS = "?start=2026-08-01&end=2026-08-31&category=0"


@pytest.fixture
def client(analytics_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (18, 'Покупатели', 1, 10, 'x'), (0, 'Продавцы', 1, 20, 'x')"
        )
        conn.execute(
            "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
            " synced_at) VALUES ('C18:NEW', 18, 'Подбор', 10, 'in_progress', 'x'),"
            " ('UC_FADPBF', 0, 'Поиск клиента', 50, 'in_progress', 'x')"
        )
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name,"
            " is_active, synced_at) VALUES (32, 'Иван Петров', 44, 'Кретов', 1, 'x')"
        )
        conn.execute(
            "INSERT INTO dim_source(source_id, name, synced_at)"
            " VALUES ('ADV', 'Реклама', 'x')"
        )
        for deal_id, category, stage in ((501, 18, "C18:NEW"), (502, 0, "UC_FADPBF")):
            conn.execute(
                """
                INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
                    assigned_by_id, source_id, opportunity, currency_id, date_create,
                    date_modify, closedate, is_closed, is_won, is_lost, is_deleted,
                    synced_at)
                VALUES (?, ?, ?, ?, 32, 'ADV', 0, 'RUB', '2026-08-02T00:00:00+00:00',
                        '2026-08-02T00:00:00+00:00', NULL, 0, 0, 0, 0, 'x')
                """,
                (deal_id, f"Карточка {deal_id}", category, stage),
            )
        conn.execute(
            "INSERT INTO analytics_meta(key, value) VALUES ('window_since', '2025-08-01')"
        )

    application = create_app()
    store.create_user(LOGIN, PASSWORD, "Руководитель", role="admin")
    session = TestClient(application, follow_redirects=False)
    session.get(f"{BASE}/login")
    assert session.post(f"{BASE}/login", data={
        "username": LOGIN, "password": PASSWORD,
        "csrf_token": session.cookies.get("dash_csrf"), "next": "",
    }).status_code == 303
    return session


def test_the_sellers_funnel_hides_every_money_block(client):
    body = client.get(f"{BASE}/deals{SELLERS}").text

    assert "Комиссию в этой воронке не ведут" in body
    for block in ("Выиграно денег", "Средний чек", "Взвешенный прогноз",
                  "Деньги воронки", "Сумма открытых"):
        assert block not in body, f"денежный блок «{block}» остался у продавцов"


def test_the_buyers_funnel_keeps_its_money(client):
    """Проверка обратная: правило не должно снести деньги там, где они есть."""
    body = client.get(f"{BASE}/deals{BUYERS}").text

    assert "Комиссию в этой воронке не ведут" not in body
    for block in ("Выиграно денег", "Взвешенный прогноз", "Деньги воронки"):
        assert block in body


def test_the_source_table_drops_its_money_columns_too(client):
    """Разрез по источнику подчиняется тому же правилу, что и вся страница."""
    sellers = client.get(f"{BASE}/deals{SELLERS}").text
    buyers = client.get(f"{BASE}/deals{BUYERS}").text

    assert "Источники" in sellers and "Реклама" in sellers
    assert "На сделку" not in sellers
    assert "На сделку" in buyers and "Комиссия" in buyers


def test_a_source_row_opens_its_cards(client):
    """Число, по которому кликнули, обязано раскладываться до карточек."""
    body = client.get(f"{BASE}/deals{BUYERS}").text

    assert "source=ADV" in body
