"""Счётчик расшифровок клиента: сколько у него есть что читать.

Число уходит в колонку `calls_with_transcript` и оттуда — в разбор
моделью: прежде чем спрашивать «что с этим клиентом», надо знать, на чём
отвечать. Ноль расшифровок не запрещает разбор, но делает его разбором
дат и комментариев, а не разговоров, и путать эти два случая нельзя.

Два решения, которые проверяются здесь и которых нет в ТЗ:

* короткий звонок с расшифровкой в счёт идёт, хотя правилу 3 он не указ;
* недоступный кэш даёт ``None``, а не ноль.
"""

from datetime import datetime, timedelta, timezone

from clients.events import Event
from clients.facts import calls_with_text
from clients.schema import EVENT_ACTIVITY, EVENT_CALL, EVENT_COMMENT
from clients.triage import MEANINGFUL_CALL_SEC

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _call(activity_id: int, *, seconds: int = 90):
    started = NOW - timedelta(days=1)
    ended = started + timedelta(seconds=seconds)
    return Event(
        "k", started.isoformat(), EVENT_CALL, str(activity_id), "deal", 1, 10, False,
        {"direction": 2, "start_time": started.isoformat(),
         "end_time": ended.isoformat()},
    )


def _comment(source_id: str):
    return Event("k", NOW.isoformat(), EVENT_COMMENT, source_id, "deal", 1, 10,
                 False, {"body": "созвонились"})


def _todo(source_id: str):
    return Event("k", NOW.isoformat(), EVENT_ACTIVITY, source_id, "deal", 1, 10,
                 False, {"completed": True, "subject": "перезвонить"})


def test_a_short_call_with_a_transcript_still_counts_as_material():
    """Порог в минуту — это правило 3, а не мера читаемости текста.

    Правило 3 не требует расшифровку с короткого звонка, потому что там
    обычно недозвон. Но если текст всё-таки есть, читать его можно, и
    объявить клиента бесполезным для разбора из-за длительности значило
    бы выбросить единственный разговор, который записан.
    """
    short = MEANINGFUL_CALL_SEC - 30
    events = [_call(101, seconds=short)]
    assert calls_with_text(events, {101}) == 1


def test_an_unreachable_cache_leaves_the_counter_unset_not_zero():
    """База аудита не на месте — «не считано», а не «расшифровок нет».

    Ноль в эту ночь означал бы, что у всего портфеля разом пропали
    расшифровки, и разбор увидел бы портфель без материала там, где не
    открылся один файл.
    """
    assert calls_with_text([_call(101), _call(102)], None) is None


def test_a_cache_that_is_simply_empty_counts_zero():
    """Кэш прочитан и пуст — это настоящий ноль, и он обязан быть числом.

    Пара с предыдущим тестом и есть смысл колонки: ``None`` и ``0``
    отвечают на разные вопросы, и колонка объявлена NULL-умеющей ровно
    затем, чтобы их различать.
    """
    assert calls_with_text([_call(101), _call(102)], set()) == 0


def test_only_calls_are_counted_even_when_another_event_shares_the_number():
    """Расшифровка бывает у звонка. Идентификаторы лент не сквозные.

    `source_id` комментария и `source_id` звонка живут в разных
    пространствах, и совпадение чисел — совпадение. Считать по одному
    номеру, не глядя на вид события, значило бы приписать клиенту
    расшифровку комментария.
    """
    events = [_call(101), _comment("101"), _todo("101")]
    assert calls_with_text(events, {101}) == 1


def test_a_call_the_cache_has_never_heard_of_is_not_counted():
    events = [_call(101), _call(102), _call(103)]
    assert calls_with_text(events, {101, 103, 999}) == 2
