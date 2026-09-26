"""Справочник проблем раздела 8: что именно не так, а не кого смотреть первым.

Состояние у клиента одно — правила идут сверху вниз, первое совпавшее
выигрывает. Проблем бывает несколько сразу, и главное здесь именно это:
проблемы считаются НЕЗАВИСИМО от цепочки правил. У человека с маркером
отказа состояние «отказ», правило 3 до него не доходит, — а
нерасшифрованные разговоры у него всё равно есть, и по состоянию их было
бы не видно никогда.

Второе решение, которого в ТЗ нет прямо: условие «на активной стадии»
стоит там у трёх кодов из пяти, а здесь применяется ко всем. По закрытой
карточке претензий не бывает: клиент, у которого всё закрыто, не
проблемный, а завершённый.
"""

from datetime import datetime, timedelta, timezone

import pytest

from analytics.schema import analytics_session
from clients import mart
from clients.issues import detect, overdue
from clients.schema import (
    ISSUE_ABANDONED,
    ISSUE_LABELS,
    ISSUE_MISSING_TRANSCRIPTS,
    ISSUE_NO_ASSIGNEE_COMMENT,
    ISSUE_ORDER,
    ISSUE_PROMISE_OVERDUE,
    ISSUE_REFUSAL_NOT_REFLECTED,
)
from clients.triage import ABANDONED_DAYS, Facts
from clients.triage import silence_days as triage_silence

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _found(facts=None, **over):
    base = {"comments_by_assignee": 3, "silence_days": 1, "promise_overdue": False}
    base.update(over)
    return detect(facts or Facts(cards_total=1, cards_closed=0), **base)


# ── независимость от цепочки правил ───────────────────────────────────

def test_several_things_can_be_wrong_with_one_person():
    """Ради этого справочник и заводился: состояние одно, претензий пять."""
    facts = Facts(cards_total=2, cards_closed=1, has_refusal=True,
                  calls_without_text=3)

    found = _found(facts, comments_by_assignee=0, silence_days=99,
                   promise_overdue=True)

    assert found == ISSUE_ORDER, "порядок тот же, что на экране"


def test_a_refusal_does_not_hide_the_missing_transcripts():
    """По состоянию их не видно: правило 2 сработало раньше правила 3.

    Клиент с маркером отказа получает состояние «отказ», и до правила
    «нет данных» цепочка не доходит. Считай проблемы по состоянию — и
    пробел в записях у такого клиента не увидел бы никто.
    """
    facts = Facts(cards_total=1, cards_closed=0, has_refusal=True,
                  calls_without_text=2)

    found = _found(facts)

    assert ISSUE_REFUSAL_NOT_REFLECTED in found
    assert ISSUE_MISSING_TRANSCRIPTS in found


# ── «на активной стадии» ──────────────────────────────────────────────

def test_a_client_with_everything_closed_has_no_complaints():
    """Завершённый, а не проблемный: по закрытой карточке претензий нет."""
    facts = Facts(cards_total=2, cards_closed=2, has_refusal=True,
                  calls_without_text=5)

    assert _found(facts, comments_by_assignee=0, silence_days=99,
                  promise_overdue=True) == ()


def test_a_client_without_cards_at_all_is_still_asked_about():
    """Ноль закрытых из нуля — не «всё закрыто», а «закрывать было нечего».

    Такого клиента завели и не закрывали, и это как раз повод для
    претензии, а не причина её снять.
    """
    found = _found(Facts(cards_total=0, cards_closed=0), comments_by_assignee=0)

    assert found == (ISSUE_NO_ASSIGNEE_COMMENT,)


# ── отдельные коды ────────────────────────────────────────────────────

def test_an_uncounted_comment_is_not_a_missing_one():
    """NULL значит «не считали», и претензия по нему — обвинение прогона.

    Ноль комментариев ответственного это претензия к брокеру. Пустота —
    это то, что прогон до подсчёта не дошёл, и объявить её претензией
    значит выставить брокеру счёт за чужую поломку.
    """
    assert ISSUE_NO_ASSIGNEE_COMMENT not in _found(comments_by_assignee=None)
    assert ISSUE_NO_ASSIGNEE_COMMENT in _found(comments_by_assignee=0)
    assert ISSUE_NO_ASSIGNEE_COMMENT not in _found(comments_by_assignee=1)


@pytest.mark.parametrize("days, complained", [
    (ABANDONED_DAYS, False),
    (ABANDONED_DAYS + 1, True),
    (None, False),
])
def test_the_abandoned_threshold_is_the_same_as_the_rule(days, complained):
    """Порог один с правилом 5: два порога об одном разошлись бы."""
    assert (ISSUE_ABANDONED in _found(silence_days=days)) is complained


def test_the_silence_is_measured_the_same_way_as_the_rule():
    """Клиента, которого не касались ни разу, видят оба — или ни один.

    Правило 5 откатывается на дату заведения карточки: клиент без единого
    касания иначе провалился бы сквозь «брошен» в «без плана». Претензия
    обязана считать тем же числом, иначе тот же человек будет «брошен» в
    списке и без претензии в исключениях — расхождение, которое РОП
    заметит раньше, чем мы.
    """
    facts = Facts(cards_total=1, cards_closed=0, oldest_card_at=_at(99))

    assert triage_silence(facts, now=NOW) == 99
    assert ISSUE_ABANDONED in _found(facts, silence_days=triage_silence(facts, now=NOW))


def test_a_broken_promise_reaches_the_codes():
    """Признак считается снаружи, а код выставляется здесь — связь проверена."""
    assert ISSUE_PROMISE_OVERDUE in _found(promise_overdue=True)
    assert ISSUE_PROMISE_OVERDUE not in _found(promise_overdue=False)


def test_calls_without_text_are_a_complaint():
    facts = Facts(cards_total=1, cards_closed=0, calls_without_text=1)

    assert ISSUE_MISSING_TRANSCRIPTS in _found(facts)


def test_every_code_has_a_russian_label():
    """Код уходит в адрес, подпись — на экран, и обоих должно быть пять."""
    assert set(ISSUE_ORDER) == set(ISSUE_LABELS)
    assert len(ISSUE_ORDER) == 5


# ── просроченное обещание ─────────────────────────────────────────────

def test_a_deadline_in_the_past_is_a_broken_promise():
    assert overdue([{"promised_at": _at(3), "wait_until": None}], now=NOW) is True


def test_a_deadline_still_ahead_is_not():
    assert overdue([{"promised_at": _at(-3), "wait_until": None}], now=NOW) is False


def test_agreed_silence_is_not_a_broken_promise():
    """«Созвонимся в конце осени» — договорённость, а не просрочка.

    Без этого отчёт ругал бы за правильную работу: срок первого обещания
    прошёл, но клиент сам попросил не звонить до ноября.
    """
    row = {"promised_at": _at(30), "wait_until": _at(-30)}

    assert overdue([row], now=NOW) is False


def test_expired_agreed_silence_stops_protecting():
    row = {"promised_at": _at(30), "wait_until": _at(5)}

    assert overdue([row], now=NOW) is True


def test_a_client_who_promised_nothing_is_not_late():
    assert overdue([], now=NOW) is False
    assert overdue([{"promised_at": "", "wait_until": ""}], now=NOW) is False


def test_one_broken_promise_among_several_cards_is_enough():
    rows = [{"promised_at": _at(-5), "wait_until": None},
            {"promised_at": _at(5), "wait_until": None}]

    assert overdue(rows, now=NOW) is True


# ── обещания читаются из витрины ──────────────────────────────────────

@pytest.fixture
def mart_db(analytics_db):
    with analytics_session() as conn:
        conn.executemany(
            "INSERT INTO fact_comment_read(entity_type, entity_id, source_hash,"
            " promised_at, wait_until, read_at) VALUES ('deal', ?, ?, ?, ?, ?)",
            [
                (7, "h1", _at(2), None, _at(2)),
                (9, "h3", _at(1), _at(-10), _at(1)),
            ],
        )
    return analytics_db


def test_one_row_per_card_and_it_is_read_as_it_lies(mart_db):
    """Выбирать «самое позднее» не из чего: строка на карточку одна.

    `fact_comment_read` держит `PRIMARY KEY (entity_type, entity_id)` и
    перезаписывается по мере появления новых комментариев, так что
    действующее обещание там уже лежит. Группировка сообщала бы читателю
    неправду об устройстве данных.
    """
    with analytics_session(readonly=True) as conn:
        found = mart.read_promises(conn, [7, 9])

    assert found[7]["promised_at"] == _at(2)
    assert found[9]["wait_until"] == _at(-10)
    assert set(found) == {7, 9}


def test_a_deal_nobody_read_has_no_promise(mart_db):
    with analytics_session(readonly=True) as conn:
        assert mart.read_promises(conn, [777]) == {}
