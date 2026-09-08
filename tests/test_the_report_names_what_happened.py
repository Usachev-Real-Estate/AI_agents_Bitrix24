"""Ежедневный разбор говорит о событиях, а не о процентах.

Агентство закрывает 19 сделок за квартал — полторы в неделю. На таких числах
недельная конверсия шум: одна сделка меняет её вдвое, и отчёт каждый день
кричал бы о просадке, которой нет.

Поэтому здесь считаются события: сделка встала, сделка сдвинулась, сделка
вернулась назад, человек неделю ничего не двигал. Каждое проверяемо и каждое
что-то значит на любом объёме.

Второе правило, проверенное отдельно: пустой блок не печатается. Отчёт,
ежедневно сообщающий «ничего не произошло», перестают открывать раньше, чем
в нём появится что-то важное.
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
KRETOV, VOLKOVA = 60, 50

STAGES = (
    ("C18:NEW", "Подбор", 10, "in_progress"),
    ("C18:SHOW", "Показ", 20, "in_progress"),
    ("C18:WON", "Успех", 90, "won"),
)


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def _user(conn, user_id, name, dept=KRETOV, dept_name="Кретов"):
    conn.execute(
        "INSERT INTO dim_user(user_id, name, last_name, department_id,"
        " department_name, is_active, synced_at) VALUES (?, ?, ?, ?, ?, 1, 'x')",
        (user_id, name, name.split()[-1], dept, dept_name),
    )


def _deal(conn, deal_id, user_id, stage, amount, *, won=0, closed=0,
          created=None, closedate=None):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id, assigned_by_id,
            source_id, opportunity, currency_id, date_create, date_modify, closedate,
            is_closed, is_won, is_lost, is_deleted, synced_at)
        VALUES (?, ?, 18, ?, ?, 'CALL', ?, 'RUB', ?, ?, ?, ?, ?, 0, 0, 'x')
        """,
        (deal_id, f"Объект {deal_id}", stage, user_id, amount,
         created or _ago(60), _ago(0), closedate, closed, won),
    )


def _event(conn, deal_id, stage, entered, left=None, seq=0):
    duration = None
    if left is not None:
        duration = int(
            (datetime.fromisoformat(left) - datetime.fromisoformat(entered))
            .total_seconds()
        )
    conn.execute(
        "INSERT INTO fact_stage_event(entity_type, entity_id, category_id, stage_id,"
        " entered_at, left_at, duration_sec, seq)"
        " VALUES ('deal', ?, 18, ?, ?, ?, ?, ?)",
        (deal_id, stage, entered, left, duration, seq),
    )


def _norm(conn, user_id, amount=4_500_000):
    conn.execute(
        "INSERT INTO plan_norm(period_code, scope_kind, scope_id, metric,"
        " basis, amount, source, updated_at)"
        " VALUES ('2026-Q3', 'user', ?, 'commission', 'absolute', ?, 'test', 'x')",
        (user_id, amount),
    )


@pytest.fixture
def mart(analytics_db):
    """Норма стадии «Подбор» — два дня: три завершённых интервала по два."""
    with analytics_session() as conn:
        conn.execute("INSERT INTO dim_pipeline(category_id, name, is_active, sort,"
                     " synced_at) VALUES (18, 'Покупатели', 1, 10, 'x')")
        for stage_id, name, sort, semantic in STAGES:
            conn.execute(
                "INSERT INTO dim_stage(stage_id, category_id, name, sort, semantic,"
                " synced_at) VALUES (?, 18, ?, ?, ?, 'x')",
                (stage_id, name, sort, semantic),
            )
        _user(conn, 1, "Мария Соколова")
        _user(conn, 2, "Пётр Ершов")
        _user(conn, 3, "Дмитрий Гусев", VOLKOVA, "Волкова")
        for user_id in (1, 2, 3):
            _norm(conn, user_id)
        for deal_id in (9001, 9002, 9003):
            _deal(conn, deal_id, 1, "C18:NEW", 0, closed=1)
            _event(conn, deal_id, "C18:NEW", _ago(40), _ago(38))
    return analytics_db


def _events(scope=None, **kwargs):
    with scoped_session(scope or Scope.everything()) as conn:
        return funnel.funnel_events(conn, SINCE, UNTIL, **kwargs)


# --------------------------------------------------------------------------
# встали
# --------------------------------------------------------------------------

def test_only_the_ones_that_stalled_just_now(mart):
    """Не «стоят сейчас» — тех сотни, и список не меняется неделями."""
    with analytics_session() as conn:
        _deal(conn, 101, 1, "C18:NEW", 5_000_000)          # перешла порог вчера
        _event(conn, 101, "C18:NEW", _ago(2.5))
        _deal(conn, 102, 1, "C18:NEW", 9_000_000)          # стоит давно
        _event(conn, 102, "C18:NEW", _ago(30))
        _deal(conn, 103, 1, "C18:NEW", 1_000_000)          # ещё в норме
        _event(conn, 103, "C18:NEW", _ago(1))

    stalled = _events()["stalled"]

    assert stalled["deals"] == 1
    assert stalled["amount"] == 5_000_000
    assert stalled["top"][0]["deal_id"] == 101


# --------------------------------------------------------------------------
# движение
# --------------------------------------------------------------------------

def test_a_move_forward_and_a_move_back_are_different_news(mart):
    """Возврат назад обычно значит неверную квалификацию — его не смешивают."""
    with analytics_session() as conn:
        _deal(conn, 201, 1, "C18:SHOW", 3_000_000)
        _event(conn, 201, "C18:NEW", _ago(9), _ago(0.5), seq=0)
        _event(conn, 201, "C18:SHOW", _ago(0.5), seq=1)
        _deal(conn, 202, 3, "C18:NEW", 800_000)
        _event(conn, 202, "C18:SHOW", _ago(9), _ago(0.5), seq=0)
        _event(conn, 202, "C18:NEW", _ago(0.5), seq=1)

    result = _events()

    assert [row["deal_id"] for row in result["advanced"]["top"]] == [201]
    assert result["advanced"]["amount"] == 3_000_000
    assert [row["deal_id"] for row in result["returned"]["top"]] == [202]


def test_a_won_deal_is_not_counted_as_progress(mart):
    """Выигранная сделка уже названа в строке про деньги — дважды не показываем."""
    with analytics_session() as conn:
        _deal(conn, 203, 1, "C18:WON", 4_000_000, won=1, closed=1,
              closedate=_ago(0.5))
        _event(conn, 203, "C18:SHOW", _ago(9), _ago(0.5), seq=0)
        _event(conn, 203, "C18:WON", _ago(0.5), seq=1)

    assert _events()["advanced"]["deals"] == 0


# --------------------------------------------------------------------------
# молчат
# --------------------------------------------------------------------------

def test_silence_is_measured_in_days_not_in_one_day(mart):
    """Брокер, не двигавший карточку сутки, работает нормально."""
    with analytics_session() as conn:
        _deal(conn, 301, 1, "C18:NEW", 1_000_000)     # двигал вчера
        _event(conn, 301, "C18:NEW", _ago(0.5))
        _deal(conn, 302, 2, "C18:NEW", 2_000_000)     # молчит третью неделю
        _event(conn, 302, "C18:NEW", _ago(20))

    silent = _events()["silent"]["people"]

    assert [man["user_id"] for man in silent] == [2]
    assert silent[0]["quiet_days"] == 20
    assert silent[0]["deals"] == 1


def test_only_those_who_carry_a_norm_are_asked(mart):
    """С новичка без нормы спроса нет — он затем и заведён, чтобы учиться."""
    with analytics_session() as conn:
        _user(conn, 9, "Новичок Зайцев")
        _deal(conn, 303, 9, "C18:NEW", 500_000)
        _event(conn, 303, "C18:NEW", _ago(30))

    everyone = _events()["silent"]["people"]
    on_plan = _events(on_plan_ids={1, 2, 3})["silent"]["people"]

    assert 9 in [man["user_id"] for man in everyone]
    assert 9 not in [man["user_id"] for man in on_plan]


# --------------------------------------------------------------------------
# качество
# --------------------------------------------------------------------------

def test_it_reports_what_broke_yesterday_not_the_standing_level(mart):
    """Заполненность 85% месяцами одна и та же — в ежедневном отчёте это фон."""
    with analytics_session() as conn:
        _deal(conn, 401, 1, "C18:WON", 0, won=1, closed=1, closedate=_ago(0.5))
        conn.execute(
            "INSERT INTO fact_deal(deal_id, title, category_id, stage_id,"
            " assigned_by_id, source_id, opportunity, currency_id, date_create,"
            " date_modify, closedate, is_closed, is_won, is_lost, is_deleted,"
            " synced_at) VALUES (402, 'Ничей', 18, 'C18:NEW', NULL, 'CALL', 0,"
            " 'RUB', ?, ?, NULL, 0, 0, 0, 0, 'x')", (_ago(0.5), _ago(0.5)),
        )

    quality = _events()["quality"]

    assert [row["deal_id"] for row in quality["won_without_amount"]] == [401]
    assert [row["deal_id"] for row in quality["without_assignee"]] == [402]


# --------------------------------------------------------------------------
# область видимости и тишина
# --------------------------------------------------------------------------

def test_a_rop_sees_only_his_own_events(mart):
    """Область видимости приходит соединением — фильтровать в отчёте нечего."""
    with analytics_session() as conn:
        _deal(conn, 501, 1, "C18:NEW", 5_000_000)
        _event(conn, 501, "C18:NEW", _ago(2.5))
        _deal(conn, 502, 3, "C18:NEW", 7_000_000)
        _event(conn, 502, "C18:NEW", _ago(2.5))

    mine = _events(Scope.departments([VOLKOVA]))["stalled"]

    assert [row["deal_id"] for row in mine["top"]] == [502]


def test_a_quiet_day_prints_nothing(mart):
    """Нечего сказать — блока нет. Иначе отчёт перестают открывать."""
    import pulse_digest

    lines = pulse_digest._event_lines(_events())

    assert lines == []
