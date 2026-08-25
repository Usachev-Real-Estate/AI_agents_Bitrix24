"""Tests for Spam/Non-target lead quality audit."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lead_quality_audit import (  # noqa: E402
    _is_trivial_text,
    _parse_violations_json,
    _sanitize_quality_violations,
    filter_excluded_findings,
    format_quality_report,
    has_spam_qualification_mark,
    is_agent_probe_note,
    is_quality_candidate,
    is_self_call_or_rating_boost,
    phone_search_variants,
)


def test_trivial_spam_word_only():
    assert _is_trivial_text("спам")
    assert _is_trivial_text("Нецелевой")
    assert _is_trivial_text("BitrixGPT\nспам")


def test_bitrixgpt_summary_not_trivial():
    text = (
        "BitrixGPT\nКлиент интересуется ЖК Событие, бюджет до 45 млн, "
        "район Хамовники, готов смотреть на выходных."
    )
    assert not _is_trivial_text(text)


def test_candidate_with_bitrixgpt_comments():
    lead = {
        "comments_field": "BitrixGPT\nВходящий звонок. Бюджет 30 млн, район Пресня.",
        "timeline": [],
        "has_incoming_call": False,
    }
    assert is_quality_candidate(lead)


def test_candidate_with_incoming_only():
    lead = {
        "comments_field": "",
        "timeline": [],
        "has_incoming_call": True,
    }
    assert is_quality_candidate(lead)


def test_skip_empty_lead():
    lead = {
        "comments_field": "",
        "timeline": [],
        "has_incoming_call": False,
    }
    assert not is_quality_candidate(lead)


def test_skip_trivial_timeline_only():
    lead = {
        "comments_field": "",
        "timeline": [{"author_id": 1, "comment": "спам"}],
        "has_incoming_call": False,
    }
    assert not is_quality_candidate(lead)


def test_self_call_and_rating_excluded():
    assert is_self_call_or_rating_boost("звонок сам себе")
    assert is_self_call_or_rating_boost("Личный звонок")
    assert is_self_call_or_rating_boost("для повышения рейтинга")
    assert is_self_call_or_rating_boost("тестовый звонок / прозвон линии")
    assert not is_self_call_or_rating_boost(
        "Клиент смотрит ЖК Событие, бюджет 40 млн"
    )


def test_candidate_skips_self_call():
    lead = {
        "comments_field": "",
        "timeline": [{"author_id": 1, "comment": "звонок сам себе, повышение рейтинга"}],
        "has_incoming_call": True,
    }
    assert not is_quality_candidate(lead)


def test_candidate_skips_when_deal_by_phone():
    lead = {
        "comments_field": "BitrixGPT бюджет 40 млн",
        "timeline": [],
        "has_incoming_call": True,
        "has_deal_by_phone": True,
        "phones": ["79001234567"],
    }
    assert not is_quality_candidate(lead)


def test_spam_mark_excludes_even_with_incoming_and_bitrixgpt():
    lead = {
        "comments_field": (
            "[p]BitrixGPT Клиент смотрит ЖК, бюджет 40 млн[/p]\nСпам"
        ),
        "timeline": [{"comment": "спам"}],
        "has_incoming_call": True,
        "has_deal_by_phone": False,
    }
    assert has_spam_qualification_mark(lead)
    assert not is_quality_candidate(lead)


def test_spam_mark_detects_short_notes():
    assert has_spam_qualification_mark(
        {"comments_field": "", "timeline": [{"comment": "это СПАМ!!!"}]}
    )
    assert not has_spam_qualification_mark(
        {
            "comments_field": "BitrixGPT бюджет 30 млн район Пресня",
            "timeline": [],
        }
    )


def test_agent_probe_formulations_excluded():
    assert is_agent_probe_note("агент пробивала")
    assert is_agent_probe_note("Агент пробивал номер")
    assert is_agent_probe_note("пробивка агента")
    assert is_agent_probe_note("это агент")
    assert is_agent_probe_note("звонок от агента")
    assert is_agent_probe_note("риелтор пробивал")
    assert is_agent_probe_note("агент")
    assert not is_agent_probe_note(
        "Клиент смотрит ЖК Событие, бюджет 40 млн, район Хамовники"
    )


def test_candidate_skips_agent_probe():
    lead = {
        "comments_field": "",
        "timeline": [{"comment": "агент пробивала"}],
        "has_incoming_call": True,
        "has_deal_by_phone": False,
    }
    assert not is_quality_candidate(lead)


def test_phone_search_variants():
    variants = phone_search_variants("8 (900) 123-45-67")
    assert "79001234567" in variants
    assert "+79001234567" in variants


def test_filter_excluded_findings():
    leads = {
        1: {
            "comments_field": "",
            "timeline": [{"comment": "личный звонок"}],
            "has_deal_by_phone": False,
        },
        2: {
            "comments_field": "BitrixGPT бюджет 30 млн",
            "timeline": [],
            "has_deal_by_phone": False,
        },
        3: {
            "comments_field": "BitrixGPT бюджет 50 млн",
            "timeline": [],
            "has_deal_by_phone": True,
        },
    }
    findings = [
        {"entity_id": 1, "rule": "lead_leaked", "reason": "без отработки"},
        {
            "entity_id": 2,
            "rule": "lead_wrong_qualification",
            # reason may quote exclusion words — must NOT exclude by reason alone
            "reason": "комментарий «личный звонок» не объясняет, клиент целевой",
        },
        {"entity_id": 3, "rule": "lead_leaked", "reason": "есть сделка"},
    ]
    kept = filter_excluded_findings(findings, leads)
    assert len(kept) == 1
    assert kept[0]["entity_id"] == 2


def test_parse_and_sanitize_quality_rules():
    content = """
    {
      "violations": [
        {
          "entity_type": "lead",
          "entity_id": 100,
          "responsible_id": 5,
          "severity": "high",
          "rule": "lead_leaked",
          "reason": "Живой клиент закрыт в «Спам» без отработки."
        },
        {
          "entity_type": "lead",
          "entity_id": 101,
          "responsible_id": 5,
          "rule": "lead_rule_2",
          "reason": "старое правило"
        },
        {
          "entity_id": 0,
          "rule": "lead_wrong_qualification",
          "reason": "bad id"
        }
      ]
    }
    """
    parsed = _parse_violations_json(content)
    clean = _sanitize_quality_violations(parsed)
    assert len(clean) == 1
    assert clean[0]["rule"] == "lead_leaked"
    assert clean[0]["entity_id"] == 100


def test_format_report_empty():
    text = format_quality_report(
        [],
        checked=10,
        candidates=3,
        since="2026-07-01",
        name_map={},
    )
    assert "Новых слитых" in text
    assert "2026-07-01" in text


def test_format_report_with_finding():
    text = format_quality_report(
        [
            {
                "entity_id": 555,
                "responsible_id": 100,
                "rule": "lead_wrong_qualification",
                "reason": "В BitrixGPT есть бюджет и район.",
                "details": {"status_name": "Спам"},
            }
        ],
        checked=20,
        candidates=5,
        since="2026-07-01",
        name_map={100: "Тестов Тест"},
    )
    assert "Неверная квалификация" in text
    assert "#555" in text
    assert "Тестов Тест" in text


def test_quality_audit_sends_to_admin_not_report_chat(monkeypatch):
    from types import SimpleNamespace

    from lead_quality_audit import run_lead_quality_audit

    sent: list[tuple[int, str]] = []

    settings = SimpleNamespace(
        lead_quality_enabled=True,
        lead_quality_since="2026-07-01",
        lead_quality_chunk_size=40,
        dry_run=False,
        contact_source_lock_notify_user=154,
        report_chat_id=22358,
    )
    monkeypatch.setattr("lead_quality_audit.init_db", lambda: None)
    monkeypatch.setattr(
        "lead_quality_audit.collect_spam_nontarget_leads", lambda _since: [],
    )
    monkeypatch.setattr(
        "lead_quality_audit.purge_excluded_stored_findings", lambda _leads: 0,
    )
    monkeypatch.setattr(
        "lead_quality_audit.analyze_leads_with_llm",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr("lead_quality_audit._resolve_names", lambda _ids: {})
    monkeypatch.setattr(
        "lead_quality_audit.send_user_chat_message_chunked",
        lambda uid, msg: sent.append((uid, msg)) or 1,
    )

    result = run_lead_quality_audit(settings)
    assert result["sent"] is True
    assert sent
    assert sent[0][0] == 154
    assert sent[0][0] != 22358


# ── Сбор лидов: контракт с _fetch_entity_timeline ──────────────────────
def test_timeline_fetch_returns_three_values():
    """Контракт, который сломался молча.

    _fetch_entity_timeline стала возвращать (id, timeline, failed), три
    вызова в tools.py обновили, четвёртый — здесь — пропустили. Прод падал
    пять суток при зелёных тестах.
    """
    import inspect

    import tools

    source = inspect.getsource(tools._fetch_entity_timeline)
    assert "return (entity_id, _extract_comments(raw), False)" in source
    assert "return (entity_id, [], True)" in source


def _lead(lead_id: int) -> dict:
    return {
        "ID": str(lead_id), "TITLE": f"Лид {lead_id}", "STATUS_ID": "JUNK",
        "ASSIGNED_BY_ID": "1", "DATE_CREATE": "2026-08-20T10:00:00+03:00",
        "COMMENTS": "клиент интересовался квартирой в центре, просил перезвонить",
        "SOURCE_ID": "CALL",
    }


def _collect(monkeypatch, timeline_result, incoming_result=False):
    import lead_quality_audit as lq

    monkeypatch.setattr(lq, "_fetch_lead_status_names", lambda: {"JUNK": "Спам"})
    monkeypatch.setattr(lq, "_bx_get_all_sync", lambda method, params: [_lead(1)])
    monkeypatch.setattr(lq, "enrich_leads_phone_deal_flags", lambda records: None)

    def _timeline(lead_id, entity_type):
        if isinstance(timeline_result, Exception):
            raise timeline_result
        return timeline_result

    def _incoming(lead_id):
        if isinstance(incoming_result, Exception):
            raise incoming_result
        return incoming_result

    monkeypatch.setattr(lq, "_fetch_entity_timeline", _timeline)
    monkeypatch.setattr(lq, "lead_has_incoming_activity", _incoming)
    return lq.collect_spam_nontarget_leads("2026-08-01")


def test_collect_unpacks_the_timeline_triple(monkeypatch):
    """Тест, которого не было: он бы поймал ValueError на проде."""
    comments = [{"author_id": 1, "comment": "перезвонил", "created": "2026-08-21"}]
    leads = _collect(monkeypatch, (1, comments, False))
    assert len(leads) == 1
    assert leads[0]["timeline"] == comments
    assert leads[0]["evidence_incomplete"] is False


def test_a_failed_timeline_marks_the_lead_incomplete(monkeypatch):
    """Пустой таймлайн из-за сбоя нельзя показывать модели как молчание."""
    leads = _collect(monkeypatch, (1, [], True))
    assert leads[0]["evidence_incomplete"] is True
    assert is_quality_candidate(leads[0]) is False


def test_a_raising_timeline_marks_the_lead_incomplete(monkeypatch):
    leads = _collect(monkeypatch, RuntimeError("Bitrix down"))
    assert leads[0]["evidence_incomplete"] is True


def test_a_failed_incoming_check_also_excludes_the_lead(monkeypatch):
    """has_incoming_call=False уходит в модель как «клиент не звонил»."""
    leads = _collect(
        monkeypatch, (1, [], False), incoming_result=RuntimeError("timeout"),
    )
    assert leads[0]["evidence_incomplete"] is True
    assert is_quality_candidate(leads[0]) is False


def test_a_readable_lead_is_still_a_candidate(monkeypatch):
    """Починка не должна выключить аудит целиком."""
    leads = _collect(monkeypatch, (1, [], False), incoming_result=True)
    assert leads[0]["evidence_incomplete"] is False
    assert is_quality_candidate(leads[0]) is True
