"""Tests for buyer client-state agent."""

from __future__ import annotations

import os
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

    monkeypatch.setattr(cs, "analyze_deal", _explode)
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


# ── Гейт по этапу: не платим модели за карточки вне контроля качества ──
def test_out_of_qc_stage_is_skipped_before_the_llm(monkeypatch):
    """Задаток/Сделка/Агент — вердикт всё равно out_of_qc, разбор не нужен."""
    import client_state as cs

    def _boom(*args, **kwargs):  # pragma: no cover — не должен вызваться
        raise AssertionError("модель не должна вызываться на этапе вне QC")

    monkeypatch.setattr(cs, "prepare_deal_record", _boom)
    monkeypatch.setattr(cs, "make_llm", _boom)

    for stage in ("C18:UC_RUCRAH", "C18:UC_8X12HI", "C18:WON", "C18:UC_2ZBA0G"):
        result = cs.analyze_deal({"ID": 77, "STAGE_ID": stage}, profile=cs.BUYER_PROFILE)
        assert result["skipped"] is True, stage
        assert result["reason"] == "stage_out_of_qc", stage
        assert result["verdict"] == "out_of_qc", stage


def test_seller_out_of_qc_stages_are_skipped_before_the_llm(monkeypatch):
    import client_state as cs

    def _boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("модель не должна вызываться на этапе вне QC")

    monkeypatch.setattr(cs, "prepare_deal_record", _boom)
    monkeypatch.setattr(cs, "make_llm", _boom)

    for stage in ("UC_KEOOG8", "UC_FADPBF", "WON"):
        result = cs.analyze_deal({"ID": 78, "STAGE_ID": stage}, profile=cs.SELLER_PROFILE)
        assert result["reason"] == "stage_out_of_qc", stage


def test_stage_without_requirements_still_goes_to_the_llm():
    """Незнакомый этап — «правил ещё нет», а не «не наше дело»: температура нужна."""
    from client_state import stage_skips_analysis
    from funnel_profiles import BUYER_PROFILE

    assert stage_skips_analysis("C18:PREPARATION", BUYER_PROFILE) == ""


def test_empty_stage_does_not_silently_skip_a_card():
    from client_state import stage_skips_analysis
    from funnel_profiles import BUYER_PROFILE, SELLER_PROFILE

    assert stage_skips_analysis("", BUYER_PROFILE) == ""
    assert stage_skips_analysis("", SELLER_PROFILE) == ""


def test_force_overrides_the_stage_gate(monkeypatch):
    """force=True — ручной перезапуск; он должен доходить до разбора."""
    import client_state as cs

    called: list[str] = []

    def _prepare(deal, **kwargs):
        called.append("prepared")
        return {"ID": 79, "STAGE_ID": "C18:WON", "evidence_incomplete": True}

    monkeypatch.setattr(cs, "prepare_deal_record", _prepare)
    result = cs.analyze_deal(
        {"ID": 79, "STAGE_ID": "C18:WON"}, profile=cs.BUYER_PROFILE, force=True,
    )
    assert called == ["prepared"]
    assert result["reason"] == "evidence_incomplete"


def test_run_client_state_counts_stage_skips(monkeypatch):
    import client_state as cs

    monkeypatch.setattr(cs, "prepare_deal_record", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("не должно вызываться"),
    ))
    stats = cs.run_client_state(
        cs.BUYER_PROFILE,
        [{"ID": 1, "STAGE_ID": "C18:WON"}, {"ID": 2, "STAGE_ID": "C18:UC_RUCRAH"}],
    )
    assert stats["skipped_out_of_qc"] == 2
    assert stats["verdicts"]["out_of_qc"] == 2
    assert stats["analyzed"] == 0
    assert stats["skipped_other"] == 0


# ── Телеметрия токенов ─────────────────────────────────────────────────
class _Resp:
    def __init__(self, usage_metadata=None, response_metadata=None):
        self.content = "{}"
        if usage_metadata is not None:
            self.usage_metadata = usage_metadata
        if response_metadata is not None:
            self.response_metadata = response_metadata


def test_usage_read_from_langchain_metadata():
    from client_state import extract_usage

    usage = extract_usage(_Resp(usage_metadata={
        "input_tokens": 1500,
        "output_tokens": 300,
        "input_token_details": {"cache_read": 1200},
    }))
    assert usage == {
        "input_tokens": 1500, "output_tokens": 300, "cached_tokens": 1200,
        "reasoning_tokens": 0,
    }


def test_usage_read_from_openai_style_token_usage():
    from client_state import extract_usage

    usage = extract_usage(_Resp(response_metadata={"token_usage": {
        "prompt_tokens": 900,
        "completion_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 640},
    }}))
    assert usage == {
        "input_tokens": 900, "output_tokens": 120, "cached_tokens": 640,
        "reasoning_tokens": 0,
    }


def test_usage_read_from_deepseek_cache_hit_field():
    from client_state import extract_usage

    usage = extract_usage(_Resp(response_metadata={"token_usage": {
        "prompt_tokens": 800,
        "completion_tokens": 100,
        "prompt_cache_hit_tokens": 512,
    }}))
    assert usage["cached_tokens"] == 512


def test_usage_absent_does_not_raise():
    from client_state import extract_usage

    assert extract_usage(_Resp()) == {
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
        "reasoning_tokens": 0,
    }


def test_invariant_payload_keys_come_first():
    """Префикс запроса должен быть одинаков для всех карточек одного этапа."""
    from client_state import build_llm_payload
    from funnel_profiles import BUYER_PROFILE

    def _payload(deal_id: int) -> str:
        return build_llm_payload(
            {"ID": deal_id, "TITLE": f"Сделка {deal_id}", "STAGE_ID": "C18:NEW"},
            None, [], [], BUYER_PROFILE,
        )

    a, b = _payload(1), _payload(2)
    prefix = os.path.commonprefix([a, b])
    # Общий префикс должен дотягиваться до значения deal_id — то есть весь
    # инвариантный блок (этап + требуемые факты) в него уже вошёл.
    assert prefix.rstrip().endswith('"deal_id":')
    assert '"facts_needed"' in prefix
    assert "следующий шаг с датой" in prefix


def test_run_client_state_sums_token_usage(monkeypatch):
    import client_state as cs

    def _fake(deal, **kwargs):
        return {
            "deal_id": int(deal["ID"]), "skipped": False, "reason": "",
            "state": {"temperature": "warm", "verdict": "good"},
            "content_hash": "x",
            "usage": {
                "input_tokens": 1000, "output_tokens": 200, "cached_tokens": 700,
                "reasoning_tokens": 150,
            },
        }

    monkeypatch.setattr(cs, "analyze_deal", _fake)
    stats = cs.run_client_state(
        cs.BUYER_PROFILE, [{"ID": 1, "STAGE_ID": "C18:NEW"}, {"ID": 2, "STAGE_ID": "C18:NEW"}],
    )
    assert stats["llm_calls"] == 2
    assert stats["usage"] == {
        "input_tokens": 2000, "output_tokens": 400, "cached_tokens": 1400,
        "reasoning_tokens": 300,
    }


def test_skipped_cards_do_not_count_as_llm_calls(monkeypatch):
    """Карточка, отсечённая гейтом или хэшем, запросов не делает."""
    import client_state as cs

    monkeypatch.setattr(cs, "prepare_deal_record", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("не должно вызываться"),
    ))
    stats = cs.run_client_state(cs.BUYER_PROFILE, [{"ID": 1, "STAGE_ID": "C18:WON"}])
    assert stats["llm_calls"] == 0
    assert stats["usage"]["input_tokens"] == 0


# ── Отпечатки событий вместо watermark по дате ─────────────────────────
def _ev(**over: Any) -> dict[str, Any]:
    base = {
        "kind": "comment", "id": 1, "created": "2026-08-01T10:00:00+03:00",
        "text": "созвонился", "status": "", "note": "",
    }
    base.update(over)
    return base


def test_event_with_unparseable_date_is_not_resent_forever():
    """Раньше такое событие уходило в модель на каждом прогоне."""
    from client_state import event_fingerprint, filter_new_events

    broken = _ev(id=7, created="не дата")
    seen = {event_fingerprint(broken)}
    assert filter_new_events([broken], "2026-08-20T00:00:00+00:00", seen) == []


def test_unseen_event_still_goes_to_the_llm():
    from client_state import event_fingerprint, filter_new_events

    old, fresh = _ev(id=1), _ev(id=2, text="показ состоялся")
    seen = {event_fingerprint(old)}
    assert filter_new_events([old, fresh], None, seen) == [fresh]


def test_edited_comment_is_treated_as_new():
    """Текст изменился — отпечаток другой, событие надо перечитать."""
    from client_state import event_fingerprint, filter_new_events

    before = _ev(id=3, text="созвонился")
    after = _ev(id=3, text="созвонился, договорились на показ 25.08")
    seen = {event_fingerprint(before)}
    assert filter_new_events([after], None, seen) == [after]


def test_backdated_event_is_not_missed():
    """Событие старше watermark, но невиданное — по дате его бы потеряли."""
    from client_state import filter_new_events

    backdated = _ev(id=9, created="2026-07-01T10:00:00+03:00")
    assert filter_new_events([backdated], "2026-08-20T00:00:00+00:00", set()) == []
    assert filter_new_events(
        [backdated], "2026-08-20T00:00:00+00:00", {"other"},
    ) == [backdated]


def test_watermark_still_used_when_no_fingerprints_stored():
    """Карточки, разобранные до появления колонки, работают по-старому."""
    from client_state import filter_new_events

    old = _ev(id=1, created="2026-07-01T10:00:00+03:00")
    new = _ev(id=2, created="2026-08-21T10:00:00+03:00")
    got = filter_new_events([old, new], "2026-08-20T00:00:00+00:00", set())
    assert got == [new]


def test_broken_fingerprint_column_falls_back_to_full_reread():
    from client_state import parse_analyzed_events

    assert parse_analyzed_events(None) == set()
    assert parse_analyzed_events("") == set()
    assert parse_analyzed_events("{не json") == set()
    assert parse_analyzed_events('{"a": 1}') == set()
    assert parse_analyzed_events('["a", "b"]') == {"a", "b"}


def test_content_hash_unchanged_by_the_fingerprint_refactor():
    """Хэш карточки — ключ кэша в БД; менять его формат нельзя."""
    from client_state import compute_content_hash

    events = [_ev(id=1), _ev(id=2, text="показ")]
    # Значение снято с реализации ДО выделения _event_identity. Если оно
    # разъедется, кэш в БД инвалидируется целиком и весь портфель уйдёт в
    # модель заново — поэтому пин, а не «лишь бы стабильно».
    assert compute_content_hash(events) == (
        "3df524ace164b0dc6f9ac5e4ebeb5e3b76c19b97ff06039656da5ae8e715eb56"
    )
    assert compute_content_hash(events) != compute_content_hash(events[:1])


def test_analyzed_events_round_trip_through_the_db(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "violations.db")
    db.init_db()
    db.save_client_state(
        deal_id=42, state_json="{}", confidence=0.5, content_hash="h",
        analyzed_at="2026-08-24T10:00:00+00:00", model="m",
        analyzed_events='["aa", "bb"]',
    )
    row = db.get_client_state(42)
    assert row is not None
    assert row["analyzed_events"] == '["aa", "bb"]'


def test_deleted_event_does_not_trigger_a_paid_call(tmp_path, monkeypatch):
    """Удалили комментарий: хэш другой, новых событий нет — модель не зовём."""
    import client_state as cs
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "violations.db")
    db.init_db()

    card = {
        "ID": 501,
        "TITLE": "Покупка",
        "STAGE_ID": "C18:NEW",
        "contacts": [],
        "activities": [],
        "transcripts": [],
        "evidence_incomplete": False,
    }
    two = [
        {"author_id": 1, "created": "2026-08-01T10:00:00+03:00", "comment": "первый"},
        {"author_id": 1, "created": "2026-08-02T10:00:00+03:00", "comment": "второй"},
    ]
    monkeypatch.setattr(cs, "prepare_deal_record", lambda d, **k: {**card, "timeline": two})
    monkeypatch.setattr(cs.get_settings(), "dry_run", False, raising=False)

    calls: list[int] = []

    def _fake_llm(deal, prev, new_events, all_events, model, profile, usage_sink=None):
        calls.append(len(new_events))
        return cs._normalize_state({"client_goal": "2к", "confidence": 0.7}, profile)

    monkeypatch.setattr(cs, "analyze_with_llm", _fake_llm)
    monkeypatch.setattr(cs, "make_llm", lambda s: object())

    settings = cs.get_settings()
    monkeypatch.setattr(settings, "dry_run", False, raising=False)

    first = cs.analyze_deal(dict(card), profile=cs.BUYER_PROFILE, settings=settings)
    assert first["skipped"] is False
    assert calls == [2]

    # Второй комментарий удалили.
    monkeypatch.setattr(cs, "prepare_deal_record", lambda d, **k: {**card, "timeline": two[:1]})
    second = cs.analyze_deal(dict(card), profile=cs.BUYER_PROFILE, settings=settings)
    assert second["reason"] == "no_new_events"
    assert calls == [2], "модель не должна вызываться повторно"

    # Хэш перезаписан — третий прогон уходит в обычный кэш «unchanged».
    third = cs.analyze_deal(dict(card), profile=cs.BUYER_PROFILE, settings=settings)
    assert third["reason"] == "unchanged"
    assert calls == [2]


def test_second_run_sends_only_the_new_comment(tmp_path, monkeypatch):
    """Главный смысл отпечатков: повторный прогон не перечитывает историю."""
    import client_state as cs
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "violations.db")
    db.init_db()

    card = {
        "ID": 502, "TITLE": "Покупка", "STAGE_ID": "C18:NEW",
        "contacts": [], "activities": [], "transcripts": [],
        "evidence_incomplete": False,
    }
    timeline = [
        # Дата, которую не разобрать — раньше такое событие уходило каждый раз.
        {"author_id": 1, "created": "не дата", "comment": "первый"},
        {"author_id": 1, "created": "2026-08-02T10:00:00+03:00", "comment": "второй"},
    ]
    sent: list[list[str]] = []

    def _fake_llm(deal, prev, new_events, all_events, model, profile, usage_sink=None):
        sent.append([str(e.get("text")) for e in new_events])
        return cs._normalize_state({"client_goal": "2к", "confidence": 0.7}, profile)

    monkeypatch.setattr(cs, "analyze_with_llm", _fake_llm)
    monkeypatch.setattr(cs, "make_llm", lambda s: object())
    settings = cs.get_settings()
    monkeypatch.setattr(settings, "dry_run", False, raising=False)

    monkeypatch.setattr(cs, "prepare_deal_record", lambda d, **k: {**card, "timeline": timeline})
    cs.analyze_deal(dict(card), profile=cs.BUYER_PROFILE, settings=settings)

    grown = timeline + [
        {"author_id": 1, "created": "2026-08-03T10:00:00+03:00", "comment": "третий"},
    ]
    monkeypatch.setattr(cs, "prepare_deal_record", lambda d, **k: {**card, "timeline": grown})
    cs.analyze_deal(dict(card), profile=cs.BUYER_PROFILE, settings=settings)

    # Порядок задаёт сортировка по дате; проверяем состав, а не порядок.
    assert sorted(sent[0]) == ["второй", "первый"]
    assert sent[1] == ["третий"], "событие с нечитаемой датой не должно уезжать снова"


def test_reasoning_tokens_are_counted_separately():
    """Размышления — отдельная и самая дорогая строка тарифа RouterAI."""
    from client_state import extract_usage

    langchain_shape = extract_usage(_Resp(usage_metadata={
        "input_tokens": 1000, "output_tokens": 900,
        "output_token_details": {"reasoning": 800},
    }))
    assert langchain_shape["reasoning_tokens"] == 800

    openai_shape = extract_usage(_Resp(response_metadata={"token_usage": {
        "prompt_tokens": 1000, "completion_tokens": 900,
        "completion_tokens_details": {"reasoning_tokens": 800},
    }}))
    assert openai_shape["reasoning_tokens"] == 800


def test_llm_limits_are_passed_only_when_configured(monkeypatch):
    import llm as llm_mod

    captured: dict[str, object] = {}

    class _Fake:
        def __init__(self, **kwargs):
            captured.clear()
            captured.update(kwargs)

    monkeypatch.setattr(llm_mod, "ChatOpenAI", _Fake)
    from config import get_settings

    settings = get_settings()
    llm_mod.make_llm(settings)
    assert captured["max_tokens"] == 16_000
    assert "reasoning_effort" not in captured, "пусто = дефолт провайдера"

    monkeypatch.setattr(settings, "llm_max_tokens", 0, raising=False)
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low", raising=False)
    llm_mod.make_llm(settings)
    assert "max_tokens" not in captured
    assert captured["reasoning_effort"] == "low"
