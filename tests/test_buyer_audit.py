"""Tests for deterministic buyer deal audit rules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools import check_buyer_deal_violations  # noqa: E402


def _deal(**overrides):
    base = {
        "deal_id": 12454,
        "title": "Test deal",
        "stage_id": "C18:UC_V0DMMX",
        "stage_name": "Подбор",
        "audit_rule": 2,
        "assigned_by_id": 100,
        "date_create": "2026-06-01T10:00:00+00:00",
        "timeline": [],
        "uf_fields": {"Дата встречи": None, "Результат показа": None},
    }
    base.update(overrides)
    return base


CURRENT = "2026-06-08T12:00:00+00:00"


def test_rule2_violation_without_timeline_comment():
    violations = check_buyer_deal_violations([_deal()], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_2"


def test_rule2_no_violation_with_timeline_comment():
    deal = _deal(timeline=[{"author_id": 100, "comment": "Обновление", "created": CURRENT}])
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule3_not_applied_on_podbor_stage():
    deal = _deal(
        audit_rule=2,
        uf_fields={"Дата встречи": "2026-06-15", "Результат показа": None},
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert all(v["rule"] != "buyer_stage_3" for v in violations)


def test_rule3_no_violation_when_show_date_filled():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Показ",
        audit_rule=3,
        uf_fields={"Дата встречи": "2026-06-15T10:00:00+03:00", "Результат показа": None},
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule3_violation_when_show_date_missing():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Показ",
        audit_rule=3,
        uf_fields={"Дата встречи": None, "Результат показа": None},
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_3"
    assert "отсутствует" in violations[0]["reason"]


def test_rule3_violation_when_show_date_overdue():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Показ",
        audit_rule=3,
        uf_fields={"Дата встречи": "2026-06-01", "Результат показа": None},
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["details"]["is_overdue"] is True
