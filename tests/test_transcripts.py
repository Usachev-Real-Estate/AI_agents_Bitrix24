"""Tests for call transcript cache."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import db  # noqa: E402
import transcripts  # noqa: E402


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "violations.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    yield db_path


def test_fetch_and_cache_stores_ok_transcript(temp_db, monkeypatch):
    calls = [{"ID": 101, "CREATED": "2026-08-20T10:00:00+03:00"}]

    monkeypatch.setattr(transcripts, "list_call_activities", lambda deal_id: calls)
    monkeypatch.setattr(
        transcripts,
        "fetch_transcript_from_api",
        lambda activity_id: ("Клиент ищет двушку", transcripts.STATUS_OK),
    )

    rows = transcripts.fetch_and_cache(5001, dry_run=False)
    assert len(rows) == 1
    assert rows[0]["status"] == transcripts.STATUS_OK
    assert rows[0]["text"] == "Клиент ищет двушку"

    cached = db.get_call_transcript(101)
    assert cached is not None
    assert cached["deal_id"] == 5001
    assert cached["chars"] == len("Клиент ищет двушку")


def test_not_ready_cached_and_not_refetched_within_hour(temp_db, monkeypatch):
    now = datetime.now(timezone.utc)
    db.upsert_call_transcript(
        activity_id=202,
        deal_id=5002,
        text="",
        status=transcripts.STATUS_NOT_READY,
        fetched_at=now.isoformat(),
        chars=0,
    )
    calls = [{"ID": 202, "CREATED": "2026-08-20T11:00:00+03:00"}]
    monkeypatch.setattr(transcripts, "list_call_activities", lambda deal_id: calls)

    api_calls: list[int] = []

    def _api(activity_id: int):
        api_calls.append(activity_id)
        return None, transcripts.STATUS_NOT_READY

    monkeypatch.setattr(transcripts, "fetch_transcript_from_api", _api)
    rows = transcripts.fetch_and_cache(5002, dry_run=False)
    assert len(rows) == 1
    assert rows[0]["status"] == transcripts.STATUS_NOT_READY
    assert api_calls == []


def test_not_ready_refetched_after_retry_window(temp_db, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    db.upsert_call_transcript(
        activity_id=303,
        deal_id=5003,
        text="",
        status=transcripts.STATUS_NOT_READY,
        fetched_at=old.isoformat(),
        chars=0,
    )
    calls = [{"ID": 303, "CREATED": "2026-08-20T12:00:00+03:00"}]
    monkeypatch.setattr(transcripts, "list_call_activities", lambda deal_id: calls)
    monkeypatch.setattr(
        transcripts,
        "fetch_transcript_from_api",
        lambda activity_id: ("готовый текст", transcripts.STATUS_OK),
    )

    rows = transcripts.fetch_and_cache(5003, dry_run=False)
    assert rows[0]["status"] == transcripts.STATUS_OK
    assert rows[0]["text"] == "готовый текст"


def test_dry_run_still_caches(temp_db, monkeypatch):
    """Кэш — локальная копия read-only данных, в CRM он ничего не меняет.

    Без записи в DRY_RUN тестовый прогон заново тянул бы тысячи расшифровок
    из Битрикса, то есть самый частый режим был бы самым дорогим.
    """
    calls = [{"ID": 404, "CREATED": "2026-08-20T13:00:00+03:00"}]
    monkeypatch.setattr(transcripts, "list_call_activities", lambda deal_id: calls)
    fetches: list[int] = []

    def _fake(activity_id: int):
        fetches.append(activity_id)
        return "tmp", transcripts.STATUS_OK

    monkeypatch.setattr(transcripts, "fetch_transcript_from_api", _fake)

    transcripts.fetch_and_cache(5004, dry_run=True)
    cached = db.get_call_transcript(404)
    assert cached is not None
    assert cached["text"] == "tmp"

    # Повторный прогон берёт из кэша и в Битрикс не ходит.
    transcripts.fetch_and_cache(5004, dry_run=True)
    assert fetches == [404]


def test_call_time_survives_the_cache(temp_db, monkeypatch):
    """Время звонка должно пережить кэширование, иначе ломается хронология."""
    calls = [{"ID": 505, "CREATED": "2026-06-01T09:30:00+03:00"}]
    monkeypatch.setattr(transcripts, "list_call_activities", lambda deal_id: calls)
    monkeypatch.setattr(
        transcripts,
        "fetch_transcript_from_api",
        lambda activity_id: ("текст", transcripts.STATUS_OK),
    )

    transcripts.fetch_and_cache(5005)
    assert db.get_call_transcript(505)["activity_created"] == (
        "2026-06-01T09:30:00+03:00"
    )

    # И при чтении из кэша тоже — а не время скачивания.
    rows = transcripts.fetch_and_cache(5005)
    assert rows[0]["activity_created"] == "2026-06-01T09:30:00+03:00"


def test_activity_list_failure_does_not_raise(temp_db, monkeypatch):
    """Сбой API по одной карточке не должен ронять прогон."""
    def _boom(method, params):
        raise RuntimeError("Bitrix недоступен")

    monkeypatch.setattr(transcripts, "_bx_get_all_sync", _boom)
    assert transcripts.fetch_and_cache(5006) == []
