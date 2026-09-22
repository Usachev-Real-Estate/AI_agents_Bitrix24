"""Комментарий брокера — это запись работы, а не примечание к ней.

Звонок говорит «был контакт», стадия — «карточка сдвинулась». Ни то, ни
другое не отвечает на вопрос, что с клиентом. Отвечает комментарий: «Был
показ 08.09, ушли думать, подбираю ещё объекты», «бюджета не хватает, в
середине июля будет известен бонус», «не продаёт и не покупает». Это и есть
разговор — записанный текстом, а не карточкой звонка.

Поэтому запись брокера считается следом работы наравне с разговором.
Требовать вдобавок карточку звонка значит наказывать за способ ведения
записей, а не за работу — и назвать заброшенной карточку, по которой брокер
всё описал словами.

Автоматическая запись — не след ничего. «Новое обращение: Звонок с Cian…»
пишет робот в момент поступления заявки, до всякой работы. Засчитать её
значит повторить ошибку с отметкой вместо разговора, только теперь в пользу
того, кто не сделал ничего.
"""

from datetime import datetime, timedelta, timezone

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import work
from schema import analytics_session
from scope import Scope, scoped_session

SELLERS = 0
CONTACT = 5001


# «Недавно» — это отсчёт от сегодняшнего дня, а не записанное число.
#
# Здесь стояло 2026-09-07. К 21 сентября эта дата пришлась ровно на границу
# порога silent_days=14, и тест начал падать от хода часов: утром проходил,
# днём краснел. Проверяется-то не «ровно четырнадцать дней», а «запись
# свежая» — значит и дата обязана быть относительной, с запасом от границы.
#
# Заморозить «сегодня» нельзя: молчание считается в SQL через
# julianday('now'), и подменить его из Python не за что. Значит двигаться
# должны данные теста, а не время.
RECENT = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
    "%Y-%m-%dT10:00:00+00:00")


def _deal(conn, deal_id, *, user=10, contact=CONTACT):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, contact_id, is_deleted, synced_at)
        VALUES (?, ?, 0, 'UC_FADPBF', ?, 'ADV', 0, 'RUB',
                '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00',
                NULL, 0, 0, 0, ?, 0, 'x')
        """,
        (deal_id, f"Объект {deal_id}", user, contact),
    )


def _note(conn, comment_id, deal_id, body, *, auto=0,
          created=None, author=10):
    conn.execute(
        "INSERT INTO fact_comment(comment_id, entity_type, entity_id, author_id,"
        " body, is_auto, created_at, synced_at)"
        " VALUES (?, 'deal', ?, ?, ?, ?, ?, 'x')",
        (comment_id, deal_id, author, body, auto, created or RECENT),
    )


@pytest.fixture
def agency(analytics_db):
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (0, 'Продавцы', 1, 10, 'x')"
        )
        conn.execute(
            "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
            " synced_at) VALUES ('UC_FADPBF', 0, 'Поиск клиента', 50,"
            " 'in_progress', 'x')"
        )
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name,"
            " is_active, synced_at) VALUES (10, 'Ольга Лобанова', 44, 'Кретов', 1, 'x')"
        )
    return analytics_db


def _work(**kwargs):
    with scoped_session(Scope.everything()) as conn:
        return work.card_work(conn, [SELLERS], **kwargs)


def test_a_written_note_is_a_trace_of_work(agency):
    """Главная проверка: брокер описал словами — карточка не заброшена."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _note(conn, 100, 1, "Связалась с клиентом не продает и не покупает")

    result = _work()
    assert result["nothing"] == 0, "запись брокера — это работа"
    assert result["talked"] == 1
    assert result["notes"] == 1
    assert result["with_notes"] == 1


def test_a_robot_note_is_a_trace_of_nothing(agency):
    """«Новое обращение» пишет робот в момент заявки, до всякой работы."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _note(conn, 100, 1,
              "Новое обращение: Звонок с Cian · тел. +7… комиссия 3% = 1 707 000 ₽",
              auto=1)

    result = _work()
    assert result["nothing"] == 1, "к карточке никто не притрагивался"
    assert result["notes"] == 0
    assert result["auto_notes"] == 1


def test_a_fresh_note_keeps_the_card_from_going_silent(agency):
    """«Ждём фотографии» вчера — карточка не молчит, сколько бы ни было звонков."""
    with analytics_session() as conn:
        _deal(conn, 1)
        conn.execute(
            """
            INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,
                provider_type_id, direction, subject, responsible_id, created_at,
                start_time, completed, synced_at)
            VALUES (200, 2, 1, 'CALL', 2, '', 10, '2026-01-10T10:00:00+00:00',
                    NULL, 1, 'x')
            """
        )
        _note(conn, 100, 1, "жду фотографии от Станислава и выложим на циан",
              created=RECENT)

    assert _work(silent_days=14)["silent"] == 0


def test_a_note_beats_a_bare_mark(agency):
    """В отметке сказано «дело закрыто», в записи — что с клиентом."""
    with analytics_session() as conn:
        _deal(conn, 1)
        conn.execute(
            """
            INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,
                provider_type_id, direction, subject, responsible_id, created_at,
                start_time, completed, synced_at)
            VALUES (200, 2, 1, 'TODO', NULL, 'Связаться с клиентом', 10,
                    ?, NULL, 1, 'x')
            """,
            (RECENT,),
        )
        _note(conn, 100, 1, "продает через риелтора, показ тоже через него")

    result = _work()
    assert result["talked"] == 1
    assert result["marked"] == 0, "запись сильнее отметки, а не рядом с ней"


def test_a_comment_on_a_foreign_deal_stays_there(agency):
    """Комментарий сужается по карточке: чужая сделка своих записей не отдаёт."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _note(conn, 100, 999, "запись по чужой карточке")

    assert _work()["notes"] == 0
