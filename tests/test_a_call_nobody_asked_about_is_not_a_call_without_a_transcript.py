"""Догрузка расшифровок: кого спрашиваем у портала и кого не спрашиваем.

Шесть процентов расшифрованных звонков на 25.09 — это не «у портала нет
текста», а «мы у него не спрашивали»: старый путь перечислял звонки
фильтром ``OWNER_TYPE_ID: 2``, то есть только по сделкам, а на контактах
их больше. Здесь закреплено главное следствие — звонок на контакте
спрашивается наравне со звонком на сделке — и три решения, которые стоят
запросов к порталу: недозвон не спрашиваем, неизмеримое не трогаем,
повтор не вытесняет нетронутых.
"""

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from clients import pull_transcripts
from clients.pull_transcripts import (
    DEFAULT_BUDGET, Call, Plan, _ask, plan, read_calls,
)
from clients.schema import OWNER_CONTACT, OWNER_DEAL, clients_session, init_clients_db
from clients.transcripts import Cached
from clients.triage import MEANINGFUL_CALL_SEC

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
RETRY_HOURS = 24.0


def _call(activity_id: int, *, on: str = OWNER_DEAL, entity_id: int = 1,
          seconds: int | None = 90) -> Call:
    return Call(activity_id=activity_id, entity_type=on, entity_id=entity_id,
                seconds=seconds)


def _plan(calls, cached=None, **over):
    base = {"now": NOW, "retry_hours": RETRY_HOURS, "budget": None}
    base.update(over)
    return plan(calls, cached or {}, **base)


def _asked(made) -> set[int]:
    return {call.activity_id for call in made.candidates}


# ── ради чего всё затевалось ──────────────────────────────────────────

def test_a_call_that_hangs_on_a_contact_is_asked_about_too():
    """Звонок на контакте — такой же разговор, как звонок на сделке.

    Ровно этого не делал старый путь, и ровно в этом причина шести
    процентов: `crm.activity.list` звали с ``OWNER_TYPE_ID: 2``, а звонков
    на контактах на портале больше, чем на сделках.
    """
    made = _plan([_call(101, on=OWNER_DEAL), _call(202, on=OWNER_CONTACT)])

    assert _asked(made) == {101, 202}
    assert made.by_owner == {OWNER_DEAL: 1, OWNER_CONTACT: 1}


def test_a_call_on_a_contact_carries_no_deal_number():
    """У звонка на контакте сделки нет, и выдумывать её нечем.

    `entity_id` у такого события — идентификатор контакта. Записать его в
    колонку `deal_id` значило бы связать расшифровку со сделкой, которой
    не существует, и отдать её потом чужой карточке.
    """
    assert _call(202, on=OWNER_CONTACT, entity_id=555).deal_id == 0
    assert _call(101, on=OWNER_DEAL, entity_id=777).deal_id == 777


# ── о чём не спрашиваем ───────────────────────────────────────────────

def test_a_call_shorter_than_a_minute_is_never_asked_about():
    """У недозвона расшифровывать нечего.

    Спросив, мы получили бы пусто, записали бы `not_ready` и заняли бы
    этим звонком место в повторах — навсегда, потому что текст у него не
    появится никогда.
    """
    made = _plan([_call(101, seconds=MEANINGFUL_CALL_SEC - 1)])

    assert made.candidates == ()
    assert made.meaningful == 0


@pytest.mark.parametrize("seconds, asked", [
    (MEANINGFUL_CALL_SEC - 1, False),
    (MEANINGFUL_CALL_SEC, True),
])
def test_a_call_of_exactly_a_minute_counts_as_a_conversation(seconds, asked):
    """Граница порога та же, что у правила «нет данных».

    Там разговором считается звонок ``>= MEANINGFUL_CALL_SEC``. Сдвинув
    границу здесь на секунду, мы получили бы звонок, по которому правило
    требует текста, а догрузка за текстом не ходит, — и клиент навсегда
    остался бы в «нет данных» без единой попытки это исправить.
    """
    assert bool(_plan([_call(101, seconds=seconds)]).candidates) is asked


def test_a_call_whose_length_cannot_be_measured_is_left_alone():
    """Порог задан в секундах. Применять его к неизвестному — выдумывать."""
    made = _plan([_call(101, seconds=None)])

    assert made.candidates == ()
    assert made.meaningful == 0


def test_a_call_that_already_has_text_is_not_asked_again():
    cached = {101: Cached(status="ok", fetched_at="2026-01-01T00:00:00+00:00")}
    made = _plan([_call(101)], cached)

    assert made.candidates == ()
    assert made.with_text == 1


@pytest.mark.parametrize("hours_ago, asked", [(1, False), (RETRY_HOURS + 1, True)])
def test_a_call_asked_about_recently_waits_its_turn(hours_ago, asked):
    """Портал ответил пусто час назад — за этот час ничего не изменилось.

    Без этого каждый прогон дёргал бы портал по одним и тем же звонкам,
    у которых расшифровки не появится никогда, и бюджет прогона уходил бы
    на них целиком.
    """
    asked_at = (NOW - timedelta(hours=hours_ago)).isoformat()
    cached = {101: Cached(status="not_ready", fetched_at=asked_at)}
    made = _plan([_call(101)], cached)

    assert bool(made.candidates) is asked
    assert made.waiting == (0 if asked else 1)


def test_a_cached_row_without_a_timestamp_is_asked_about():
    """Когда спрашивали — неизвестно, значит считаем, что давно."""
    cached = {101: Cached(status="not_ready", fetched_at="")}

    assert _asked(_plan([_call(101)], cached)) == {101}


# ── порядок и бюджет ──────────────────────────────────────────────────

def test_a_call_nobody_ever_asked_about_goes_before_one_that_was():
    """Нетронутый звонок вперёд повтора: у него шанс получить текст выше.

    Отдав бюджет повторам, прогон потратил бы ночь на звонки, по которым
    портал уже один раз ответил пусто, и не дошёл бы до тех, про кого не
    спрашивали ни разу.
    """
    old = (NOW - timedelta(days=30)).isoformat()
    cached = {101: Cached(status="not_ready", fetched_at=old)}
    made = _plan([_call(101), _call(202)], cached, budget=1)

    assert _asked(made) == {202}


def test_the_budget_caps_the_asking_without_hiding_what_was_left():
    """Предел режет список запросов, а не правду о портфеле.

    Число «спросим» под бюджетом, остальные числа — по всему портфелю:
    иначе прогон с бюджетом 10 сообщал бы, что расшифровать осталось
    десять звонков, и очередь выглядела бы разобранной.
    """
    calls = [_call(number) for number in range(101, 111)]
    made = _plan(calls, budget=3)

    assert len(made.candidates) == 3
    assert made.calls == 10 and made.meaningful == 10


# ── что уходит в кэш ──────────────────────────────────────────────────

def test_a_deal_link_the_cache_already_knows_is_not_replaced_by_a_zero(monkeypatch):
    """Звонок на контакте не отбирает у сделки уже записанную привязку.

    `deal_id` у такого звонка ноль, а перезапись строки ставит то, что ей
    передали. Затерев известную сделку нулём, мы отняли бы расшифровку у
    `list_call_transcripts_for_deal` — она ищет ровно по этой колонке.
    """
    written = []
    monkeypatch.setattr("transcripts.fetch_transcript_from_api",
                        lambda activity_id: ("разговор", "ok"))
    monkeypatch.setattr("db.upsert_call_transcript",
                        lambda **kwargs: written.append(kwargs))

    _ask([_call(202, on=OWNER_CONTACT, entity_id=555)],
         {202: Cached(status="not_ready", fetched_at="", deal_id=42)}, moment=NOW)

    assert written[0]["deal_id"] == 42, "известная сделка обязана пережить перезапись"
    assert written[0]["text"] == "разговор"


def test_an_empty_answer_is_recorded_so_it_is_not_asked_again_tomorrow(monkeypatch):
    """Портал ответил пусто — это тоже ответ, и он обязан попасть в кэш.

    Не записав его, прогон завтра спросит про тот же звонок снова, и так
    каждую ночь: «рано повторять» считается по отметке времени, а её
    ставит только запись.
    """
    written = []
    monkeypatch.setattr("transcripts.fetch_transcript_from_api",
                        lambda activity_id: (None, "not_ready"))
    monkeypatch.setattr("db.upsert_call_transcript",
                        lambda **kwargs: written.append(kwargs))

    counted = _ask([_call(101)], {}, moment=NOW)

    assert len(written) == 1 and written[0]["status"] == "not_ready"
    assert written[0]["fetched_at"] == NOW.isoformat()
    assert counted == {"спрошено": 1, "текст получен": 0,
                       "портал ответил пусто": 1, "ошибок": 0}


# ── список звонков берётся из книги ───────────────────────────────────

def test_the_call_list_comes_from_the_book_and_not_from_the_portal(tmp_path):
    """Идентификаторы уже собраны — ходить за ними в портал незачем.

    Старый путь звал `crm.activity.list` на каждую сделку: тысячи запросов
    ради списка, который лежит в `client_events`.
    """
    book = tmp_path / "clients.db"
    init_clients_db(book)
    started, ended = "2026-09-01T10:00:00+00:00", "2026-09-01T10:02:00+00:00"
    with clients_session(book) as conn:
        conn.executemany(
            "INSERT INTO client_events(client_key, at, kind, source_id,"
            " entity_type, entity_id, payload_json) VALUES ('k', ?, ?, ?, ?, ?, ?)",
            [
                (started, "call", "101", OWNER_DEAL, 777,
                 f'{{"start_time": "{started}", "end_time": "{ended}"}}'),
                (started, "call", "202", OWNER_CONTACT, 555,
                 f'{{"start_time": "{started}", "end_time": "{ended}"}}'),
                (started, "comment", "c1", OWNER_DEAL, 777, "{}"),
                # Дело, а не звонок, и идентификатор у него числовой:
                # с нечисловым проверялся бы разбор строки, а не фильтр.
                (started, "activity", "303", OWNER_DEAL, 777,
                 f'{{"start_time": "{started}", "end_time": "{ended}"}}'),
            ],
        )

    with clients_session(book, readonly=True) as conn:
        calls = read_calls(conn)

    assert {call.activity_id for call in calls} == {101, 202}, "встреча и письмо не звонки"
    assert {call.seconds for call in calls} == {120}
    assert {call.deal_id for call in calls} == {777, 0}


# ── предел прогона доезжает от ключа до среза ─────────────────────────
#
# Проверяется проводка, а не сама отсечка: та закреплена выше. Цена
# обрыва — прогон, который обещал снять предел и тихо оставил его на
# месте, а заметить это можно только по числу запросов на живом портале.

def _budget_reaching_pull(monkeypatch, argv) -> dict:
    seen: dict = {}
    monkeypatch.setattr(pull_transcripts, "pull", lambda **kw: seen.update(kw) or {})
    monkeypatch.setattr(sys, "argv", ["pull_transcripts", *argv])

    assert pull_transcripts.main() == 0
    return seen


def test_asking_for_no_limit_really_removes_the_limit(monkeypatch):
    """`--budget 0` обязан снять предел, а не вернуть умолчание.

    Так и было: `None` означал «вызывающий не сказал» в одном месте и «без
    предела» в другом, и прогон с `--budget 0` спросил ровно четыреста
    звонков вместо всех четырёхсот двадцати.
    """
    assert _budget_reaching_pull(monkeypatch, ["--budget", "0"])["budget"] is None


def test_a_negative_budget_does_not_quietly_cut_from_the_end(monkeypatch):
    """`candidates[:-5]` отрезал бы пятерых с конца и никому не сказал."""
    assert _budget_reaching_pull(monkeypatch, ["--budget", "-5"])["budget"] is None


def test_a_run_without_the_flag_keeps_the_nightly_budget(monkeypatch):
    """Умолчание — обещание не занимать ночь целиком, и оно обязано жить."""
    assert _budget_reaching_pull(monkeypatch, [])["budget"] == DEFAULT_BUDGET


def test_the_budget_reaches_the_plan_unchanged(monkeypatch):
    """Между ключом и отсечкой значение не подменяется по дороге.

    Подменялось ровно здесь, внутри `pull`, и предыдущие три теста этого
    бы не увидели: они смотрят на вход в `pull`, а терялось значение
    после него.
    """
    seen: dict = {}

    @contextmanager
    def _no_book(*args, **kwargs):
        yield None

    monkeypatch.setattr(pull_transcripts, "clients_session", _no_book)
    monkeypatch.setattr(pull_transcripts, "read_calls", lambda conn: [])
    monkeypatch.setattr(pull_transcripts, "cache_state", lambda ids: {})
    monkeypatch.setattr(pull_transcripts, "plan",
                        lambda *args, **kw: seen.update(kw) or Plan())

    pull_transcripts.pull(dry_run=True, budget=None)

    assert seen["budget"] is None
