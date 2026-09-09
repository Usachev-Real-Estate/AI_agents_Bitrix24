"""Обещал, дня не назвал и замолчал.

Половина обещаний портфеля — без срока: «перезвонить позднее»,
«свяжемся». Просрочки у них нет и быть не может, и брокер прав, если
возразит: он не обещал «в пятницу», он обещал «позднее». Предъявить такое
обещание как невыполненное значит проиграть разговор — и заодно обесценить
соседние строки, где всё верно.

Проверяемым его делает молчание. «Обещал перезвонить, и с тех пор сорок
дней ни одной записи» — уже не придирка: обещание есть, времени прошло
больше, чем на любую задержку, следа работы нет.

Порог молчания взят не новый, а тот же SILENT_DAYS, которым во всей
витрине меряется брошенная карточка. Второе определение молчания однажды
дало бы два разных ответа на один вопрос.

Место в сводке общее с просроченными обещаниями, и вес намеренно на два
порядка ниже. Это не приём против правила, а его смысл: одно
невыполненное «позвонить в пятницу» весит больше любой горки «перезвоню
как-нибудь», потому что разговор по нему короткий.
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


def _day(shift: int) -> str:
    return (date.today() + timedelta(days=shift)).isoformat()


def _card(conn, deal_id, *, promised="Перезвонить позднее", promised_at=None,
          wait_until=None, quiet=40, closed=0, user=10,
          name="Ксения Зайцева"):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
            assigned_by_id, source_id, opportunity, currency_id, date_create,
            date_modify, closedate, is_closed, is_won, is_lost, contact_id,
            is_deleted, synced_at)
        VALUES (?, ?, 0, 'UC_SEARCH', ?, 'ADV', 0, 'RUB',
                '2026-05-01T00:00:00+00:00', '2026-05-01T00:00:00+00:00',
                NULL, ?, 0, 0, 5000, 0, 'x')
        """,
        (deal_id, f"ЖК «Событие {deal_id}»", user, closed),
    )
    conn.execute("INSERT OR IGNORE INTO dim_user(user_id, name, is_active,"
                 " synced_at) VALUES (?, ?, 1, 'x')", (user, name))
    conn.execute(
        "INSERT INTO fact_comment(comment_id, entity_type, entity_id,"
        " author_id, body, is_auto, created_at, synced_at)"
        " VALUES (?, 'deal', ?, 1, 'Перезвонить позднее', 0, ?, 'x')",
        (deal_id * 10, deal_id, _ago(quiet)),
    )
    conn.execute(
        """
        INSERT INTO fact_comment_read(entity_type, entity_id, source_hash,
            promised, promised_at, wait_until, refused, refused_why, ready,
            terms, read_at, prompt_version)
        VALUES ('deal', ?, 'h', ?, ?, ?, 0, '', '', '', '2026-09-09', '4')
        """,
        (deal_id, promised, promised_at, wait_until),
    )


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        _card(conn, 1)
    return analytics_db


def _rows():
    with scoped_session(Scope.everything()) as conn:
        return work.promises_without_date(conn, [0])


# ── Что попадает в список ──────────────────────────────────────────────
def test_a_silent_card_with_a_vague_promise_is_found(mart):
    rows = _rows()

    assert [row["deal_id"] for row in rows] == [1]
    assert rows[0]["promised"] == "Перезвонить позднее"
    assert 39 <= rows[0]["quiet_days"] <= 41


def test_a_fresh_promise_is_not_a_reproach(analytics_db):
    """Обещал вчера — спрашивать не о чем. Дня он и не называл."""
    with analytics_session() as conn:
        _card(conn, 1, quiet=3)
    assert _rows() == []


def test_the_silence_threshold_is_the_one_the_mart_already_uses(analytics_db):
    """Порог — SILENT_DAYS, а не своё число: второе определение молчания
    однажды ответило бы иначе на тот же вопрос."""
    with analytics_session() as conn:
        _card(conn, 1, quiet=work.SILENT_DAYS - 1)
        _card(conn, 2, quiet=work.SILENT_DAYS + 1)

    assert [row["deal_id"] for row in _rows()] == [2]


def test_a_promise_with_a_day_belongs_to_the_other_rule(analytics_db):
    """Срок назван — это просрочка, и разговор о ней другой."""
    with analytics_session() as conn:
        _card(conn, 1, promised="Позвонить в пятницу", promised_at=_day(-20))
    assert _rows() == []


def test_an_agreed_silence_is_still_legitimate(analytics_db):
    """Договорились ждать — молчание законно и здесь тоже."""
    with analytics_session() as conn:
        _card(conn, 1, wait_until=_day(+10))
    assert _rows() == []


def test_a_closed_card_is_left_alone(analytics_db):
    with analytics_session() as conn:
        _card(conn, 1, closed=1)
    assert _rows() == []


# ── Как это звучит ─────────────────────────────────────────────────────
def test_the_advice_asks_for_a_day(mart):
    item = advice_rules.promise_without_date(_rows())[0]

    assert item.who == "Ксения Зайцева"
    assert "1 обещание без срока, и карточка молчит" in item.title
    assert "«Перезвонить позднее»" in item.proof
    assert "записей нет 40 дн" in item.proof
    assert "называть день" in item.action


# ── Место в сводке ─────────────────────────────────────────────────────
def _vague(count, days=40):
    return [{
        "deal_id": i, "title": f"ЖК «Событие {i}»", "stage_id": "UC_SEARCH",
        "stage_name": "Поиск клиента", "broker": "Ксения Зайцева",
        "user_id": 10, "promised": "Перезвонить позднее", "terms": "",
        "ready": "", "quiet_days": float(days - i),
    } for i in range(count)]


def _dated(count, days=27):
    return [{
        "deal_id": 100 + i, "title": f"ВГ {747 + i}", "stage_id": "UC_X",
        "stage_name": "Подбор", "broker": "Марат Абзалилов", "user_id": 11,
        "promised": "Позвонить, запросить обратную связь",
        "promised_at": "2026-08-14", "terms": "", "ready": "",
        "overdue_days": float(days - i),
    } for i in range(count)]


def test_a_named_day_outweighs_any_pile_of_vague_ones():
    """Одно «позвонить в пятницу» важнее тридцати «перезвоню как-нибудь».

    Разговор по названному дню короткий и спорить не о чем, по обещанию
    без дня — длинный и брокер прав наполовину.
    """
    selection = advice.select(
        advice_rules.collect(promises=_dated(1), vague=_vague(30)), {},
    )

    promise = [a for a in selection.advices if a.slot == advice.SLOT_PROMISE]
    assert [a.rule for a in promise] == ["promise_overdue"]


def test_without_overdue_promises_the_vague_ones_speak():
    """Просроченных нет — очередь этих, иначе правило мертво."""
    selection = advice.select(
        advice_rules.collect(promises=None, vague=_vague(12)), {},
    )

    assert [a.rule for a in selection.advices] == ["promise_no_date"]
    assert selection.advices[0].value == 12
    # Согласование проверяется на обоих числах, иначе оно снова уедет.
    assert "12 обещаний без срока, и карточки молчат" in selection.advices[0].title
