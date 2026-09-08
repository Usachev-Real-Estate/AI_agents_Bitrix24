"""Карточка, по которой не звонили, не работается — как бы она ни стояла.

Движение по стадиям на этот вопрос не отвечает. Объект собственника в
рекламе месяцами стоит на «Поиске клиента», и по стадии не отличить того,
по кому брокер звонит каждую неделю, от того, о ком забыли в день заведения.
Ровно из-за этой неразличимости стадия исключена из подсчёта зависших:
норма там — отбор выживших. Исключение опиралось на обещание, что работу
покажут действия, и это его проверка.

Считаются только CALL и MEETING. TODO и TASKS_TASK — планирование, их на
портале 5 902 за год; засчитав их разговором, отчёт назвал бы лучшим
работником того, кто ведёт список дел и не звонит.

Отдельно проверяется связка через контакт. Звонок висит на контакте чаще,
чем на сделке, и джойн, взявший только сделку, назвал бы молчащими тех, кто
звонил. Условие джойна здесь на две ветки через OR и ещё одно через AND —
место, где потерянные скобки дают тихо неверный ответ на всех строках
сразу, а таблица при этом рисуется как ни в чём не бывало.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import work
from schema import analytics_session
from scope import Scope, scoped_session

SELLERS = 0
BUYERS = 18
CONTACT = 5001


def _deal(conn, deal_id, *, contact=None, user=10, stage="UC_FADPBF",
          category=SELLERS, closed=0, created="2026-06-01T00:00:00+00:00"):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, contact_id, is_deleted, synced_at)
        VALUES (?, ?, ?, ?, ?, 'ADV', 0, 'RUB', ?, ?, NULL, ?, 0, 0, ?, 0, 'x')
        """,
        (deal_id, f"Объект {deal_id}", category, stage, user, created, created,
         closed, contact),
    )


def _act(conn, activity_id, owner_type, owner_id, *, kind="CALL", direction=2,
         user=10, created="2026-09-01T10:00:00+00:00"):
    conn.execute(
        """
        INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,
            provider_type_id, direction, subject, responsible_id, created_at,
            completed, synced_at)
        VALUES (?, ?, ?, ?, ?, '', ?, ?, 1, 'x')
        """,
        (activity_id, owner_type, owner_id, kind, direction, user, created),
    )


@pytest.fixture
def agency(analytics_db):
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_pipeline(category_id, name, is_active, sort, synced_at)"
            " VALUES (0, 'Продавцы', 1, 10, 'x'), (18, 'Покупатели', 1, 20, 'x')"
        )
        conn.execute(
            "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
            " synced_at) VALUES ('UC_FADPBF', 0, 'Поиск клиента', 50,"
            " 'in_progress', 'x'), ('NEW', 0, 'Назначение встречи', 10,"
            " 'in_progress', 'x'), ('C18:NEW', 18, 'Подбор', 10, 'in_progress', 'x')"
        )
        for user_id, name in ((10, "Ольга Лобанова"), (11, "Марат Абзалилов")):
            conn.execute(
                "INSERT INTO dim_user(user_id, name, department_id, department_name,"
                " is_active, synced_at) VALUES (?, ?, 44, 'Кретов', 1, 'x')",
                (user_id, name),
            )
    return analytics_db


def _work(categories=(SELLERS,), **kwargs):
    with scoped_session(Scope.everything()) as conn:
        return work.card_work(conn, categories, **kwargs)


def test_a_call_on_the_contact_counts_for_the_card(agency):
    """Звонок висит на контакте — карточка всё равно отработана."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 3, CONTACT)

    result = _work()
    assert result["cards"] == 1
    assert result["talks"] == 1
    assert result["untouched"] == 0


def test_a_call_on_someone_elses_contact_stays_there(agency):
    """Скобки в джойне держат: чужой контакт не приносит разговоров."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 3, 9999)
        # Сделка с тем же номером, что и контакт: если ветки джойна
        # перепутать, этот звонок припишется карточке 1.
        _act(conn, 101, 2, CONTACT)

    result = _work()
    assert result["talks"] == 0
    assert result["untouched"] == 1


def test_a_task_is_not_a_conversation(agency):
    """TODO и TASKS_TASK — планирование, а не разговор с человеком."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, kind="TODO")
        _act(conn, 101, 2, 1, kind="TASKS_TASK")
        _act(conn, 102, 3, CONTACT, kind="CONFIGURABLE")

    result = _work()
    assert result["talks"] == 0
    assert result["untouched"] == 1, "аккуратный список дел — не работа с клиентом"


def test_an_incoming_call_is_counted_but_named(agency):
    """Клиент позвонил сам — карточка живая, но это не заслуга брокера."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, direction=1)
        _act(conn, 101, 2, 1, direction=2)

    result = _work()
    assert result["talks"] == 2
    assert result["outgoing"] == 1


def test_a_closed_card_is_silent_by_right(agency):
    """Закрытая сделка молчит законно и в счёт заброшенных не идёт."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT, closed=1)
        _deal(conn, 2, contact=5002)

    result = _work()
    assert result["cards"] == 1
    assert result["untouched"] == 1


def test_an_old_conversation_is_silence_not_work(agency):
    """Звонили и бросили — это не «работают», и порог называется вслух."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, created="2026-01-10T10:00:00+00:00")
        _deal(conn, 2, contact=5002)
        _act(conn, 101, 2, 2, created="2026-09-07T10:00:00+00:00")

    result = _work(silent_days=14)
    assert result["silent"] == 1
    assert result["untouched"] == 0
    assert result["cold"] == 1, "молчащая и нетронутая складываются в одно число"
    assert result["silent_days"] == 14


def test_the_report_names_the_broker_and_the_stage(agency):
    """Разбор по людям и по стадиям — оба по тем же правилам, что и итог."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT, user=10)
        _act(conn, 100, 2, 1)
        _deal(conn, 2, contact=5002, user=11)
        _deal(conn, 3, contact=5003, user=11, stage="NEW")

    result = _work()
    by_user = {row["name"]: row for row in result["by_user"]}
    assert by_user["Марат Абзалилов"]["cards"] == 2
    assert by_user["Марат Абзалилов"]["untouched"] == 2
    assert by_user["Ольга Лобанова"]["untouched"] == 0
    # Худший сверху: разговор начинают с того, у кого лежит больше всего.
    assert result["by_user"][0]["name"] == "Марат Абзалилов"

    by_stage = {row["name"]: row for row in result["by_stage"]}
    assert by_stage["Поиск клиента"]["cards"] == 2
    assert by_stage["Назначение встречи"]["untouched"] == 1
    # Стадии идут по порядку воронки, а не по числу карточек.
    assert [row["name"] for row in result["by_stage"]] == [
        "Назначение встречи", "Поиск клиента",
    ]


def test_the_oldest_forgotten_card_comes_first(agency):
    """Полгода без единого звонка — это не «ещё не дошли руки»."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT, created="2026-08-01T00:00:00+00:00")
        _deal(conn, 2, contact=5002, created="2026-02-01T00:00:00+00:00")
        _deal(conn, 3, contact=5003)
        _act(conn, 100, 2, 3)

    worst = _work()["worst"]
    assert [row["deal_id"] for row in worst] == [2, 1]


def test_a_shared_contact_is_counted_and_named(agency):
    """Контакт из двух воронок делает карточку отработаннее, чем она есть."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _deal(conn, 2, contact=CONTACT, category=BUYERS, stage="C18:NEW")
        _deal(conn, 3, contact=5003)

    result = _work()
    assert result["shared_contacts"] == 1, "перекос обязан быть на виду"


def test_another_funnel_is_not_this_report(agency):
    """Отчёт по продавцам не считает карточки покупателей."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _deal(conn, 2, contact=5002, category=BUYERS, stage="C18:NEW")

    assert _work()["cards"] == 1
    assert _work(categories=(BUYERS,))["cards"] == 1
