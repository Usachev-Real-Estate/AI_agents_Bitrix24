"""Брокер написал после обещания — обещание больше не висит.

Правило агентства от 15.09. Обещание считается закрытым, когда брокер
вернулся к карточке: написал результат, «не дозвонился», «перенесли» —
что угодно. Спрашивать с него за молчание, которого не было, значит
предъявить упрёк на пустом месте, а один такой упрёк обесценивает
соседние верные строки.

В коде это правило БЫЛО ЗАПИСАНО В ОПИСАНИИ ФУНКЦИИ, но не в запросе:
«обещание, после которого появилась новая запись, снимается само —
отпечаток изменился, карточку прочитали заново». Держалось оно на
перечтении, а перечтение отстаёт: читатель берёт 400 карточек за ночь из
тысячи с лишним, и очередь идёт по кругу. Брокер закрывал обещание
сегодня, а дашборд упрекал его этим ещё двое-трое суток.

Витрина знает ответ без модели: комментарии обновляет ETL каждые
пятнадцать минут. Если человеческая запись появилась ПОСЛЕ чтения,
разбор устарел — что в ней написано, неизвестно, и обвинять не на чем.
Перечтение потом скажет своё слово, а до тех пор карточка молчит.

Сравнение через julianday, а не строкой: время записи приходит из портала
со смещением +03:00, время чтения — в UTC, и лексикографически «10:00+03»
больше «08:30+00», хотя случилось раньше.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import work  # noqa: E402
from schema import analytics_session  # noqa: E402
from scope import Scope, scoped_session  # noqa: E402

CAT = 18
BROKER = 11
DEAL = 300
TODAY = "2026-09-15"
READ_AT = "2026-09-10T08:30:00+00:00"


def _at(day: str, time: str = "10:00:00", offset: str = "+00:00") -> str:
    return f"{day}T{time}{offset}"


def _card(conn, deal_id, *, refused=0):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
            assigned_by_id, source_id, opportunity, currency_id, date_create,
            date_modify, closedate, is_closed, is_won, is_lost, contact_id,
            is_deleted, synced_at)
        VALUES (?, ?, 18, 'C18:SHOW', ?, 'CALL', 500000, 'RUB',
                '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
                NULL, 0, 0, 0, NULL, 0, 'x')
        """,
        (deal_id, f"Квартира {deal_id}", BROKER))
    conn.execute(
        """
        INSERT INTO fact_comment_read(entity_type, entity_id, source_hash,
            promised, promised_at, wait_until, refused, refused_why, ready,
            terms, read_at, prompt_version)
        VALUES ('deal', ?, 'h', 'Позвонить собственнице', '2026-09-12', NULL,
                ?, ?, '', '', ?, '5')
        """,
        (deal_id, refused, "дорого" if refused else "", READ_AT))


def _note(conn, deal_id, at, *, auto=0, body="Дозвонился, договорились"):
    conn.execute(
        "INSERT INTO fact_comment(comment_id, entity_type, entity_id,"
        " author_id, body, is_auto, created_at, synced_at)"
        " VALUES (?, 'deal', ?, ?, ?, ?, ?, 'x')",
        (deal_id * 100 + abs(hash(at)) % 90, deal_id, BROKER, body, auto, at))


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active,"
                     " sort, synced_at) VALUES (18, 'Покупатели', 1, 10, 'x')")
        conn.execute("INSERT INTO dim_stage(stage_id, category_id, name, sort,"
                     " semantic, synced_at)"
                     " VALUES ('C18:SHOW', 18, 'Показ', 20, 'in_progress', 'x')")
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id,"
            " department_name, is_active, synced_at)"
            " VALUES (?, 'Марат Абзалилов', 44, 'Отдел', 1, 'x')", (BROKER,))
    return analytics_db


def _overdue():
    with scoped_session(Scope.everything()) as conn:
        return [row["deal_id"] for row in work.promises(conn, [CAT], today=TODAY)]


def _refusals():
    with scoped_session(Scope.everything()) as conn:
        return [row["deal_id"] for row in work.refused_in_work(conn, [CAT])]


# ── Обещания ───────────────────────────────────────────────────────────
def test_silence_after_the_deadline_is_still_overdue(mart):
    """Опора всей проверки: без новой записи упрёк законен."""
    with analytics_session() as conn:
        _card(conn, DEAL)
        _note(conn, DEAL, _at("2026-09-09"), body="Позвонить собственнице")

    assert _overdue() == [DEAL]


def test_a_note_written_after_the_reading_closes_it(mart):
    """Брокер вернулся к карточке — спрашивать не о чем."""
    with analytics_session() as conn:
        _card(conn, DEAL)
        _note(conn, DEAL, _at("2026-09-09"), body="Позвонить собственнице")
        _note(conn, DEAL, _at("2026-09-13"))

    assert _overdue() == []


def test_a_note_written_before_the_reading_changes_nothing(mart):
    """Записи до чтения модель уже видела и всё равно назвала обещание."""
    with analytics_session() as conn:
        _card(conn, DEAL)
        _note(conn, DEAL, _at("2026-09-08"))
        _note(conn, DEAL, _at("2026-09-09"), body="Позвонить собственнице")

    assert _overdue() == [DEAL]


def test_a_robot_note_does_not_close_a_promise(mart):
    """«Новое обращение: звонок с Cian» — не возвращение к карточке."""
    with analytics_session() as conn:
        _card(conn, DEAL)
        _note(conn, DEAL, _at("2026-09-09"), body="Позвонить собственнице")
        _note(conn, DEAL, _at("2026-09-13"), auto=1,
              body="Новое обращение: Звонок с Cian")

    assert _overdue() == [DEAL]


def test_the_comparison_survives_a_moscow_offset(mart):
    """Смещение в записи и UTC в чтении нельзя сравнивать строкой.

    Запись 10.09 в 10:00 по Москве — это 07:00 UTC, то есть РАНЬШЕ чтения
    в 08:30 UTC. Лексикографически же «10:00:00+03:00» больше
    «08:30:00+00:00», и обещание снялось бы по записи, сделанной до
    чтения.
    """
    with analytics_session() as conn:
        _card(conn, DEAL)
        _note(conn, DEAL, _at("2026-09-09"), body="Позвонить собственнице")
        _note(conn, DEAL, _at("2026-09-10", "10:00:00", "+03:00"))

    assert _overdue() == [DEAL], "запись сделана ДО чтения, обещание висит"


# ── Отказы ─────────────────────────────────────────────────────────────
def test_a_refusal_is_closed_by_a_later_note_too(mart):
    """Тот же вопрос — тот же ответ: «клиент передумал» снимает отказ."""
    with analytics_session() as conn:
        _card(conn, DEAL, refused=1)
        _note(conn, DEAL, _at("2026-09-09"), body="Сказал дорого")

    assert _refusals() == [DEAL]

    with analytics_session() as conn:
        _note(conn, DEAL, _at("2026-09-13"), body="Передумал, выходим на сделку")

    assert _refusals() == []
