"""Звонок, висящий на контакте, доходит до карточки клиента.

Это половина всех разговоров, а не доборка. В work.py записан замер
портала: 13 277 дел на контактах против 7 131 на сделках. Звонок в Битриксе
привязывают к контакту, а сделка ссылается на тот же контакт своим полем —
и прогон, который берёт дела только по сделкам, назовёт молчащими тех, кто
звонил каждый день.

Здесь же закреплено всё остальное, что превращает строки витрины в ленту:
отпечаток перехода по стадии, роботная запись как не-касание и числа
списка.
"""

from datetime import datetime, timedelta, timezone

import pytest

from clients.events import build_events, stage_source_id, totals
from clients.keys import Card, assign_keys
from clients.mart import Portfolio

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _portfolio(**over):
    base = dict(
        cards=(Card(7, "2-к", "C18:NEW", 77),),
        deals={7: {"deal_id": 7, "title": "2-к", "category_id": 18,
                   "stage_id": "C18:NEW", "assigned_by_id": 10, "contact_id": 77,
                   "date_create": _at(60), "is_closed": 0}},
        leads={},
        promises={},
        comments=(),
        activities=(),
        moves=(),
        users={10: {"user_id": 10, "name": "Пётр", "last_name": "Иванов",
                    "department_id": 5, "department_name": "Отдел"}},
        stages={("C18:NEW", 18): "Подбор"},
        mart_full_sync_at=_at(0.4),
    )
    base.update(over)
    return Portfolio(**base)


def _activity(**over):
    row = dict(
        activity_id=900, owner_type_id=3, owner_id=77, provider_type_id="CALL",
        direction=1, subject="Входящий", description="", author_id=10,
        responsible_id=10, created_at=_at(1), start_time=_at(1),
        end_time=_at(1), deadline=None, completed=1,
    )
    row.update(over)
    return row


def _keys(portfolio):
    contact = {77: {"NAME": "Пётр", "PHONE": [{"VALUE": "+79001112233"}]}}
    decisions = assign_keys(portfolio.cards, contact, type_names={}).decisions
    by_deal = {deal_id: item.key for deal_id, item in decisions.items()}
    by_contact = {77: decisions[7].key}
    return by_deal, by_contact


def test_a_call_owned_by_the_contact_lands_on_the_client():
    """Дело с владельцем-контактом находится по клиенту, у которого этот контакт."""
    portfolio = _portfolio(activities=(_activity(),))
    by_deal, by_contact = _keys(portfolio)

    events = build_events(portfolio, by_deal, by_contact)

    assert len(events) == 1
    assert events[0].client_key == by_deal[7]
    assert events[0].kind == "call"
    assert events[0].entity_type == "contact", "источник обязан быть виден в ленте"


def test_a_call_owned_by_a_stranger_contact_is_dropped():
    """Дело чужого контакта в чужую карточку не попадает.

    Обратная половина: без неё предыдущий тест был бы истинен и для правила
    «брать все дела подряд».
    """
    portfolio = _portfolio(activities=(_activity(owner_id=99),))
    by_deal, by_contact = _keys(portfolio)

    assert build_events(portfolio, by_deal, by_contact) == []


def test_a_deal_activity_that_is_not_a_call_is_a_mark():
    """Дело-отметка отличается от звонка видом, а не догадкой."""
    portfolio = _portfolio(activities=(
        _activity(activity_id=901, owner_type_id=2, owner_id=7, provider_type_id="TODO"),
    ))
    by_deal, by_contact = _keys(portfolio)

    events = build_events(portfolio, by_deal, by_contact)

    assert [event.kind for event in events] == ["activity"]
    assert events[0].entity_type == "deal"


def test_a_robot_comment_is_not_a_touch():
    """Роботная запись портала не делает карточку отработанной.

    «Новое обращение: Звонок с Cian» пишет робот. Засчитав её работой, отчёт
    назвал бы отработанной карточку, к которой никто не притрагивался.
    """
    portfolio = _portfolio(comments=(
        {"comment_id": 1, "entity_type": "deal", "entity_id": 7, "author_id": None,
         "body": "Новое обращение", "is_auto": 1, "created_at": _at(1)},
        {"comment_id": 2, "entity_type": "deal", "entity_id": 7, "author_id": 10,
         "body": "Созвонились", "is_auto": 0, "created_at": _at(5)},
    ))
    by_deal, by_contact = _keys(portfolio)
    events = build_events(portfolio, by_deal, by_contact)

    got = totals(events, assignee_id=10, now=NOW)

    assert got.comments_total == 1, "роботная запись комментарием брокера не считается"
    assert got.last_touch_at == _at(5), "и касанием тоже"
    assert got.last_event_at == _at(1), "но в ленте она есть — это было"
    assert got.silence_days == 5


def test_a_comment_by_someone_else_is_not_the_assignees_work():
    """Комментарий РОПа не засчитывается ответственному."""
    portfolio = _portfolio(comments=(
        {"comment_id": 1, "entity_type": "deal", "entity_id": 7, "author_id": 99,
         "body": "Проверил", "is_auto": 0, "created_at": _at(2)},
    ))
    by_deal, by_contact = _keys(portfolio)

    got = totals(build_events(portfolio, by_deal, by_contact), assignee_id=10, now=NOW)

    assert got.comments_total == 1
    assert got.comments_by_assignee == 0


def test_an_old_comment_of_the_assignee_falls_out_of_the_month():
    """Тридцатидневное окно считается от «сейчас», а не от начала времён."""
    portfolio = _portfolio(comments=(
        {"comment_id": 1, "entity_type": "deal", "entity_id": 7, "author_id": 10,
         "body": "Давно", "is_auto": 0, "created_at": _at(45)},
        {"comment_id": 2, "entity_type": "deal", "entity_id": 7, "author_id": 10,
         "body": "Недавно", "is_auto": 0, "created_at": _at(3)},
    ))
    by_deal, by_contact = _keys(portfolio)

    got = totals(build_events(portfolio, by_deal, by_contact), assignee_id=10, now=NOW)

    assert got.comments_by_assignee == 2
    assert got.comments_by_assignee_30d == 1


def test_a_stage_move_is_identified_by_its_fingerprint_not_by_a_row_id():
    """Отпечаток перехода не зависит ни от прогона ETL, ни от соли интерпретатора.

    `fact_stage_event.id` перевыдаётся каждые пятнадцать минут: ленту
    сущности переписывают целиком через DELETE + INSERT. Ключ на нём плодил
    бы дубликаты. Встроенный `hash()` не годится по другой причине — он
    солится PYTHONHASHSEED, и прогон с другой солью завёл бы всю ленту
    заново.
    """
    first = stage_source_id("deal", 7, _at(3), "C18:NEW", "C18:UC_UFPFKK")
    same = stage_source_id("deal", 7, _at(3), "C18:NEW", "C18:UC_UFPFKK")
    other = stage_source_id("deal", 7, _at(3), "C18:UC_UFPFKK", "C18:NEW")

    assert first == same
    assert first != other, "направление перехода обязано менять отпечаток"
    # Значение, а не форма. Проверка «сорок шестнадцатеричных знаков»
    # проходит и для встроенного hash(), который солится PYTHONHASHSEED и
    # в соседнем процессе даёт другое число — то есть заводит всю ленту
    # заново. Эталон пришивает отпечаток к содержимому перехода.
    assert stage_source_id(
        "deal", 7, "2026-09-19T12:00:00+00:00", "C18:NEW", "C18:WON",
    ) == "8e85b2b98602c9813d9ba69e1364bb3f61f04dfe"


def test_the_first_move_of_a_card_comes_from_nowhere():
    """У первого перехода нет предыдущей стадии, а у второго она есть."""
    portfolio = _portfolio(moves=(
        {"entity_type": "deal", "entity_id": 7, "category_id": 18,
         "stage_id": "C18:NEW", "entered_at": _at(60), "seq": 1},
        {"entity_type": "deal", "entity_id": 7, "category_id": 18,
         "stage_id": "C18:UC_UFPFKK", "entered_at": _at(30), "seq": 2},
    ))
    by_deal, by_contact = _keys(portfolio)

    events = build_events(portfolio, by_deal, by_contact)

    assert [event.payload["stage_from"] for event in events] == ["", "C18:NEW"]
    assert events[1].payload["stage_name"] == ""
    assert events[0].payload["stage_name"] == "Подбор"


def test_each_card_starts_its_own_chain_of_moves():
    """Первый переход второй карточки не наследует стадию первой."""
    portfolio = _portfolio(
        cards=(Card(7, contact_id=77), Card(8, contact_id=77)),
        deals={
            7: {"deal_id": 7, "category_id": 18, "stage_id": "C18:NEW",
                "assigned_by_id": 10, "contact_id": 77, "date_create": _at(60)},
            8: {"deal_id": 8, "category_id": 18, "stage_id": "C18:NEW",
                "assigned_by_id": 10, "contact_id": 77, "date_create": _at(50)},
        },
        moves=(
            {"entity_type": "deal", "entity_id": 7, "category_id": 18,
             "stage_id": "C18:NEW", "entered_at": _at(60), "seq": 1},
            {"entity_type": "deal", "entity_id": 7, "category_id": 18,
             "stage_id": "C18:WON", "entered_at": _at(40), "seq": 2},
            {"entity_type": "deal", "entity_id": 8, "category_id": 18,
             "stage_id": "C18:NEW", "entered_at": _at(50), "seq": 1},
        ),
    )
    by_deal, by_contact = _keys(portfolio)

    events = build_events(portfolio, by_deal, by_contact)
    eights = [e for e in events if e.entity_id == 8]

    assert [e.payload["stage_from"] for e in eights] == [""]


def test_the_next_step_is_the_nearest_open_deadline():
    """Следующий шаг — ближайший срок незакрытого дела."""
    portfolio = _portfolio(activities=(
        _activity(activity_id=1, completed=0, deadline=_at(-10)),
        _activity(activity_id=2, completed=0, deadline=_at(-3)),
    ))
    by_deal, by_contact = _keys(portfolio)

    got = totals(build_events(portfolio, by_deal, by_contact), assignee_id=10, now=NOW)

    assert got.next_step_at == _at(-3)
    assert got.next_step_overdue is False


def test_a_finished_task_is_no_longer_a_promise():
    """Закрытое дело обещанием быть перестало: его уже сделали."""
    portfolio = _portfolio(activities=(_activity(completed=1, deadline=_at(5)),))
    by_deal, by_contact = _keys(portfolio)

    got = totals(build_events(portfolio, by_deal, by_contact), assignee_id=10, now=NOW)

    assert got.next_step_at is None
    assert got.next_step_overdue is None, "«не назначен» и «просрочен» — разные ответы"


def test_a_deadline_in_the_past_is_overdue():
    """Срок в прошлом у незакрытого дела — просроченное обещание."""
    portfolio = _portfolio(activities=(_activity(completed=0, deadline=_at(5)),))
    by_deal, by_contact = _keys(portfolio)

    got = totals(build_events(portfolio, by_deal, by_contact), assignee_id=10, now=NOW)

    assert got.next_step_at == _at(5)
    assert got.next_step_overdue is True


def test_a_client_with_no_events_counts_nothing_rather_than_zero():
    """Пустая лента не выдаёт себя за посчитанный ноль."""
    got = totals((), assignee_id=10, now=NOW)

    assert got.silence_days is None
    assert got.last_touch_at is None
    assert got.next_step_overdue is None


@pytest.mark.parametrize("reverse", [False, True], ids=["как-есть", "наоборот"])
def test_the_feed_does_not_depend_on_the_order_the_mart_answered_in(reverse):
    """Порядок строк витрины не меняет ленту."""
    rows = (
        {"comment_id": 1, "entity_type": "deal", "entity_id": 7, "author_id": 10,
         "body": "раз", "is_auto": 0, "created_at": _at(3)},
        {"comment_id": 2, "entity_type": "deal", "entity_id": 7, "author_id": 10,
         "body": "два", "is_auto": 0, "created_at": _at(3)},
    )
    portfolio = _portfolio(comments=tuple(reversed(rows)) if reverse else rows)
    by_deal, by_contact = _keys(portfolio)

    events = build_events(portfolio, by_deal, by_contact)

    assert [event.source_id for event in events] == ["1", "2"]
