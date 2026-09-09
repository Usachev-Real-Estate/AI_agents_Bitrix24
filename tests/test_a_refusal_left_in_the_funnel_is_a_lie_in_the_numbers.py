"""Клиент отказал, а карточка числится в работе.

Это не про брокера, а про то, чему верит отчёт. Пайплайн складывается из
открытых карточек, и карточка с записью «не хочет продавать» продолжает
добавлять туда свою сумму. Директор видит 601,9 млн ₽ «в работе» и решает
по этому числу, а витрина в соседней таблице сама же его опровергает.

Проверяется такой совет цитатой, а не пересказом: «клиент сказал “есть
свой риэлтор”, а карточка на «Назначении встречи» третий месяц» — это
десять секунд в CRM. Поэтому отказ без слов сюда не попадает вовсе:
«модель считает, что клиент отказал» — не разговор, а спор о модели.

Отдельное место в сводке — не украшение. Первый раз мы это уже прошли:
просроченные обещания стояли рядом с событиями по сделкам и не могли
прозвучать ни разу, потому что рубли побеждают штуки. Здесь та же ловушка
в мягкой форме — холодных карточек у человека десятки, а отказов единицы,
и в общем ряду отказ не был бы высказан никогда.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import advice  # noqa: E402
import advice_rules  # noqa: E402
import work  # noqa: E402
from schema import analytics_session  # noqa: E402
from scope import Scope, scoped_session  # noqa: E402


def _ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat() + "T00:00:00+00:00"


def _card(conn, deal_id, *, refused=1, why="есть свой риэлтор", closed=0,
          user=10, name="Ирина Логутина", stage="UC_MEET", days=75):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
            assigned_by_id, source_id, opportunity, currency_id, date_create,
            date_modify, closedate, is_closed, is_won, is_lost, contact_id,
            is_deleted, synced_at)
        VALUES (?, ?, 0, ?, ?, 'ADV', 1000000, 'RUB',
                '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00',
                NULL, ?, 0, 0, 5000, 0, 'x')
        """,
        (deal_id, f"ЖК «Белоус {deal_id}»", stage, user, closed),
    )
    conn.execute("INSERT OR IGNORE INTO dim_user(user_id, name, is_active,"
                 " synced_at) VALUES (?, ?, 1, 'x')", (user, name))
    conn.execute(
        "INSERT INTO dim_stage(stage_id, category_id, name, sort, synced_at)"
        " VALUES (?, 0, 'Назначение встречи', 10, 'x')"
        " ON CONFLICT DO NOTHING",
        (stage,),
    )
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id,"
        " stage_id, seq, entered_at, left_at)"
        " VALUES ('deal', ?, 0, ?, 1, ?, NULL)",
        (deal_id, stage, _ago(days)),
    )
    conn.execute(
        """
        INSERT INTO fact_comment_read(entity_type, entity_id, source_hash,
            promised, promised_at, wait_until, refused, refused_why, ready,
            terms, read_at, prompt_version)
        VALUES ('deal', ?, 'h', '', NULL, NULL, ?, ?, '', '', '2026-09-09', '4')
        """,
        (deal_id, refused, why),
    )


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        _card(conn, 1)
    return analytics_db


def _rows():
    with scoped_session(Scope.everything()) as conn:
        return work.refused_in_work(conn, [0])


# ── Что попадает в список ──────────────────────────────────────────────
def test_a_refused_card_still_in_work_is_found(mart):
    """Основной случай: отказ записан, карточка открыта."""
    rows = _rows()

    assert [row["deal_id"] for row in rows] == [1]
    assert rows[0]["refused_why"] == "есть свой риэлтор"
    assert rows[0]["stage_name"] == "Назначение встречи"
    assert 74 <= rows[0]["days_in_stage"] <= 76


def test_a_closed_card_is_not_a_problem(analytics_db):
    """Карточку закрыли — воронка и запись согласны, спрашивать не о чем."""
    with analytics_session() as conn:
        _card(conn, 1, closed=1)
    assert _rows() == []


def test_a_refusal_without_words_is_not_reported(analytics_db):
    """Без цитаты остаётся «модель так считает» — это спор, а не разговор."""
    with analytics_session() as conn:
        _card(conn, 1, why="")
    assert _rows() == []


def test_a_card_without_a_refusal_is_left_alone(analytics_db):
    with analytics_session() as conn:
        _card(conn, 1, refused=0, why="")
    assert _rows() == []


def test_a_card_with_no_stage_history_is_still_reported(analytics_db):
    """Дырка в истории стадий не повод потерять противоречие."""
    with analytics_session() as conn:
        _card(conn, 1)
        conn.execute("DELETE FROM fact_stage_event WHERE entity_id = 1")

    rows = _rows()
    assert [row["deal_id"] for row in rows] == [1]
    assert rows[0]["days_in_stage"] is None


# ── Как это звучит ─────────────────────────────────────────────────────
def _advice(rows):
    return advice_rules.refusal_in_funnel(rows)[0]


def test_the_advice_quotes_the_client_and_names_the_stage(mart):
    item = _advice(_rows())

    assert item.who == "Ирина Логутина"
    assert item.value == 1
    assert "1 карточка с отказом клиента числится в работе" in item.title
    assert "«есть свой риэлтор»" in item.proof
    assert "«Назначение встречи»" in item.proof
    assert "Закройте" in item.action


def test_several_cards_become_one_conversation(analytics_db):
    """Пять карточек одного человека — один разговор, а не пять советов."""
    with analytics_session() as conn:
        for deal_id in range(1, 6):
            _card(conn, deal_id, days=30 + deal_id)

    items = advice_rules.refusal_in_funnel(_rows())

    assert len(items) == 1
    assert items[0].value == 5
    assert "5 карточек с отказом клиента числятся в работе" in items[0].title
    assert "и ещё 2" in items[0].action


# ── Место в сводке ─────────────────────────────────────────────────────
def test_the_refusal_and_the_cold_cards_are_both_heard():
    """Разные просьбы — разные места: на одном выживает только одна.

    Сначала я написал этот тест не про то: проверял, что отказ не заглушат
    холодные карточки, — и он проходил даже с общим местом. Веса
    разошлись в обратную сторону: у отказа len*100+days, у холодных просто
    число карточек, то есть в общем ряду замолчали бы КАРТОЧКИ. Ошибка та
    же самая, только зеркальная, и мимо неё легко пройти, проверяя лишь
    одну сторону.

    Поэтому проверяется свойство, а не победитель: слышно должно быть и
    то и другое. Это две разные просьбы — «закрой карточку, где клиент
    отказал» и «позвони по тем, где не звонили», — и выбирать между ними
    сводке незачем.
    """
    refusals = [{
        "deal_id": 1, "title": "ЖК «Белоус»", "stage_id": "UC_MEET",
        "stage_name": "Назначение встречи", "broker": "Ирина Логутина",
        "user_id": 10, "refused_why": "есть свой риэлтор", "ready": "",
        "opportunity": 1_000_000.0, "days_in_stage": 75.0,
    }]
    cold = {"by_user": [{
        "key": 11, "name": "Антон Кретов", "cold": 35, "cards": 55,
        "cold_share": 63.6, "nothing": 20, "silent": 15,
    }]}

    selection = advice.select(
        advice_rules.collect(sellers_work=cold, refusals=refusals), {},
    )

    rules = [item.rule for item in selection.advices]
    assert "refused_in_work" in rules, "отказ потерян"
    assert "broker_cold" in rules, "холодные карточки потеряны"
