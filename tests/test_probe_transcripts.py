"""Разведка звонков: длительность выводится, а не приходит готовой.

Скрипт отвечает на один вопрос — сколько запросов за расшифровками придётся
сделать и сколько звонков останется без текста. Весь ответ держится на
длительности, а готовой длительности в ответе портала нет: CALL_DURATION
живёт в статистике телефонии, а она на этом портале не наполняется (222
записи против 3555 звонков — звонки регистрирует внешняя АТС через
REST-приложение). Остаётся разность START_TIME и END_TIME, и ошибка в ней
сдвигает всю оценку.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from probe_transcripts import _bucket, call_duration_sec  # noqa: E402

START = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


def _call(seconds: int | None, *, start: datetime | None = START) -> dict:
    activity: dict = {"ID": 1}
    if start is not None:
        activity["START_TIME"] = start.isoformat()
    if seconds is not None and start is not None:
        activity["END_TIME"] = (start + timedelta(seconds=seconds)).isoformat()
    return activity


# ── Длительность ───────────────────────────────────────────────────────
def test_duration_is_the_gap_between_the_two_stamps():
    assert call_duration_sec(_call(154)) == 154


def test_a_call_without_an_end_stamp_counts_as_empty():
    """Неизвестную длительность нельзя записывать в длинные.

    Иначе очередь на расшифровку наберётся из звонков, о которых неизвестно
    даже, состоялись ли они, и отстреливать её будет человек.
    """
    assert call_duration_sec(_call(None)) == 0


def test_a_call_without_a_start_stamp_counts_as_empty():
    activity = {"ID": 1, "END_TIME": START.isoformat()}
    assert call_duration_sec(activity) == 0


def test_stamps_in_the_wrong_order_do_not_go_negative():
    """Отрицательная длительность — это порченые данные, а не «минус минута»."""
    activity = _call(0)
    activity["END_TIME"] = (START - timedelta(seconds=90)).isoformat()
    assert call_duration_sec(activity) == 0


def test_a_missed_call_lasts_zero():
    assert call_duration_sec(_call(0)) == 0


# ── Корзины ────────────────────────────────────────────────────────────
def test_the_buckets_split_where_the_filter_cuts():
    """Граница корзин совпадает с порогом отсечки — иначе отчёт врёт.

    По отчёту решают, какой бюджет ставить. Если «>=60» в отчёте означает
    не то же, что «>=60» в модуле, число запросов окажется другим, и узнают
    об этом на боевом прогоне.
    """
    assert _bucket(0) == "0"
    assert _bucket(1) == "1-29"
    assert _bucket(29) == "1-29"
    assert _bucket(30) == "30-59"
    assert _bucket(59) == "30-59"
    assert _bucket(60) == ">=60"
    assert _bucket(3600) == ">=60"


def test_the_probe_threshold_matches_the_module():
    """Два места с числом 60 обязаны означать одно и то же число."""
    import dossier
    import probe_transcripts

    assert probe_transcripts.LONG_CALL_SEC == dossier.LONG_CALL_SEC
