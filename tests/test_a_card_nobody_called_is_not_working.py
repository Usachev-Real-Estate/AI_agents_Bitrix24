"""Карточка, по которой не звонили, не работается — как бы она ни стояла.

Движение по стадиям на этот вопрос не отвечает. Объект собственника в
рекламе месяцами стоит на «Поиске клиента», и по стадии не отличить того,
по кому брокер звонит каждую неделю, от того, о ком забыли в день заведения.
Ровно из-за этой неразличимости стадия исключена из подсчёта зависших:
норма там — отбор выживших. Исключение опиралось на обещание, что работу
покажут действия, и это его проверка.

Состояний три, а не два, и это главное, что здесь проверяется. Агентство
отмечает работу делами: «Связаться с клиентом» — 3 085 дел за год, «Встреча
с клиентом» — 401, а отдельных активностей «встреча» всего 68. Считать
только звонки и встречи — 511 молчащих карточек из 819; засчитать все
выполненные дела — 315. Ни одно из двух чисел не верно: в первом теряются
встречи, во втором «Отчет» и «Актуальный» становятся работой с клиентом.

Поэтому карточка бывает в трёх состояниях: разговор был, только отметка
(дело выполнено, записи разговора нет) и не трогали вовсе. Слить второе с
первым значит поверить отметке на слово, слить с третьим — обвинить того,
кто работал мимо портала.

Встреча узнаётся по теме дела, а тему приходится опускать в нижний регистр
в Python: SQLite делает это только с латиницей, и «Встреча» с «встреча» для
него разные строки.

Отдельно проверяется связка через контакт. Звонок висит на контакте чаще,
чем на сделке, и джойн, взявший только сделку, назвал бы молчащими тех, кто
звонил. Условие джойна здесь на две ветки через OR и ещё одно через AND —
место, где потерянные скобки дают тихо неверный ответ на всех строках
сразу, а таблица при этом рисуется как ни в чём не бывало.
"""

from datetime import datetime, timedelta, timezone

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import work
from schema import analytics_session
from scope import Scope, scoped_session

SELLERS = 0
BUYERS = 18
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
         user=10, created="2026-09-01T10:00:00+00:00", subject="", completed=1):
    conn.execute(
        """
        INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,
            provider_type_id, direction, subject, responsible_id, created_at,
            completed, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'x')
        """,
        (activity_id, owner_type, owner_id, kind, direction, subject, user,
         created, completed),
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
    assert result["talked"] == 1


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
    assert result["nothing"] == 1


def test_a_finished_task_is_a_mark_not_a_conversation(agency):
    """«Связаться с клиентом» выполнено — это отметка, а не запись разговора."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, kind="TODO", subject="Связаться с клиентом",
             completed=1)

    result = _work()
    assert result["talks"] == 0
    assert result["marked"] == 1, "брокер отметился — это не «не трогали»"
    assert result["nothing"] == 0
    assert result["talked"] == 0, "и не «разговаривал»: портал разговора не видел"


def test_an_unfinished_task_is_nothing_at_all(agency):
    """Запланированное дело — это намерение, а не работа."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, kind="TODO", subject="Связаться с клиентом",
             completed=0)

    assert _work()["nothing"] == 1


def test_a_meeting_hides_in_a_finished_task(agency):
    """Встречу заводят делом: «Встреча с клиентом» выполнено — это встреча."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, kind="TODO", subject="Встреча с клиентом",
             completed=1)
        _deal(conn, 2, contact=5002)
        _act(conn, 101, 2, 2, kind="TODO", subject="показ", completed=1)

    result = _work()
    assert result["meetings"] == 2, "регистр кириллицы обязан разбираться в Python"
    assert result["talked"] == 2
    assert result["marked"] == 0


def test_a_word_that_only_looks_like_a_meeting(agency):
    """«Договориться на рекламу объекта» — не встреча, как бы ни звучало."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, kind="TODO",
             subject="Договориться на рекламу объекта", completed=1)

    result = _work()
    assert result["meetings"] == 0
    assert result["marked"] == 1


def test_a_staff_task_is_not_client_work(agency):
    """Задачу сотруднику ставят внутри агентства — клиента она не касается."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, kind="TASKS_TASK", subject="Проверить документы",
             completed=1)
        _act(conn, 101, 3, CONTACT, kind="CONFIGURABLE", completed=1)

    assert _work()["nothing"] == 1


def test_an_incoming_call_is_counted_but_named(agency):
    """Клиент позвонил сам — карточка живая, но это не заслуга брокера."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, direction=1)
        _act(conn, 101, 2, 1, direction=2)

    result = _work()
    assert result["talks"] == 2
    assert result["outgoing"] == 1


def test_a_missed_call_is_not_a_conversation(agency):
    """Незавершённый входящий — это непринятый вызов. Никто не поговорил."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, direction=1, completed=0)

    result = _work()
    assert result["talks"] == 0
    assert result["nothing"] == 1, "не дозвонились — значит не работали"
    assert result["missed"] == 1
    assert result["missed_cards"] == 1
    assert result["never_returned"] == 1


def test_a_missed_call_answered_back_is_work(agency):
    """Пропустили и перезвонили — это работа, а не потеря."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, direction=1, completed=0)
        _act(conn, 101, 2, 1, direction=2)

    result = _work()
    assert result["missed_cards"] == 1
    assert result["never_returned"] == 0
    assert result["talked"] == 1


def test_a_closed_card_is_silent_by_right(agency):
    """Закрытая сделка молчит законно и в счёт заброшенных не идёт."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT, closed=1)
        _deal(conn, 2, contact=5002)

    result = _work()
    assert result["cards"] == 1
    assert result["nothing"] == 1


def test_an_old_conversation_is_silence_not_work(agency):
    """Звонили и бросили — это не «работают», и порог называется вслух."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, created="2026-01-10T10:00:00+00:00")
        _deal(conn, 2, contact=5002)
        _act(conn, 101, 2, 2, created=RECENT)

    result = _work(silent_days=14)
    assert result["silent"] == 1
    assert result["nothing"] == 0
    assert result["cold"] == 1, "брошенная и нетронутая складываются в одно число"
    assert result["silent_days"] == 14


def test_a_mark_alone_never_joins_the_cold_count(agency):
    """Отметка без разговора — вопрос к брокеру, а не приговор ему."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        _act(conn, 100, 2, 1, kind="TODO", subject="Связаться с клиентом",
             completed=1, created="2026-01-10T10:00:00+00:00")

    result = _work()
    assert result["marked"] == 1
    assert result["cold"] == 0, "человек мог звонить с личного телефона"


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
    assert by_user["Марат Абзалилов"]["nothing"] == 2
    assert by_user["Ольга Лобанова"]["nothing"] == 0
    # Худший сверху: разговор начинают с того, у кого лежит больше всего.
    assert result["by_user"][0]["name"] == "Марат Абзалилов"

    by_stage = {row["name"]: row for row in result["by_stage"]}
    assert by_stage["Поиск клиента"]["cards"] == 2
    assert by_stage["Назначение встречи"]["nothing"] == 1
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


def test_who_does_not_pick_up_is_counted_across_all_calls(agency):
    """Не берут трубку — считается по всем звонкам, а не по карточкам.

    По открытым карточкам пропущенных 63 при 5 169 по порталу: почти все
    непринятые не привязаны ни к одной открытой сделке. Считать их через
    карточки значит увидеть один процент проблемы — теряют на входе, до
    того как заводится сделка.
    """
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        for i in range(40):
            _act(conn, 200 + i, 3, 9999, direction=1,
                 completed=0 if i < 30 else 1, user=11)

    pickup = {row["name"]: row for row in _work()["pickup"]}
    assert pickup["Марат Абзалилов"]["missed"] == 30
    assert pickup["Марат Абзалилов"]["incoming"] == 40
    assert pickup["Марат Абзалилов"]["missed_share"] == 75.0


def test_a_handful_of_calls_does_not_top_the_table(agency):
    """Два пропущенных из пяти — это 40% и ничего не значит."""
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        for i in range(5):
            _act(conn, 300 + i, 3, 9999, direction=1,
                 completed=0 if i < 2 else 1, user=11)

    assert _work()["pickup"] == []


def test_a_robot_is_not_a_person_who_does_not_answer(agency):
    """Общая линия агентства — вопрос маршрутизации, а не дисциплины.

    На живых данных её строка первая: 929 непринятых из 1286. Число
    настоящее и с экрана не убирается — но в утреннем сообщении, которое
    зовёт поговорить с брокером, робота называть нельзя.
    """
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name,"
            " is_active, synced_at)"
            " VALUES (90, 'Агентство Недвижимости', 44, 'Битрикс', 1, 'x')"
        )
        _deal(conn, 1, contact=CONTACT)
        for i in range(40):
            _act(conn, 700 + i, 3, 9999, direction=1, completed=0, user=90)

    row = next(r for r in _work()["pickup"] if r["user_id"] == 90)
    assert row["missed"] == 40, "число остаётся на экране"
    assert row["service"] is True
    assert row["person"] is False


def test_a_departed_employee_is_not_in_the_rating(agency):
    """Уволенного в рейтинге звонков нет: спрашивать не с кого.

    Раньше он стоял здесь строкой с пометкой «не работает» — как находка:
    на человека, которого нет, всё ещё идут звонки. Решение агентства от
    10.09: уволенных на дашборде не показывать вовсе, и рейтинг брокеров
    — первое место, где строка про ушедшего только занимала место.

    Сами звонки при этом не исчезли: они остаются в числах портала, и
    вопрос «куда уходят входящие уволенного» — вопрос к настройке линии,
    а не к рейтингу тех, кто трубку берёт.
    """
    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name,"
            " is_active, synced_at)"
            " VALUES (91, 'Сергей Миронов', 44, 'Потерянные', 0, 'x')"
        )
        _deal(conn, 1, contact=CONTACT)
        for i in range(35):
            _act(conn, 800 + i, 3, 9999, direction=1, completed=0, user=91)

    assert 91 not in {row["user_id"] for row in _work()["pickup"]}


def test_the_back_office_is_not_judged_by_a_brokers_measure(agency):
    """Бэк-офису входящие сваливает маршрутизация, а не клиент.

    На живых данных бэк-офис занимает верх таблицы: 119 непринятых из 149,
    80% — хуже любого брокера. Число настоящее и с экрана не убирается, но
    спрос с него другой: брокеру звонит клиент, выбравший его, а в бэк-офис
    звонок попадает потому, что его туда направили. Поставить эти строки
    рядом в утреннем сообщении значит начать разговор не с тем человеком.
    """
    import plans

    with analytics_session() as conn:
        for user_id, name, dept_id, dept in (
            (20, "Ирина Брокова", plans.sales_department_ids()[0], "Кретов"),
            (21, "Анастасия Бэкова", 900, "Бэк-офис"),
        ):
            conn.execute(
                "INSERT INTO dim_user(user_id, name, department_id,"
                " department_name, is_active, synced_at) VALUES (?, ?, ?, ?, 1, 'x')",
                (user_id, name, dept_id, dept),
            )
        _deal(conn, 1, contact=CONTACT, user=20)
        for offset, user_id in ((900, 20), (1000, 21)):
            for i in range(40):
                _act(conn, offset + i, 3, 9990 + i, direction=1,
                     completed=0 if i < 32 else 1, user=user_id)

    pickup = {row["name"]: row for row in _work()["pickup"]}
    assert pickup["Анастасия Бэкова"]["missed"] == 32, "число остаётся на экране"
    assert pickup["Анастасия Бэкова"]["sells"] is False
    assert pickup["Анастасия Бэкова"]["person"] is False
    assert pickup["Ирина Брокова"]["person"] is True


def test_an_unreadable_department_list_does_not_silence_everyone(agency, monkeypatch):
    """Настройка не прочиталась — отбор не сужается, а не выкашивает всех.

    Пустой список означает «не знаем», а не «никто». Иначе сбой конфига
    молча убрал бы из сводки всех до единого, и выглядело бы это как
    «сегодня трубку берут все».
    """
    monkeypatch.setattr(work, "_sales_departments", lambda: ())

    with analytics_session() as conn:
        conn.execute(
            "INSERT INTO dim_user(user_id, name, department_id, department_name,"
            " is_active, synced_at) VALUES (21, 'Анастасия Бэкова', 900, 'Бэк', 1, 'x')"
        )
        _deal(conn, 1, contact=CONTACT, user=21)
        for i in range(40):
            _act(conn, 1100 + i, 3, 9990 + i, direction=1, completed=0, user=21)

    row = next(r for r in _work()["pickup"] if r["user_id"] == 21)
    assert row["person"] is True


# ── Окно звонков ───────────────────────────────────────────────────────
def test_calls_outside_the_window_are_not_counted(agency):
    """Таблица звонков обязана отвечать за выбранный период.

    Раньше она считалась за всю историю независимо от фильтра: на странице
    с чипом «7 дней» стояли числа за год, и понять это по ней было нельзя.
    Остальные блоки страницы к периоду не привязаны сознательно — «по чему
    не работают» вопрос не суточный, — но звонки за период спрашивают, и
    ответ должен быть про период.
    """
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        for i in range(40):
            _act(conn, 900 + i, 3, 9999, direction=1, completed=0, user=11,
                 created="2026-08-15T10:00:00+00:00")

    inside = _work(
        since="2026-08-01T00:00:00+00:00", until="2026-09-01T00:00:00+00:00")
    outside = _work(
        since="2026-09-01T00:00:00+00:00", until="2026-10-01T00:00:00+00:00")

    assert any(row["user_id"] == 11 for row in inside["pickup"])
    assert outside["pickup"] == []


def test_without_a_window_it_still_counts_everything(agency):
    """Умолчание — «за всё время»: утренняя сводка спрашивает именно так.

    Доля непринятых за один день скачет от случайных трёх звонков, а порог
    в 30 входящих рассчитан на длинное окно.
    """
    with analytics_session() as conn:
        _deal(conn, 1, contact=CONTACT)
        for i in range(40):
            _act(conn, 900 + i, 3, 9999, direction=1, completed=0, user=11,
                 created="2026-08-15T10:00:00+00:00")

    assert any(row["user_id"] == 11 for row in _work()["pickup"])
