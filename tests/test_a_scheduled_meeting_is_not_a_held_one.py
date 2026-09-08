"""Назначенная встреча — не проведённая, и путать их дорого.

Из 68 активностей «встреча» в портале завершены 26. Остальные 42 —
назначенные, и по ним неизвестно даже, состоялись ли они. Пока счёт не
смотрел на завершённость, все 68 шли в «разговор был»: карточка, по которой
брокер только запланировал встречу и ничего не сделал, числилась
отработанной.

Ценное состояние здесь одно — срок прошёл, а «выполнено» не поставлено.
Либо встреча не состоялась, либо о ней не отчитались, и оба ответа стоят
разговора с брокером. Проведённые вопросов не вызывают, назначенные на
будущее тем более.

Встречи без даты считаются отдельно и печатаются рядом. По ним просрочку не
отличить вовсе, и молчать о размере слепого пятна значит выдать часть
картины за всю: если их много, признак не годится и дату придётся брать из
поля карточки, а не из дела.
"""

from datetime import datetime, timedelta, timezone

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import work
from schema import analytics_session
from scope import Scope, scoped_session

SELLERS = 0
CONTACT = 5001
NOW = datetime.now(timezone.utc)
PAST = (NOW - timedelta(days=3)).isoformat()
FUTURE = (NOW + timedelta(days=3)).isoformat()


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


def _meet(conn, activity_id, deal_id, *, kind="TODO", subject="Встреча с клиентом",
          completed=0, start=None, user=10):
    conn.execute(
        """
        INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,
            provider_type_id, direction, subject, responsible_id, created_at,
            start_time, completed, synced_at)
        VALUES (?, 2, ?, ?, NULL, ?, ?, '2026-09-01T10:00:00+00:00', ?, ?, 'x')
        """,
        (activity_id, deal_id, kind, subject, user, start, completed),
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
        for user_id, name in ((10, "Ольга Лобанова"), (11, "Марат Абзалилов")):
            conn.execute(
                "INSERT INTO dim_user(user_id, name, department_id, department_name,"
                " is_active, synced_at) VALUES (?, ?, 44, 'Кретов', 1, 'x')",
                (user_id, name),
            )
    return analytics_db


def _work():
    with scoped_session(Scope.everything()) as conn:
        return work.card_work(conn, [SELLERS])


def test_a_scheduled_meeting_does_not_count_as_a_conversation(agency):
    """Главная проверка: намерение не заносится в актив как сделанное."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _meet(conn, 100, 1, start=FUTURE)

    result = _work()
    assert result["meetings"] == 0, "встреча ещё не состоялась"
    assert result["meetings_planned"] == 1
    assert result["talked"] == 0
    assert result["nothing"] == 1


def test_an_overdue_meeting_is_the_one_worth_asking_about(agency):
    """Срок прошёл, дело не закрыто — либо не было, либо не отчитались."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _meet(conn, 100, 1, start=PAST)

    result = _work()
    assert result["meetings_overdue"] == 1
    assert result["meetings_planned"] == 0
    assert result["talked"] == 0


def test_a_held_meeting_is_a_conversation(agency):
    """Обратная проверка: выполненная встреча по-прежнему работа."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _meet(conn, 100, 1, completed=1, start=PAST)

    result = _work()
    assert result["meetings"] == 1
    assert result["talked"] == 1
    assert result["meetings_overdue"] == 0


def test_the_meeting_activity_obeys_the_same_rule(agency):
    """Отдельная активность «встреча» считается так же, как дело.

    В портале их 68, из них не завершено 42 — больше половины. Пока счёт
    брал их все, эти 42 шли в «разговор был».
    """
    with analytics_session() as conn:
        _deal(conn, 1)
        _meet(conn, 100, 1, kind="MEETING", subject="", start=PAST)
        _deal(conn, 2, contact=5002)
        _meet(conn, 101, 2, kind="MEETING", subject="", completed=1, start=PAST)

    result = _work()
    assert result["meetings"] == 1
    assert result["meetings_overdue"] == 1
    assert result["talked"] == 1


def test_a_meeting_without_a_date_is_counted_apart(agency):
    """Без срока просрочку не отличить — и размер слепого пятна назван."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _meet(conn, 100, 1, start=None)

    result = _work()
    assert result["meetings_undated"] == 1
    assert result["meetings_overdue"] == 0
    assert result["meetings_planned"] == 0


def test_a_plain_task_never_becomes_a_meeting(agency):
    """«Связаться с клиентом» просрочено — это не просроченная встреча."""
    with analytics_session() as conn:
        _deal(conn, 1)
        _meet(conn, 100, 1, subject="Связаться с клиентом", start=PAST)

    result = _work()
    assert result["meetings_overdue"] == 0
    assert result["nothing"] == 1, "невыполненное дело — это намерение"


def test_the_overdue_meetings_are_counted_per_broker(agency):
    """Разговор начинают с того, у кого их больше."""
    with analytics_session() as conn:
        for deal_id in (1, 2, 3):
            _deal(conn, deal_id, user=11, contact=5000 + deal_id)
            _meet(conn, 100 + deal_id, deal_id, start=PAST, user=11)
        _deal(conn, 4, user=10, contact=5004)
        _meet(conn, 104, 4, start=PAST)

    by_user = {row["name"]: row for row in _work()["by_user"]}
    assert by_user["Марат Абзалилов"]["meetings_overdue"] == 3
    assert by_user["Ольга Лобанова"]["meetings_overdue"] == 1
