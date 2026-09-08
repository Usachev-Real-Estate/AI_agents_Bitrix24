"""Утренняя сводка называет карточки, по которым не звонили.

Все остальные блоки сводки смотрят на сутки: что сдвинулось, что встало,
кто ушёл из работы. Этот — на состояние, и по-другому нельзя. Карточка, до
которой не дошли руки полгода, вчера ничем себя не проявила: суточное окно
не покажет её никогда, а вопрос «как отработали выданные контакты» —
ровно про неё.

Ведущее число — сколько карточек лежит без разговора, а не сколько звонков
сделано. Звонки складываются в большое число, даже когда все их сделал один
человек по трём карточкам, и «112 звонков за неделю» усыпляет ровно там,
где надо будить.

Брокеры называются долей, а не количеством. У одного в работе 52 карточки,
у другого 11, и «двадцать молчащих» значит у них разное. Порог по числу
карточек при этом обязателен: брокер с тремя карточками, из которых молчат
две, даёт 67% и возглавил бы список, ничего не значив.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import events as funnel
import pulse_digest
import work
from schema import analytics_session
from scope import Scope, scoped_session

SELLERS = 0
WINDOW = {"label": "вчера", "since": "2026-09-07T00:00:00+03:00",
          "until": "2026-09-08T00:00:00+03:00"}


def _deal(conn, deal_id, *, user, contact, stage="UC_FADPBF"):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, contact_id, is_deleted, synced_at)
        VALUES (?, ?, 0, ?, ?, 'ADV', 0, 'RUB', '2026-03-01T00:00:00+00:00',
                '2026-03-01T00:00:00+00:00', NULL, 0, 0, 0, ?, 0, 'x')
        """,
        (deal_id, f"Объект {deal_id}", stage, user, contact),
    )


def _call(conn, activity_id, deal_id, *, user, created="2026-09-07T10:00:00+00:00"):
    conn.execute(
        """
        INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,
            provider_type_id, direction, subject, responsible_id, created_at,
            completed, synced_at)
        VALUES (?, 2, ?, 'CALL', 2, '', ?, ?, 1, 'x')
        """,
        (activity_id, deal_id, user, created),
    )


@pytest.fixture
def agency(analytics_db):
    """Двое: у одного двенадцать карточек и молчат все, у другого — ни одной."""
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort,"
                     " synced_at) VALUES (0, 'Продавцы', 1, 10, 'x')")
        conn.execute("INSERT INTO dim_stage(stage_id, category_id, name, sort,"
                     " semantic, synced_at) VALUES ('UC_FADPBF', 0, 'Поиск клиента',"
                     " 50, 'in_progress', 'x'), ('NEW', 0, 'Назначение встречи', 10,"
                     " 'in_progress', 'x')")
        for user_id, name in ((11, "Марат Абзалилов"), (10, "Ольга Лобанова")):
            conn.execute(
                "INSERT INTO dim_user(user_id, name, department_id, department_name,"
                " is_active, synced_at) VALUES (?, ?, 44, 'Кретов', 1, 'x')",
                (user_id, name),
            )
        for i in range(1, 13):
            _deal(conn, i, user=11, contact=5000 + i)
        for i in range(21, 33):
            _deal(conn, i, user=10, contact=5000 + i, stage="NEW")
            _call(conn, 100 + i, i, user=10)
    return analytics_db


def _lines(**kwargs):
    with scoped_session(Scope.everything()) as conn:
        data = work.card_work(conn, [SELLERS], **kwargs)
    return "\n".join(pulse_digest._work_lines(data))


def test_the_count_of_untouched_cards_leads(agency):
    """Первое число — карточки без единого следа, а не сумма звонков."""
    text = _lines()

    assert "Не трогали вовсе 12 карточек из 24 (50%)" in text
    assert text.index("Не трогали") < text.index("больше всего лежит")


def test_the_broker_is_named_with_his_share(agency):
    """«12 из 12» отвечает на вопрос, а «12 молчащих» — нет."""
    text = _lines()

    assert "Марат Абзалилов 12/12" in text
    assert "Ольга Лобанова" not in text, "у кого всё отработано — не новость"


def test_a_broker_with_three_cards_does_not_lead_the_list(agency):
    """Две молчащие из трёх — это 67% и ничего не значит."""
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_user(user_id, name, department_id,"
                     " department_name, is_active, synced_at)"
                     " VALUES (12, 'Новичок Петров', 44, 'Кретов', 1, 'x')")
        for i in range(41, 44):
            _deal(conn, i, user=12, contact=5000 + i)
        _call(conn, 200, 41, user=12)

    assert "Новичок Петров" not in _lines()


def test_the_stage_where_it_piles_up_is_named(agency):
    """Стадия говорит, на каком шаге работа встала, — с неё и начинают."""
    assert "чаще всего на стадии «Поиск клиента»: 12 из 12" in _lines()


def test_an_old_conversation_is_a_separate_line(agency):
    """«Звонили и бросили» — не то же самое, что «не звонили вовсе»."""
    with analytics_session() as conn:
        _deal(conn, 50, user=10, contact=5050)
        _call(conn, 300, 50, user=10, created="2026-01-10T10:00:00+00:00")

    text = _lines(silent_days=14)
    assert "звонили, но дольше 14 дней назад" in text


def test_a_funnel_without_cards_says_nothing(analytics_db):
    """Пустой блок не печатается: сводка «событий нет» приучает не открывать."""
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort,"
                     " synced_at) VALUES (0, 'Продавцы', 1, 10, 'x')")

    assert _lines() == ""


def test_the_block_joins_the_sellers_section(agency):
    """Работа печатается внутри «Продавцов», а не отдельным письмом."""
    with scoped_session(Scope.everything()) as conn:
        data = work.card_work(conn, [SELLERS])

    # События берутся настоящие, а не собранные вручную: заглушка молча
    # разошлась бы с форматтером на первом же новом ключе.
    with scoped_session(Scope.everything()) as conn:
        events = funnel.funnel_events(
            conn, WINDOW["since"], WINDOW["until"], categories=[SELLERS],
        )

    text = pulse_digest.format_events(
        {}, events, WINDOW, sellers=None, sellers_work=data,
    )
    assert "🏠 Продавцы за вчера" in text
    assert "Не трогали вовсе" in text


def test_a_mark_without_a_call_is_its_own_line(agency):
    """«Дело закрыто, звонка нет» — вопрос к брокеру, а не приговор."""
    with analytics_session() as conn:
        for i in range(41, 44):
            _deal(conn, i, user=11, contact=5000 + i)
            conn.execute(
                """
                INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,
                    provider_type_id, direction, subject, responsible_id,
                    created_at, completed, synced_at)
                VALUES (?, 2, ?, 'TODO', NULL, 'Связаться с клиентом', 11,
                        '2026-09-07T10:00:00+00:00', 1, 'x')
                """,
                (400 + i, i),
            )

    text = _lines()
    assert "3 с отметкой без разговора" in text
    assert "Не трогали вовсе 12" in text, "отметка не приплюсовалась к нетронутым"


def test_the_missed_calls_get_their_own_line(agency):
    """Пропущенный вызов — единственная потеря, где клиент пришёл сам."""
    with analytics_session() as conn:
        for i in range(60):
            conn.execute(
                """
                INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,
                    provider_type_id, direction, subject, responsible_id,
                    created_at, completed, synced_at)
                VALUES (?, 3, 9999, 'CALL', 1, '', 11,
                        '2026-09-07T10:00:00+00:00', ?, 'x')
                """,
                (500 + i, 0 if i < 45 else 1),
            )

    assert "не берут трубку: Марат Абзалилов 45/60" in _lines()


def test_a_broker_who_mostly_answers_is_not_named(agency):
    """Уронил четверть входящих — это не строка в сводке директору."""
    with analytics_session() as conn:
        for i in range(60):
            conn.execute(
                """
                INSERT INTO fact_activity(activity_id, owner_type_id, owner_id,
                    provider_type_id, direction, subject, responsible_id,
                    created_at, completed, synced_at)
                VALUES (?, 3, 9999, 'CALL', 1, '', 11,
                        '2026-09-07T10:00:00+00:00', ?, 'x')
                """,
                (600 + i, 0 if i < 15 else 1),
            )

    assert "не берут трубку" not in _lines()
