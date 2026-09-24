"""Факты, на которых стоят правила: что считать ответом, отказом и звонком.

Между лентой и правилами. Правила проверены отдельно и знают только
готовые признаки; здесь проверяется, как эти признаки добываются из
событий — и именно тут живут решения, которых в ТЗ не было.

Три самых дорогих: комментарий не считается ответом клиенту, роботная
запись не считается его словами, а звонок, длительность которого измерить
нечем, не считается поводом требовать расшифровку.
"""

from datetime import datetime, timedelta, timezone

import pytest

from clients.events import Event
from clients.facts import collect, coverage, duration_sec
from clients.schema import EVENT_ACTIVITY, EVENT_CALL, EVENT_COMMENT
from clients.triage import MEANINGFUL_CALL_SEC

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
DEALS = {1: {"is_closed": 0, "date_create": "2026-01-10T09:00:00+00:00"}}


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _call(activity_id: int, days_ago: float, *, direction: int, seconds: int = 90):
    started = NOW - timedelta(days=days_ago)
    ended = started + timedelta(seconds=seconds)
    return Event(
        "k", started.isoformat(), EVENT_CALL, str(activity_id), "deal", 1, 10, False,
        {"direction": direction, "start_time": started.isoformat(),
         "end_time": ended.isoformat()},
    )


def _comment(days_ago: float, body: str = "", *, is_system: bool = False):
    return Event("k", _at(days_ago), EVENT_COMMENT, f"c{days_ago}", "deal", 1, 10,
                 is_system, {"body": body})


def _todo(days_ago: float, *, completed: bool):
    return Event("k", _at(days_ago), EVENT_ACTIVITY, f"t{days_ago}", "deal", 1, 10,
                 False, {"completed": completed, "subject": "перезвонить"})


def _facts(events, **over):
    base = {
        "deals": DEALS, "last_touch_at": _at(1), "next_step_at": None,
        "transcribed": set(), "refused_calls": set(), "markers": ("передумал",),
    }
    base.update(over)
    return collect(events, [1], **base)


# ── что считается ответом клиенту (правило 4) ──────────────────────────

def test_a_comment_is_not_an_answer_to_the_client():
    """Написать себе в карточку — не значит перезвонить.

    Самое дорогое решение этого модуля. Засчитав комментарий за
    исходящую активность, правило 4 молчало бы ровно там, где брокер
    отметился в CRM вместо того, чтобы выйти на связь, — то есть в том
    единственном случае, ради которого правило и написано.
    """
    events = [_call(1, 3, direction=1), _comment(1, "клиент звонил, надо перезвонить")]

    got = _facts(events)

    assert got.last_incoming_call_at is not None
    assert got.last_outgoing_at is None, "комментарий исходящей активностью не является"


def test_an_outgoing_call_is_an_answer():
    """Обратная половина: перезвонили — ответ засчитан."""
    got = _facts([_call(1, 3, direction=1), _call(2, 1, direction=2)])

    assert got.last_outgoing_at > got.last_incoming_call_at


def test_a_todo_still_open_is_not_an_answer_either():
    """«Перезвонить» в планах — это ещё не звонок.

    Незакрытое дело означает намерение. Засчитав его, правило 4 снимало бы
    клиента с очереди за то, что брокер завёл себе напоминание.
    """
    got = _facts([_call(1, 3, direction=1), _todo(1, completed=False)])

    assert got.last_outgoing_at is None


def test_a_finished_task_counts_as_an_answer():
    """Закрытое дело — действие, и оно засчитывается.

    Встреча и выполненная задача — тоже выход на связь, и требовать
    именно звонка значило бы держать в очереди клиента, с которым
    брокер вчера встречался.
    """
    got = _facts([_call(1, 3, direction=1), _todo(1, completed=True)])

    assert got.last_outgoing_at is not None


# ── что считается отказом (правило 2) ──────────────────────────────────

def test_a_robot_comment_is_not_the_clients_words():
    """Роботная запись отказом не объявляет.

    Автоматический комментарий пишет интеграция, и слово из её шаблона —
    это не то, что сказал человек. Правило 2 перебивает тишину, то есть
    убирает клиента из списка брокера: заплатить за чужой шаблон живым
    клиентом слишком дорого.
    """
    assert _facts([_comment(1, "клиент передумал", is_system=True)]).has_refusal is False
    assert _facts([_comment(1, "клиент передумал")]).has_refusal is True


def test_a_refusal_heard_on_a_call_counts():
    """Маркер в расшифровке — тот же отказ, что в комментарии."""
    got = _facts([_call(7, 1, direction=1)], refused_calls={7})

    assert got.has_refusal is True


def test_a_refusal_on_someone_elses_call_does_not_count():
    """Чужой номер звонка отказом этого клиента не делает."""
    got = _facts([_call(7, 1, direction=1)], refused_calls={999})

    assert got.has_refusal is False


# ── что считается поводом искать расшифровку (правило 3) ───────────────

def test_a_short_call_does_not_demand_a_transcript():
    """Недозвон расшифровывать незачем, и требовать по нему текста тоже.

    Иначе правило 3 объявляло бы безданным каждого, кому не дозвонились —
    а таких в портфеле больше всех.
    """
    short = _call(1, 1, direction=2, seconds=MEANINGFUL_CALL_SEC - 1)

    assert _facts([short]).calls_without_text == 0


def test_a_call_exactly_a_minute_long_does_demand_one():
    """Ровно минута — уже разговор. Граница проверяется с обеих сторон."""
    exact = _call(1, 1, direction=2, seconds=MEANINGFUL_CALL_SEC)

    assert _facts([exact]).calls_without_text == 1


def test_a_call_nobody_can_measure_is_not_counted():
    """Длительность неизвестна — порог применять не к чему.

    Витрина хранит метки начала и конца, а не длительность `[V28]`, и
    заполнены они не всегда. Считать неизмеримый звонок длинным значит
    выдумывать: правило 3 требовало бы текста по недозвонам.
    """
    blind = Event("k", _at(1), EVENT_CALL, "1", "deal", 1, 10, False,
                  {"direction": 2, "start_time": None, "end_time": None})

    assert _facts([blind]).calls_without_text == 0


def test_a_transcribed_call_leaves_no_hole():
    """Текст есть — дыры нет."""
    assert _facts([_call(1, 1, direction=2)], transcribed={1}).calls_without_text == 0


def test_a_missing_cache_does_not_declare_the_whole_portfolio_blind():
    """Кэш недоступен — правило 3 молчит, а не срабатывает на всех.

    ``None`` означает «не знаем». Посчитав его за «расшифровок нет»,
    прогон отправил бы в «нет данных» каждого клиента со звонком — в ту
    самую ночь, когда файл кэша оказался не на месте.
    """
    got = _facts([_call(1, 1, direction=2)], transcribed=None)

    assert got.calls_without_text == 0


# ── длительность ───────────────────────────────────────────────────────

def test_the_duration_comes_from_the_two_marks():
    """Длительности в витрине нет, она считается разностью."""
    started = NOW - timedelta(minutes=5)
    payload = {"start_time": started.isoformat(),
               "end_time": (started + timedelta(seconds=137)).isoformat()}

    assert duration_sec(payload) == 137


def test_an_end_before_the_start_is_not_a_duration():
    """Конец раньше начала — значит одна из меток не про этот звонок.

    Считать по ней минуту нельзя: отрицательная разность прошла бы порог
    как угодно, а вычитание по модулю выдумало бы разговор там, где его
    не было.
    """
    started = NOW
    payload = {"start_time": started.isoformat(),
               "end_time": (started - timedelta(seconds=90)).isoformat()}

    assert duration_sec(payload) is None


@pytest.mark.parametrize("payload", [
    {}, {"start_time": None, "end_time": None},
    {"start_time": "не дата", "end_time": "тоже"},
    {"start_time": "2026-09-24T10:00:00+00:00"},
])
def test_a_missing_or_broken_mark_gives_no_duration(payload):
    """Пусто и мусор одинаково означают «не измерить»."""
    assert duration_sec(payload) is None


def test_the_coverage_counts_what_can_be_measured_at_all():
    """Сколько звонков вообще поддаётся измерению — число для лога.

    Порог, применённый к неизмеримому, молча не срабатывает никогда, и
    отличить «дыр в данных нет» от «нечем мерить» по состояниям клиентов
    невозможно.
    """
    blind = Event("k", _at(1), EVENT_CALL, "9", "deal", 1, 10, False, {"direction": 2})

    got = coverage([
        _call(1, 1, direction=2, seconds=90),
        _call(2, 1, direction=1, seconds=10),
        blind,
        _comment(1, "не звонок"),
    ])

    assert got.calls == 3
    assert got.measurable == 2
    assert got.meaningful == 1


# ── карточки ───────────────────────────────────────────────────────────

def test_closed_cards_are_counted():
    """Правилу 1 нужно число закрытых карточек, а не их наличие."""
    deals = {1: {"is_closed": 1}, 2: {"is_closed": 0}}

    got = collect([], [1, 2], deals=deals, last_touch_at=None, next_step_at=None,
                  transcribed=set(), refused_calls=set(), markers=())

    assert (got.cards_total, got.cards_closed) == (2, 1)


def test_the_oldest_card_date_is_collected():
    """Дата заведения самой старой карточки доезжает до правил.

    По ней считается тишина клиента, которого не касались ни разу. Не
    собрав её, сборщик оставил бы правило 5 без единственного входа в
    этот случай, и тесты правил остались бы зелёными.
    """
    deals = {
        1: {"is_closed": 0, "date_create": "2026-03-01T00:00:00+00:00"},
        2: {"is_closed": 0, "date_create": "2026-01-01T00:00:00+00:00"},
    }

    got = collect([], [1, 2], deals=deals, last_touch_at=None, next_step_at=None,
                  transcribed=set(), refused_calls=set(), markers=())

    assert got.oldest_card_at == "2026-01-01T00:00:00+00:00"


def test_a_card_the_mart_does_not_know_is_still_a_card():
    """Карточка, которой нет в витрине, не роняет сборку.

    Сделка могла уехать из воронок между чтением и решением. Упасть здесь
    значит не собрать портфель целиком из-за одной строки.
    """
    got = collect([], [1, 404], deals=DEALS, last_touch_at=None, next_step_at=None,
                  transcribed=set(), refused_calls=set(), markers=())

    assert got.cards_total == 2
    assert got.cards_closed == 0
