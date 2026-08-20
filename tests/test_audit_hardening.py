"""Regression tests for audit hardening fixes.

Each test pins down a defect that previously produced false violations,
lost data, or crashed report delivery.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notify import _split_message  # noqa: E402
from tools import (  # noqa: E402
    NO_COMMENT_DAYS,
    _author_comment_meets,
    _days_since_last_comment_by_authors,
    _filter_calls_for_entity,
    _position_is_rop,
    build_stage_name_index,
    check_buyer_deal_violations,
    check_missed_callback_violations,
    check_seller_deal_violations,
    humanize_violation_reason,
)

CURRENT = "2026-07-31T12:00:00+00:00"
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def _missed(entity_type: str, entity_id: int):
    return {
        "call_type": "incoming",
        "status": "missed",
        "start_date": "2026-07-30T10:00:00+00:00",
        "crm_entity_type": entity_type,
        "crm_entity_id": entity_id,
    }


# ── Привязка звонков к сущности ────────────────────────────────────────
def test_calls_filtered_to_the_owning_lead():
    calls = [_missed("LEAD", 500), _missed("LEAD", 501), _missed("DEAL", 500)]
    assert _filter_calls_for_entity(calls, "lead", 500) == [calls[0]]


def test_calls_filtered_to_the_owning_deal_numeric_owner_type():
    calls = [_missed("2", 900), _missed("1", 900)]
    assert _filter_calls_for_entity(calls, "deal", 900) == [calls[0]]


def test_unrelated_calls_do_not_reach_other_cards():
    """A missed call on lead 500 must not raise a violation on lead 501."""
    other_lead_calls = _filter_calls_for_entity([_missed("LEAD", 500)], "lead", 501)
    entities = [{"lead_id": 501, "assigned_by_id": 10, "calls": other_lead_calls}]
    assert check_missed_callback_violations(entities, "lead") == []


def test_own_missed_call_still_raises_violation():
    own = _filter_calls_for_entity([_missed("LEAD", 500)], "lead", 500)
    entities = [{"lead_id": 500, "assigned_by_id": 10, "calls": own}]
    violations = check_missed_callback_violations(entities, "lead")
    assert [v["rule"] for v in violations] == ["lead_missed_callback"]


# ── Нечитаемые доказательства не равны их отсутствию ───────────────────
def _seller_deal(**over):
    base = {
        "deal_id": 15000,
        "title": "Seller deal",
        "stage_id": "NEW",
        "stage_name": "Назначение встречи",
        "assigned_by_id": 100,
        "date_create": "2026-07-20T10:00:00+00:00",
        "source_id": "24",
        "category_id": 0,
        "timeline": [],
        "deal_activities": [],
        "open_activities": [],
        "calls": [],
    }
    base.update(over)
    return base


def test_seller_deal_with_unreadable_timeline_is_not_flagged():
    stale = _seller_deal()
    assert check_seller_deal_violations(
        [stale], CURRENT, {}, {},
    ), "контроль: без флага нарушение есть"

    unreadable = _seller_deal(evidence_incomplete=True)
    assert check_seller_deal_violations([unreadable], CURRENT, {}, {}) == []


def test_buyer_deal_with_unreadable_timeline_is_not_flagged():
    base = {
        "deal_id": 16000,
        "title": "Buyer deal",
        "stage_id": "C18:NEW",
        "stage_name": "Подбор",
        "audit_rule": 2,
        "assigned_by_id": 100,
        "date_create": "2026-07-01T10:00:00+00:00",
        "stage_entered_at": "2026-07-01T10:00:00+00:00",
        "timeline": [],
        "deal_activities": [],
        "open_activities": [],
        "responsible_open_activities": [],
        "calls": [],
    }
    assert check_buyer_deal_violations([dict(base)], CURRENT, {}, {})

    unreadable = dict(base, evidence_incomplete=True)
    assert check_buyer_deal_violations([unreadable], CURRENT, {}, {}) == []


# ── Даты комментариев ──────────────────────────────────────────────────
def test_latest_comment_picked_by_time_not_by_string():
    """+03:00 сортируется лексикографически выше, но по времени раньше."""
    timeline = [
        {"author_id": 1, "comment": "раньше", "created": "2026-07-30T10:00:00+03:00"},
        {"author_id": 1, "comment": "позже", "created": "2026-07-30T09:00:00+00:00"},
    ]
    days = _days_since_last_comment_by_authors(timeline, NOW, {1})
    # Позднейший момент — 09:00 UTC 30-го, значит прошло чуть больше суток.
    assert days == 1


def test_comment_without_parsable_date_is_not_counted():
    since = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
    timeline = [{"author_id": 1, "comment": "x" * 50, "created": "не дата"}]
    assert _author_comment_meets(timeline, {1}, 30, since) is False


def test_comment_with_valid_date_after_stage_is_counted():
    since = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
    timeline = [
        {"author_id": 1, "comment": "x" * 50, "created": "2026-07-30T10:00:00+00:00"},
    ]
    assert _author_comment_meets(timeline, {1}, 30, since) is True


def test_no_comments_returns_sentinel():
    assert _days_since_last_comment_by_authors([], NOW, {1}) == NO_COMMENT_DAYS


# ── Определение РОПа ───────────────────────────────────────────────────
def test_rop_detected_by_substring_variants():
    subs = ["РОП", "Руководитель отдела продаж"]
    assert _position_is_rop("Руководитель отдела продаж (РОП)", subs)
    assert _position_is_rop("РОП", subs)
    assert _position_is_rop("руководитель отдела продаж", subs)
    assert not _position_is_rop("Брокер", subs)
    assert not _position_is_rop("", subs)


def test_rop_match_respects_word_boundaries():
    """«роп» встречается внутри обычных слов — это не должность."""
    subs = ["РОП", "Руководитель отдела продаж"]
    assert not _position_is_rop("Менеджер по Европе", subs)
    assert not _position_is_rop("Европа-Тур", subs)
    assert not _position_is_rop("Агроном", subs)


# ── Индекс названий стадий ─────────────────────────────────────────────
def test_prebuilt_name_index_matches_inline_build():
    violation = {
        "entity_type": "deal",
        "entity_id": 1,
        "reason": "Сделка на этапе C18:UC_UFPFKK более 2 дней.",
        "details": {},
    }
    deals = [{"deal_id": 1, "stage_id": "C18:UC_UFPFKK", "stage_name": "Первый показ"}]
    inline = humanize_violation_reason(violation, buyers_deals=deals)
    prebuilt = humanize_violation_reason(
        violation, name_index=build_stage_name_index(buyers_deals=deals),
    )
    assert inline == prebuilt
    assert "«Первый показ»" in prebuilt


# ── Разбиение сообщений ────────────────────────────────────────────────
def test_long_single_line_is_hard_split():
    line = "y" * 9500
    chunks = _split_message(f"шапка\n{line}", 4000)
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(chunks).replace("\n", "") == "шапка" + line


def test_short_message_stays_single_chunk():
    assert _split_message("одна\nдве", 4000) == ["одна\nдве"]


# ── Форматирование счётчика дней в отчёте ──────────────────────────────
def test_reportable_days_guard():
    """Строка в details не должна ронять рассылку отчётов."""
    from graph import _is_reportable_days

    assert _is_reportable_days(3) is True
    assert _is_reportable_days(2.5) is True
    assert _is_reportable_days(NO_COMMENT_DAYS) is False
    assert _is_reportable_days(None) is False
    assert _is_reportable_days("") is False
    assert _is_reportable_days("5") is False   # раньше здесь был TypeError
    assert _is_reportable_days(True) is False
