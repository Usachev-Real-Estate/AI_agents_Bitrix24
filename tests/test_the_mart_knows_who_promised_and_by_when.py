"""Кто завёл дело и на какой срок — две вещи, которых витрина не знала.

Без срока нельзя ответить на вопрос «назначен ли следующий шаг», а на нём
держится и состояние клиента, и счёт просроченных обещаний: карточка, по
которой брокер поставил себе дело на пятницу, и карточка, по которой не
поставил ничего, в отчёте выглядели одинаково.

Без автора нельзя отличить работу брокера от работы за него. Комментарии
витрина различает по автору с самого начала — ровно затем, чтобы «по
карточке три записи» не выдавалось за работу ответственного. С делами было
не так: хранился только ответственный, то есть тот, НА кого дело завели, а
не тот, КТО его завёл. Звонок за брокера часто заводит колл-центр, дело
«перезвонить» ставит РОП — и в обоих случаях ответственным стоит брокер.

Оба поля портал отдаёт тем же ответом crm.activity.list, который витрина и
так запрашивает: два новых поля не стоят ни одного лишнего запроса. Это и
было причиной взять их в витрину, а не добирать отдельным проходом.

Отдельная история — «срока нет». Портал не оставляет DEADLINE пустым: делу
без срока он проставляет дату из далёкого будущего. Записать её как есть
значит получить портфель, у которого следующий шаг назначен у всех и
никогда не просрочен.
"""

import sqlite3

import analytics  # noqa: F401  — кладёт src/analytics на sys.path
import etl
import pytest
from schema import analytics_session


def _raw(activity_id, **extra):
    row = {
        "ID": str(activity_id), "OWNER_TYPE_ID": "2", "OWNER_ID": "101",
        "PROVIDER_TYPE_ID": "CALL", "SUBJECT": "Перезвонить",
        "RESPONSIBLE_ID": "32", "COMPLETED": "N",
        "CREATED": "2026-08-01T10:00:00+03:00",
    }
    row.update(extra)
    return row


# ── Срок дела ──────────────────────────────────────────────────────────
def test_a_deadline_reaches_the_mart():
    """Основное: срок, названный порталом, доезжает до строки."""
    row = etl._activity_row(_raw(1, DEADLINE="2026-08-15T18:00:00+03:00"), "x")

    assert row["deadline"] == "2026-08-15T15:00:00+00:00"


@pytest.mark.parametrize("raw", ["9999-12-31T00:00:00+03:00",
                                 "2100-01-01T00:00:00+03:00",
                                 "3000-06-15T12:00:00+03:00"])
def test_a_date_from_the_far_future_means_there_is_no_deadline(raw):
    """«Срока нет» портал изображает датой, а не пустотой.

    Сравнивать с конкретным значением нельзя: оно зависит от версии портала
    и часового пояса. Отсекается всё, что дальше 2090 года.

    Случай 2100-01-01 здесь не для ровного счёта: первая редакция отсечки
    сравнивала первые четыре символа строки, и этот тест её уронил. По
    Москве 2100-01-01 — это 2099-12-31 в UTC, то есть «далёкое будущее»
    проходило проверку как настоящий срок. Сравнение моментов от сдвига
    часового пояса не зависит.
    """
    assert etl._activity_row(_raw(1, DEADLINE=raw), "x")["deadline"] is None


@pytest.mark.parametrize("raw", ["", None, "не дата"])
def test_a_missing_deadline_is_not_invented(raw):
    """Пустое и мусорное — это отсутствие срока, а не ноль и не сегодня."""
    assert etl._activity_row(_raw(1, DEADLINE=raw), "x")["deadline"] is None


def test_a_real_deadline_is_not_mistaken_for_the_sentinel():
    """Граница отсечки не должна съедать настоящие сроки.

    У агентства встречаются долгие договорённости — «вернуться через год»,
    «после сдачи дома». Порог в 2100 году оставляет для них весь запас.
    """
    row = etl._activity_row(_raw(1, DEADLINE="2031-03-01T09:00:00+03:00"), "x")

    assert row["deadline"] == "2031-03-01T06:00:00+00:00"


# ── Автор дела ─────────────────────────────────────────────────────────
def test_the_author_is_kept_apart_from_the_responsible():
    """Тот, КТО завёл дело, и тот, НА кого завели, — разные люди."""
    row = etl._activity_row(_raw(1, AUTHOR_ID="44", RESPONSIBLE_ID="32"), "x")

    assert (row["author_id"], row["responsible_id"]) == (44, 32)


def test_an_activity_without_an_author_is_stored_all_the_same():
    """Старые дела приезжают без автора — это не повод потерять строку."""
    row = etl._activity_row(_raw(1), "x")

    assert row["author_id"] is None
    assert row["activity_id"] == 1


# ── Оба поля запрашиваются у портала ───────────────────────────────────
def test_the_portal_is_asked_for_both_fields():
    """Колонка, которую не просят у портала, остаётся пустой навсегда.

    Проверка дешёвая и ловит самую обидную ошибку: поле добавлено в схему
    и в разбор ответа, а в список запрашиваемых — нет.
    """
    assert "DEADLINE" in etl.ACTIVITY_SELECT
    assert "AUTHOR_ID" in etl.ACTIVITY_SELECT


# ── Схема и миграция должны сойтись ────────────────────────────────────
def test_a_fresh_mart_gets_both_columns_without_any_migration():
    """DDL обязан нести колонки сам, а не полагаться на миграцию.

    Миграция ALTER TABLE ADD COLUMN выполняется и тогда, когда в DDL
    колонки нет: она просто добавит её. Поэтому удаление колонки из DDL
    ничего не ломает в тестах — и уезжает незамеченным, а расходиться
    этим двум местам нельзя.

    В этом проекте такое уже было: боевая витрина числилась шестой версией
    схемы, а колонки из шестой в ней не было, и миграция под неё приехала
    раньше номера. Пока DDL гнался безусловно, это сходило с рук; с
    проверкой версии база осталась бы без колонки навсегда.
    """
    from analytics.schema import _DDL

    conn = sqlite3.connect(":memory:")
    for statement in _DDL:
        conn.execute(statement)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fact_activity)")}
    conn.close()

    assert {"deadline", "author_id"} <= columns


# ── Доезд до таблицы ───────────────────────────────────────────────────
def test_both_fields_survive_the_round_trip(analytics_db):
    """Схема, запрос и разбор должны сойтись — по отдельности они уже сошлись."""
    with analytics_session() as conn:
        conn.execute(etl._ACTIVITY_UPSERT, etl._activity_row(
            _raw(1, AUTHOR_ID="44", DEADLINE="2026-08-15T18:00:00+03:00"), "x",
        ))
        stored = conn.execute(
            "SELECT author_id, responsible_id, deadline FROM fact_activity"
            " WHERE activity_id = 1"
        ).fetchone()

    assert stored["author_id"] == 44
    assert stored["responsible_id"] == 32
    assert stored["deadline"] == "2026-08-15T15:00:00+00:00"


def test_an_update_overwrites_both_fields(analytics_db):
    """Срок переносят, а дело остаётся тем же: upsert обязан его обновить."""
    with analytics_session() as conn:
        conn.execute(etl._ACTIVITY_UPSERT, etl._activity_row(
            _raw(1, AUTHOR_ID="44", DEADLINE="2026-08-15T18:00:00+03:00"), "x",
        ))
        conn.execute(etl._ACTIVITY_UPSERT, etl._activity_row(
            _raw(1, AUTHOR_ID="7", DEADLINE="2026-09-01T12:00:00+03:00"), "y",
        ))
        stored = conn.execute(
            "SELECT author_id, deadline FROM fact_activity WHERE activity_id = 1"
        ).fetchone()

    assert stored["author_id"] == 7
    assert stored["deadline"] == "2026-09-01T09:00:00+00:00"
