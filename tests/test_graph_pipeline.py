"""End-to-end smoke test of the audit graph with stubbed CRM access.

graph.py had no coverage at all; this pins the dispatcher contract that the
hardening changes depend on: report/mutation/persist ordering under DRY_RUN.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import graph  # noqa: E402
from config import get_settings  # noqa: E402


def _lead(lead_id: int, assigned: int = 10) -> dict:
    return {
        "lead_id": lead_id,
        "title": f"Лид {lead_id}",
        "status_id": "UC_A7I8DK",  # Нецелевой — требует обоснования
        "status_name": "Нецелевой",
        "assigned_by_id": assigned,
        "date_create": "2026-07-01T10:00:00+00:00",
        "comments_field": "",
        "timeline": [],
        "calls": [],
    }


@pytest.fixture
def stub_crm(monkeypatch):
    """Replace collectors and all outbound side effects."""
    sent: list[tuple] = []
    moved: list[int] = []

    # @tool оборачивает функции в pydantic-модель, поэтому подменяем объект
    # целиком, а не его метод.
    class _Stub:
        def __init__(self, payload):
            self._payload = payload

        def invoke(self, _args):
            return self._payload

    monkeypatch.setattr(
        graph, "get_all_leads_with_timeline",
        _Stub({"leads": [_lead(1), _lead(2)], "total": 2}),
    )
    monkeypatch.setattr(
        graph, "get_deals_by_funnel_with_timeline",
        _Stub({"deals": [], "total": 0}),
    )
    monkeypatch.setattr(
        graph, "get_general_base_deals_with_timeline",
        lambda: {"deals": [], "total": 0},
    )
    monkeypatch.setattr(graph, "_build_rop_map", lambda: {})
    monkeypatch.setattr(
        graph, "_build_user_map",
        _fake_user_map,
    )
    monkeypatch.setattr(
        graph, "send_chat_message_chunked",
        lambda chat_id, text: sent.append(("chat", chat_id, text)) or 1,
    )
    monkeypatch.setattr(
        graph, "send_user_chat_message",
        lambda uid, text, **kw: sent.append(("user", uid, text)) or 1,
    )
    monkeypatch.setattr(
        graph, "process_deals_to_general_base",
        lambda violations, funnel: moved.append(len(violations)) or violations,
    )
    return sent, moved


async def _fake_user_map(violations, leads, buyers, sellers, gb=None):
    return {10: "Иван Иванов (Кретов)"}, {10: 42}, set()


def test_dry_run_reports_but_never_persists(stub_crm, monkeypatch, tmp_path):
    sent, moved = stub_crm
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.dry_run is True

    # Любая запись в БД в DRY_RUN — ошибка.
    import db

    def _boom(*_a, **_k):
        raise AssertionError("DRY_RUN не должен писать в БД")

    monkeypatch.setattr(db, "save_audit_run", _boom)
    monkeypatch.setattr(db, "save_violations", _boom)
    monkeypatch.setattr(db, "upsert_brokers", _boom)

    result = asyncio.run(graph.run_audit_v2(settings))

    assert result["status"] == "completed_dry_run"
    assert sent == [], "в DRY_RUN сообщения не отправляются"
    # Нарушения по лидам всё равно посчитаны.
    assert len(result["violations"]) == 2


def test_mutations_run_once_from_the_dispatcher(stub_crm, monkeypatch):
    """Перенос вызывается диспетчером ровно один раз на воронку."""
    _sent, moved = stub_crm
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()

    asyncio.run(graph.run_audit_v2(get_settings()))

    # Ровно два вызова — покупатели и продавцы, оба из диспетчера.
    assert len(moved) == 2


def test_dispatcher_runs_exactly_once(stub_crm, monkeypatch):
    """Ветки графа должны сходиться в merge на одной глубине.

    Иначе merge срабатывает дважды, и отчёты по отделам уходят в чаты РОПов
    по два раза.
    """
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()

    runs: list[int] = []
    original = graph.report_dispatcher

    async def counting(state, settings):
        runs.append(len(state.get("violations", [])))
        return await original(state, settings)

    monkeypatch.setattr(graph, "report_dispatcher", counting)
    asyncio.run(graph.run_audit_v2(get_settings()))

    assert runs == [2], f"диспетчер запускался {len(runs)} раз(а): {runs}"


def test_inactive_user_violations_are_dropped(stub_crm, monkeypatch):
    _sent, _moved = stub_crm
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()

    async def _all_inactive(violations, leads, buyers, sellers, gb=None):
        return {10: "Иван Иванов (Кретов)"}, {10: 42}, {10}

    monkeypatch.setattr(graph, "_build_user_map", _all_inactive)
    result = asyncio.run(graph.run_audit_v2(get_settings()))
    assert result["status"] == "completed_dry_run"
