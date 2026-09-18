"""Расшифровки: за что платим запросом, а за что не платим никогда.

Расшифровка стоит одного обращения к порталу на звонок, и это самая дорогая
часть выгрузки: звонков по портфелю три с половиной тысячи. Поэтому порядок
проверок задан ценой — длительность (бесплатно, обе границы уже в ответе),
кэш (диск), портал (запрос). Переставить их местами значит платить за то,
что уже известно.

Отсечка по длительности — не экономия ради экономии. На боевом портале 30 %
звонков длятся ровно ноль секунд, ещё 31 % — меньше тридцати. Разговора в
них нет, и спрашивать по ним расшифровку нечего.

Отдельная забота — статус. У читающей модели есть правило: карточка с
незабранным разговором на активной стадии получает «данных недостаточно», а
не «работа не ведётся». Если короткие звонки попадут в тот же статус, что и
незабранные, правило сработает на трети портфеля и обесценится.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import db  # noqa: E402
import dossier  # noqa: E402

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
DEAL_ID = 16722


def _call(activity_id: int, seconds: int, *, created: datetime | None = None) -> dict:
    start = created or (NOW - timedelta(days=2))
    return {
        "ID": activity_id,
        "TYPE_ID": 2,
        "CREATED": start.isoformat(),
        "START_TIME": start.isoformat(),
        "END_TIME": (start + timedelta(seconds=seconds)).isoformat(),
        "DIRECTION": 2,
    }


@pytest.fixture
def asked(monkeypatch):
    """Счётчик обращений к порталу за расшифровкой."""
    calls: list[int] = []

    def _fetch(activity_id: int):
        calls.append(activity_id)
        return None, "not_ready"

    monkeypatch.setattr(dossier, "fetch_transcript_from_api", _fetch)
    return calls


# ── Отсечка по длительности ────────────────────────────────────────────
def test_a_zero_second_call_is_never_asked_about(asked):
    resolver = dossier.TranscriptResolver(budget=100)
    status, text = resolver.resolve(_call(1, 0), DEAL_ID, None)

    assert status == dossier.T_TOO_SHORT
    assert text == ""
    assert asked == [], "за пустой звонок платить запросом нельзя"


def test_a_call_of_fifty_nine_seconds_is_still_too_short(asked):
    resolver = dossier.TranscriptResolver(budget=100)
    assert resolver.resolve(_call(1, 59), DEAL_ID, None)[0] == dossier.T_TOO_SHORT
    assert asked == []


def test_a_call_of_a_minute_is_worth_a_request(asked):
    resolver = dossier.TranscriptResolver(budget=100)
    assert resolver.resolve(_call(1, 60), DEAL_ID, None)[0] == dossier.T_ABSENT
    assert asked == [1]


def test_a_call_without_an_end_time_counts_as_empty(asked):
    """Неизвестную длительность нельзя записывать в длинные.

    Иначе очередь на расшифровку наберётся из звонков, о которых неизвестно
    даже, состоялись ли они, и человек будет отстреливать пустоту.
    """
    activity = _call(1, 100)
    activity["END_TIME"] = ""

    resolver = dossier.TranscriptResolver(budget=100)
    assert resolver.resolve(activity, DEAL_ID, None)[0] == dossier.T_TOO_SHORT
    assert asked == []


def test_the_short_call_status_is_not_the_same_as_not_fetched():
    """Ради этого различия и заведён отдельный статус.

    «Короткий» — это политика: мы не спросим никогда. «Не забрали» — это
    наша нехватка, и она значит «данных недостаточно». Свести их в одно
    значит выдать «данных недостаточно» трети портфеля.
    """
    assert dossier.T_TOO_SHORT != dossier.T_DEFERRED


# ── Кэш и бюджет ───────────────────────────────────────────────────────
def test_a_cached_transcript_costs_nothing(asked):
    db.init_db()
    db.upsert_call_transcript(
        activity_id=1, deal_id=DEAL_ID, text="Клиент готов смотреть",
        status="ok", fetched_at=NOW.isoformat(), chars=21,
    )

    resolver = dossier.TranscriptResolver(budget=100)
    status, text = resolver.resolve(_call(1, 120), DEAL_ID, None)

    assert status == dossier.T_OK
    assert text == "Клиент готов смотреть"
    assert asked == [], "за то, что уже лежит на диске, не платят"


def test_an_exhausted_budget_defers_instead_of_lying(asked):
    """Бюджет кончился — это «не забрали», а не «текста нет»."""
    resolver = dossier.TranscriptResolver(budget=1)
    first = resolver.resolve(_call(1, 120), DEAL_ID, None)
    second = resolver.resolve(_call(2, 120), DEAL_ID, None)

    assert first[0] == dossier.T_ABSENT
    assert second[0] == dossier.T_DEFERRED
    assert asked == [1]
    assert resolver.deferred_budget == 1


def test_a_read_failure_is_counted_apart_from_a_spent_budget(monkeypatch):
    """Сетевые сбои, смешанные с нормой, обнаруживаются, когда станет больно."""
    monkeypatch.setattr(
        dossier, "fetch_transcript_from_api", lambda _id: (None, "error"),
    )
    resolver = dossier.TranscriptResolver(budget=10)

    assert resolver.resolve(_call(1, 120), DEAL_ID, None)[0] == dossier.T_DEFERRED
    assert resolver.deferred_error == 1
    assert resolver.deferred_budget == 0


# ── Очередь на запуск ──────────────────────────────────────────────────
def _row(**over) -> dict:
    row = {
        "id": DEAL_ID,
        "stage_id": "C18:UC_UFPFKK",
        "stage_history": [],
        "counters": {"comments_by_assignee": 1},
        "calls": [{
            "activity_id": 101, "duration": 180,
            "date": (NOW - timedelta(days=2)).isoformat(),
            "transcript_status": dossier.T_ABSENT,
        }],
    }
    row.update(over)
    return row


def test_a_long_call_without_text_becomes_a_queue_line():
    queue = dossier.collect_queue([_row()], {}, budget=100, now=NOW)

    assert len(queue) == 1
    assert queue[0]["activity_id"] == 101
    assert queue[0]["deal_id"] == DEAL_ID
    assert queue[0]["attempts"] == 0


def test_a_call_that_already_has_text_is_not_queued():
    row = _row()
    row["calls"][0]["transcript_status"] = dossier.T_OK

    assert dossier.collect_queue([row], {}, budget=100, now=NOW) == []


def test_a_settled_card_is_not_worth_a_transcript():
    """На «Задатке» торг кончился: расшифровка ничего уже не изменит."""
    assert dossier.collect_queue(
        [_row(stage_id="C18:UC_RUCRAH")], {}, budget=100, now=NOW,
    ) == []


def test_a_silent_broker_goes_to_the_front_of_the_queue():
    """Карточка, где брокер не написал ни строки, — первая, что надо услышать."""
    loud = _row(id=1, counters={"comments_by_assignee": 4})
    loud["calls"] = [dict(loud["calls"][0], activity_id=201)]
    silent = _row(id=2, counters={"comments_by_assignee": 0})
    silent["calls"] = [dict(silent["calls"][0], activity_id=202)]

    queue = dossier.collect_queue([loud, silent], {}, budget=100, now=NOW)

    assert [c["activity_id"] for c in queue] == [202, 201]
    assert queue[0]["priority"] == dossier.PRIORITY_ASSIGNEE_SILENT


def test_a_recent_stage_move_beats_a_quiet_card():
    quiet = _row(id=1)
    quiet["calls"] = [dict(quiet["calls"][0], activity_id=201)]
    moved = _row(id=2, stage_history=[
        {"date": (NOW - timedelta(days=2)).isoformat()},
    ])
    moved["calls"] = [dict(moved["calls"][0], activity_id=202)]

    queue = dossier.collect_queue([quiet, moved], {}, budget=100, now=NOW)

    assert queue[0]["activity_id"] == 202
    assert queue[0]["priority"] == dossier.PRIORITY_RECENT_STAGE


def test_the_queue_is_cut_to_the_budget():
    """Список на три тысячи строк — это список, который не отработают вовсе."""
    rows = []
    for index in range(5):
        row = _row(id=index)
        row["calls"] = [dict(row["calls"][0], activity_id=300 + index)]
        rows.append(row)

    assert len(dossier.collect_queue(rows, {}, budget=2, now=NOW)) == 2


def test_the_same_call_is_not_queued_twice_in_one_day():
    """Запуск асинхронный: текст появляется позже, а не к следующему прогону."""
    launches = {101: {
        "attempts": 1,
        "last_queued_at": (NOW - timedelta(hours=3)).isoformat(),
        "outcome": "",
    }}

    assert dossier.collect_queue([_row()], launches, budget=100, now=NOW) == []


def test_a_day_later_the_same_call_may_be_queued_again():
    launches = {101: {
        "attempts": 1,
        "last_queued_at": (NOW - timedelta(days=2)).isoformat(),
        "outcome": "",
    }}

    queue = dossier.collect_queue([_row()], launches, budget=100, now=NOW)
    assert len(queue) == 1 and queue[0]["attempts"] == 1


def test_after_two_attempts_the_call_is_left_alone():
    """Если после второй постановки текста нет, третья ничего не изменит."""
    launches = {101: {
        "attempts": dossier.MAX_LAUNCH_ATTEMPTS,
        "last_queued_at": (NOW - timedelta(days=10)).isoformat(),
        "outcome": "",
    }}

    assert dossier.collect_queue([_row()], launches, budget=100, now=NOW) == []


def test_the_third_look_writes_the_call_off(monkeypatch):
    """Две постановки без результата — и звонок перестаёт быть надеждой."""
    db.init_db()
    row = _row()
    launches = {101: {"attempts": 2, "last_queued_at": "", "outcome": ""}}

    failed = dossier.settle_launches([row], launches)

    assert failed == 1
    assert row["calls"][0]["transcript_status"] == dossier.T_FAILED


def test_a_call_whose_text_arrived_closes_its_queue_line():
    db.init_db()
    db.record_transcript_launch(101, DEAL_ID, NOW.isoformat())
    row = _row()
    row["calls"][0]["transcript_status"] = dossier.T_OK
    launches = {101: {"attempts": 1, "last_queued_at": "", "outcome": ""}}

    dossier.settle_launches([row], launches)

    assert db.get_transcript_launch(101)["outcome"] == dossier.T_OK


# ── Точка расширения ───────────────────────────────────────────────────
def test_the_server_side_launch_is_declared_but_not_pretended():
    """Заглушка обязана падать, а не молча ничего не делать.

    Тихая заглушка — это очередь, которая «отстреливается» каждый прогон и
    никогда не даёт результата, и заметить это можно только через месяц.
    """
    with pytest.raises(NotImplementedError):
        dossier.launch_transcription(101, DEAL_ID)
