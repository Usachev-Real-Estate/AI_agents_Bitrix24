"""Tests for buyer client-state agent."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import db  # noqa: E402
from client_state import (  # noqa: E402
    analyze_buyer_deal,
    build_evidence_events,
    compute_content_hash,
    filter_new_events,
    _normalize_state,
    _parse_state_json,
)
from masking import build_mask_map  # noqa: E402


class _FakeLLM:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def invoke(self, messages: list[Any]) -> Any:
        class _Resp:
            content = json.dumps(self.payload, ensure_ascii=False)

        return _Resp()


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "violations.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    yield db_path


def test_parse_state_json_with_trailing_text():
    raw = '{"client_goal":"x","confidence":0.5} extra'
    parsed = _parse_state_json(raw)
    assert parsed is not None
    assert parsed["client_goal"] == "x"


def test_normalize_state_clamps_confidence():
    state = _normalize_state({"confidence": 5, "risk": "HIGH", "recoverable": 1})
    assert state["confidence"] == 1.0
    assert state["risk"] == "high"
    assert state["recoverable"] is True


def test_content_hash_stable():
    events = [{"kind": "comment", "id": 1, "created": "t", "text": "a"}]
    assert compute_content_hash(events) == compute_content_hash(events)


def test_filter_new_events_by_watermark():
    events = [
        {"created": "2026-08-19T10:00:00+00:00", "text": "old"},
        {"created": "2026-08-21T10:00:00+00:00", "text": "new"},
    ]
    fresh = filter_new_events(events, "2026-08-20T00:00:00+00:00")
    assert len(fresh) == 1
    assert fresh[0]["text"] == "new"


def _base_deal(deal_id: int) -> dict:
    return {
        "ID": deal_id,
        "TITLE": f"Сделка {deal_id}",
        "STAGE_ID": "C18:PREPARATION",
        "timeline": [],
        "activities": [],
        "transcripts": [],
        "contacts": [],
        "evidence_incomplete": False,
    }


def test_skip_evidence_incomplete():
    deal = _base_deal(1)
    deal["evidence_incomplete"] = True
    result = analyze_buyer_deal(deal, prepared=True)
    assert result["skipped"] is True
    assert result["reason"] == "evidence_incomplete"


def test_skip_unchanged_when_hash_matches(temp_db, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    from config import get_settings

    get_settings.cache_clear()
    deal = _base_deal(2)
    deal["timeline"] = [{"id": 1, "created": "2026-08-20", "comment": "бюджет 20 млн"}]
    mask = build_mask_map(deal)
    events = build_evidence_events(deal["timeline"], [], [], mask)
    content_hash = compute_content_hash(events)
    state = {"client_goal": "квартира", "confidence": 0.8, "recoverable": True}
    db.save_client_state(
        deal_id=2,
        state_json=json.dumps(state),
        confidence=0.8,
        content_hash=content_hash,
        analyzed_at="2026-08-20T09:00:00+00:00",
        model="test",
    )
    result = analyze_buyer_deal(deal, prepared=True)
    assert result["skipped"] is True
    assert result["reason"] == "unchanged"


def test_dry_run_does_not_persist_state(temp_db, monkeypatch):
    deal = _base_deal(3)
    deal["timeline"] = [{"id": 1, "created": "2026-08-20", "comment": "район Хамовники"}]
    llm = _FakeLLM(
        {
            "client_goal": "2к в Хамовниках",
            "situation": "активный подбор",
            "last_event": {"what": "комментарий", "when": "2026-08-20"},
            "next_step": {"what": "показ", "when": "2026-08-22", "who": "broker"},
            "blockers": [],
            "risk": "low",
            "recoverable": True,
            "missing": [],
            "confidence": 0.85,
            "evidence": ["район Хамовники"],
        },
    )
    monkeypatch.setenv("DRY_RUN", "true")
    from config import get_settings

    get_settings.cache_clear()
    result = analyze_buyer_deal(deal, prepared=True, llm=llm)
    assert result["skipped"] is False
    assert db.get_client_state(3) is None


def test_three_synthetic_cards(monkeypatch):
    """Full, empty, and contradictory cards — mocked LLM outputs."""
    scenarios = [
        {
            "name": "полная",
            "deal": {
                **_base_deal(101),
                "timeline": [
                    {
                        "id": 1,
                        "created": "2026-08-18T10:00:00+03:00",
                        "comment": (
                            "Бюджет до 25 млн, ищет 2к в Хамовниках, "
                            "ипотека одобрена, показ в субботу"
                        ),
                    },
                ],
                "transcripts": [
                    {
                        "activity_id": 11,
                        "status": "ok",
                        "text": "Клиент подтвердил бюджет 25 млн и район",
                        "activity_created": "2026-08-17T15:00:00+03:00",
                    },
                ],
            },
            "llm": {
                "client_goal": "2к, до 25 млн, Хамовники",
                "situation": "ипотека одобрена, готов к показу",
                "last_event": {"what": "подтвердил бюджет", "when": "2026-08-17"},
                "next_step": {"what": "показ", "when": "2026-08-20", "who": "client"},
                "blockers": [],
                "risk": "low",
                "recoverable": True,
                "missing": [],
                "confidence": 0.9,
                "evidence": ["Бюджет до 25 млн"],
            },
            "expect_recoverable": True,
        },
        {
            "name": "пустая",
            "deal": {
                **_base_deal(102),
                "timeline": [
                    {
                        "id": 2,
                        "created": "2026-08-19T12:00:00+03:00",
                        "comment": "созвонился, договорились",
                    },
                ],
            },
            "llm": {
                "client_goal": "unknown",
                "situation": "unknown",
                "last_event": {"what": "созвонился", "when": "2026-08-19"},
                "next_step": {"what": "unknown", "when": "unknown", "who": "unknown"},
                "blockers": [],
                "risk": "medium",
                "recoverable": False,
                "missing": ["бюджет", "район", "сроки", "мотивация"],
                "confidence": 0.1,
                "evidence": ["созвонился, договорились"],
            },
            "expect_recoverable": False,
        },
        {
            "name": "противоречивая",
            "deal": {
                **_base_deal(103),
                "timeline": [
                    {
                        "id": 3,
                        "created": "2026-08-10T10:00:00+03:00",
                        "comment": "Бюджет 15 млн, только новостройки",
                    },
                    {
                        "id": 4,
                        "created": "2026-08-15T10:00:00+03:00",
                        "comment": "Готов рассматривать вторичку до 30 млн",
                    },
                ],
            },
            "llm": {
                "client_goal": "противоречивые требования по бюджету и типу жилья",
                "situation": "бюджет и тип объекта не согласованы",
                "last_event": {"what": "вторичка до 30 млн", "when": "2026-08-15"},
                "next_step": {
                    "what": "уточнить бюджет и тип",
                    "when": "unknown",
                    "who": "broker",
                },
                "blockers": ["противоречие в комментариях"],
                "risk": "high",
                "recoverable": True,
                "missing": ["актуальный бюджет", "тип жилья"],
                "confidence": 0.55,
                "evidence": ["Бюджет 15 млн", "вторичку до 30 млн"],
            },
            "expect_recoverable": True,
        },
    ]

    outputs: list[str] = []
    for scenario in scenarios:
        deal = scenario["deal"]
        result = analyze_buyer_deal(
            deal,
            prepared=True,
            llm=_FakeLLM(scenario["llm"]),
        )
        state = result["state"]
        assert state is not None
        assert state["recoverable"] is scenario["expect_recoverable"]
        outputs.append(
            f"{scenario['name']}: recoverable={state['recoverable']} "
            f"confidence={state['confidence']} goal={state['client_goal']!r}",
        )

    # Visible in pytest -vv output for manual review.
    assert len(outputs) == 3
    print("\n".join(outputs))


# ── Маскировка не должна обходиться через сохранённое состояние ────────
def _card_with_contact() -> dict[str, Any]:
    return {
        "ID": 900,
        "TITLE": "Покупка",
        "STAGE_ID": "C18:NEW",
        "contacts": [{
            "ID": 5,
            "NAME": "Ирина",
            "LAST_NAME": "Логутина",
            "PHONE": [{"VALUE": "+7 916 123-45-67"}],
            "EMAIL": [],
        }],
        "timeline": [{
            "author_id": 10,
            "created": "2026-08-14T10:00:00+03:00",
            "comment": "Созвонился с Ириной Логутиной, ищет 2к до 25 млн",
        }],
        "activities": [],
        "transcripts": [],
        "evidence_incomplete": False,
    }


def _state_payload(evidence: list[str], **over: Any) -> dict[str, Any]:
    payload = {
        "client_goal": "2к до 25 млн",
        "situation": "подбор",
        "last_event": {"what": "звонок", "when": "2026-08-14"},
        "next_step": {"what": "показ", "when": "неделя", "who": "broker"},
        "blockers": [],
        "risk": "low",
        "recoverable": True,
        "missing": [],
        "confidence": 0.9,
        "evidence": evidence,
    }
    payload.update(over)
    return payload


def test_stored_state_stays_masked(temp_db, monkeypatch):
    """В БД уходит маска: на следующем прогоне она вернётся в модель."""
    monkeypatch.setenv("DRY_RUN", "false")
    from config import get_settings
    get_settings.cache_clear()

    quote = "Созвонился с КЛИЕНТ_1, ищет 2к до 25 млн"
    llm = _FakeLLM(_state_payload([quote]))
    result = analyze_buyer_deal(_card_with_contact(), llm=llm, prepared=True)

    stored = json.loads(db.get_client_state(900)["state_json"])
    assert stored["evidence"] == [quote]
    for secret in ("Ирин", "Логутин", "916"):
        assert secret not in json.dumps(stored, ensure_ascii=False)

    # Человеку возвращается расшифрованная копия.
    assert "Ирина" in result["state"]["evidence"][0]


def test_invented_quote_is_dropped_and_confidence_capped(temp_db, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    from config import get_settings
    get_settings.cache_clear()

    llm = _FakeLLM(_state_payload(["клиент готов внести задаток завтра"]))
    result = analyze_buyer_deal(_card_with_contact(), llm=llm, prepared=True)

    assert result["state"]["evidence"] == []
    assert result["evidence_dropped"] == 1
    assert result["state"]["confidence"] <= 0.3
    assert "подтверждённые цитаты" in result["state"]["missing"]


def test_unchanged_card_is_skipped_in_dry_run(temp_db, monkeypatch):
    """DRY_RUN обязан пользоваться кэшем, иначе это самый дорогой режим."""
    from config import get_settings

    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    quote = "Созвонился с КЛИЕНТ_1, ищет 2к до 25 млн"
    analyze_buyer_deal(
        _card_with_contact(), llm=_FakeLLM(_state_payload([quote])), prepared=True,
    )

    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()

    class _Boom:
        def invoke(self, messages):
            raise AssertionError("модель не должна вызываться для неизменной карточки")

    again = analyze_buyer_deal(_card_with_contact(), llm=_Boom(), prepared=True)
    assert again["reason"] == "unchanged"

    forced = analyze_buyer_deal(
        _card_with_contact(),
        llm=_FakeLLM(_state_payload([quote])),
        prepared=True,
        force=True,
    )
    assert forced["reason"] != "unchanged"


# ── Бюджет контекста и устойчивость ────────────────────────────────────
def test_trim_events_keeps_the_most_recent():
    from client_state import trim_events_to_budget

    events = [{"text": "x" * 100, "note": ""} for _ in range(10)]
    events[-1]["text"] = "самое свежее"
    kept, dropped = trim_events_to_budget(events, 250)
    assert dropped == 10 - len(kept)
    assert kept[-1]["text"] == "самое свежее"
    assert sum(len(e["text"]) for e in kept) <= 250


def test_trim_keeps_at_least_one_event():
    from client_state import trim_events_to_budget

    kept, dropped = trim_events_to_budget([{"text": "x" * 5000, "note": ""}], 100)
    assert len(kept) == 1 and dropped == 0


def test_collect_failure_skips_only_that_card(temp_db, monkeypatch):
    import client_state as cs

    def _boom(deal, settings=None):
        raise RuntimeError("Bitrix недоступен")

    monkeypatch.setattr(cs, "prepare_deal_record", _boom)
    result = cs.analyze_buyer_deal({"ID": 1}, llm=_FakeLLM(_state_payload([])))
    assert result["skipped"] is True
    assert result["reason"] == "collect_error"


def test_batch_survives_a_failing_card(temp_db, monkeypatch):
    import client_state as cs

    def _explode(deal, **kwargs):
        if deal.get("ID") == 2:
            raise RuntimeError("неожиданная ошибка")
        return {"deal_id": deal.get("ID"), "skipped": False, "reason": "",
                "state": {"recoverable": True}, "content_hash": "h"}

    monkeypatch.setattr(cs, "analyze_buyer_deal", _explode)
    stats = cs.run_buyer_client_state([{"ID": 1}, {"ID": 2}, {"ID": 3}])
    assert stats["total"] == 3
    assert stats["analyzed"] == 2
    assert stats["errors"] == 1


def test_single_oversized_event_is_truncated_not_dropped():
    """Одна длинная расшифровка не должна ни вылетать, ни рвать контекст."""
    from client_state import trim_events_to_budget

    kept, dropped = trim_events_to_budget([{"text": "я" * 5000, "note": ""}], 1000)
    assert dropped == 0
    assert len(kept[0]["text"]) == 1000
    assert kept[0]["truncated"] is True
