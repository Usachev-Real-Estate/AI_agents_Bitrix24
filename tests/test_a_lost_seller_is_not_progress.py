"""Уход собственника из работы — потеря, а не движение вперёд.

Воронка продавцов устроена так, что порядок стадий обманчив: «Отложенная
продажа» (60) и «Сделка проиграна» (70) стоят ВЫШЕ «Переговоров» (11) и
«Подготовки объекта в рекламу» (20). Считая продвижение по номеру стадии,
уход собственника в проигрыш попал бы в блок хороших новостей — и отчёт
каждый день радовался бы потерям.

Поэтому потерянные стадии узнаются по семантике из справочника, а не по
порядку. Отложенная продажа при этом считается потерей наравне с
проигрышем: в портале у неё семантика lost, и выдумывать третье состояние
там, где агентство завело два, значит спорить с агентством.

Главный вопрос собственника к этой воронке — где брокеры не дорабатывают.
Отвечает на него не число потерь, а стадия, С КОТОРОЙ ушли: потерять
собственника на переговорах и не доехать до него на встречу — две разные
недоработки, и разговор с брокером о них разный.
"""

from datetime import datetime, timedelta, timezone

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import events as funnel
from schema import analytics_session
from scope import Scope, scoped_session

NOW = datetime.now(timezone.utc)
SINCE = (NOW - timedelta(days=1)).isoformat()
UNTIL = NOW.isoformat()
SELLERS = 0

# Снимок боевого справочника на 08.09.2026.
STAGES = (
    ("NEW", "Назначение встречи", 10, "in_progress"),
    ("UC_KEOOG8", "Переговоры", 11, "in_progress"),
    ("FINAL_INVOICE", "Подготовка объекта в рекламу", 20, "in_progress"),
    ("WON", "Договор закрыт", 50, "won"),
    ("LOSE", "Отложенная продажа", 60, "lost"),
    ("APOLOGY", "Сделка проиграна", 70, "lost"),
)


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def _deal(conn, deal_id, stage, title, *, amount=0, closed=0):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, 0, ?, 1, 'CALL', ?, 'RUB', ?, ?, NULL, ?, 0, ?, 0, 'x')
        """,
        (deal_id, title, stage, amount, _ago(40), _ago(0), closed, closed),
    )


def _move(conn, deal_id, from_stage, to_stage, when):
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id,"
        " entered_at, left_at, duration_sec, seq)"
        " VALUES ('deal', ?, 0, ?, ?, ?, 100, 0)",
        (deal_id, from_stage, _ago(20), when),
    )
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id,"
        " entered_at, left_at, duration_sec, seq)"
        " VALUES ('deal', ?, 0, ?, ?, NULL, NULL, 1)",
        (deal_id, to_stage, when),
    )


@pytest.fixture
def sellers(analytics_db):
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort,"
                     " synced_at) VALUES (0, 'Продавцы', 1, 20, 'x')")
        for stage_id, name, sort, semantic in STAGES:
            conn.execute(
                "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
                " synced_at) VALUES (?, 0, ?, ?, ?, 'x')",
                (stage_id, name, sort, semantic),
            )
        conn.execute(
            "INSERT INTO dim_user(user_id, name, last_name, department_id,"
            " department_name, is_active, synced_at)"
            " VALUES (1, 'Мария Соколова', 'Соколова', 60, 'Кретов', 1, 'x')")
    return analytics_db


def _events():
    with scoped_session(Scope.everything()) as conn:
        return funnel.funnel_events(conn, SINCE, UNTIL, categories=[SELLERS])


# --------------------------------------------------------------------------

def test_a_loss_is_never_counted_as_progress(sellers):
    """«Проиграна» стоит по порядку выше «Переговоров» — и это ловушка."""
    with analytics_session() as conn:
        _deal(conn, 1, "APOLOGY", "Квартира на Ленина", closed=1)
        _move(conn, 1, "UC_KEOOG8", "APOLOGY", _ago(0.5))

    result = _events()

    assert result["advanced"]["deals"] == 0, "потеря — не движение вперёд"
    assert result["left_work"]["deals"] == 1


def test_a_deferred_sale_is_a_loss_too(sellers):
    """Отложенная продажа помечена в портале как lost — витрина не спорит."""
    with analytics_session() as conn:
        _deal(conn, 2, "LOSE", "Дом в Жостово", closed=1)
        _move(conn, 2, "FINAL_INVOICE", "LOSE", _ago(0.5))

    left = _events()["left_work"]

    assert left["deals"] == 1
    assert left["top"][0]["to_name"] == "Отложенная продажа"


def test_it_says_from_which_stage_they_were_lost(sellers):
    """Не число потерь, а этап, на котором не доработали."""
    with analytics_session() as conn:
        _deal(conn, 3, "APOLOGY", "Первая", closed=1)
        _move(conn, 3, "UC_KEOOG8", "APOLOGY", _ago(0.5))
        _deal(conn, 4, "APOLOGY", "Вторая", closed=1)
        _move(conn, 4, "UC_KEOOG8", "APOLOGY", _ago(0.6))
        _deal(conn, 5, "LOSE", "Третья", closed=1)
        _move(conn, 5, "NEW", "LOSE", _ago(0.7))

    left = _events()["left_work"]

    assert left["deals"] == 3
    assert left["by_stage"] == [("Переговоры", 2), ("Назначение встречи", 1)]


def test_a_move_between_working_stages_is_still_progress(sellers):
    """Ловушка не должна перекрыть настоящее движение."""
    with analytics_session() as conn:
        _deal(conn, 6, "FINAL_INVOICE", "Идёт в рекламу")
        _move(conn, 6, "UC_KEOOG8", "FINAL_INVOICE", _ago(0.5))

    result = _events()

    assert result["advanced"]["deals"] == 1
    assert result["left_work"]["deals"] == 0


def test_yesterdays_losses_only(sellers):
    """Триста пятьдесят четыре проигранных карточки — это не новость дня."""
    with analytics_session() as conn:
        _deal(conn, 7, "APOLOGY", "Позавчерашняя", closed=1)
        _move(conn, 7, "UC_KEOOG8", "APOLOGY", _ago(5))

    assert _events()["left_work"]["deals"] == 0


def test_the_chat_message_names_the_stage_and_the_broker(sellers):
    """Сообщение должно называть, кто и на каком этапе потерял собственника."""
    import pulse_digest

    with analytics_session() as conn:
        _deal(conn, 8, "APOLOGY", "Квартира на Ленина", closed=1)
        _move(conn, 8, "UC_KEOOG8", "APOLOGY", _ago(0.5))

    lines = "\n".join(pulse_digest._sellers_lines(_events()))

    assert "Ушла из работы 1 сделка" in lines
    assert "«Переговоры» 1" in lines
    assert "«Переговоры» → «Сделка проиграна»" in lines
    assert "Мария Соколова" in lines
