"""Tests for deterministic buyer deal audit rules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools import (  # noqa: E402
    BUYERS_SKIP_AUDIT_STAGES,
    BUYERS_STAGE_AUDIT_RULE,
    _buyers_audit_rule,
    check_buyer_deal_violations,
)


def _deal(**overrides):
    base = {
        "deal_id": 12454,
        "title": "Test deal",
        "stage_id": "C18:NEW",
        "stage_name": "Подбор",
        "audit_rule": 2,
        "assigned_by_id": 100,
        "date_create": "2026-06-01T10:00:00+00:00",
        "timeline": [],
        "uf_fields": {},
        "open_activities": [],
        "responsible_open_activities": [],
    }
    base.update(overrides)
    return base


CURRENT = "2026-06-08T12:00:00+00:00"


def test_buyers_stage_mapping_covers_new_funnel():
    assert _buyers_audit_rule("C18:NEW") == 2
    assert _buyers_audit_rule("C18:UC_UFPFKK") == 3
    assert _buyers_audit_rule("C18:UC_DVW1P9") == 4
    assert _buyers_audit_rule("C18:UC_8Z3SP6") == 4
    assert _buyers_audit_rule("C18:LOSE") == 5
    assert _buyers_audit_rule("C18:UC_RUCRAH") is None
    assert _buyers_audit_rule("C18:UC_8X12HI") is None
    assert _buyers_audit_rule("C18:WON") is None
    assert _buyers_audit_rule("C18:APOLOGY") is None
    assert "C18:UC_V0DMMX" not in BUYERS_STAGE_AUDIT_RULE
    assert "C18:UC_A15GLR" not in BUYERS_STAGE_AUDIT_RULE


def test_rule1_still_works_when_mapped():
    """Rule 1 kept for legacy; not mapped to any current stage."""
    deal = _deal(
        stage_id="C18:NEW", stage_name="Подбор", audit_rule=1,
        date_create="2026-06-01T10:00:00+00:00",
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_1"


def test_rule2_violation_no_comments_on_podbor():
    deal = _deal(stage_id="C18:NEW", stage_name="Подбор", audit_rule=2, timeline=[])
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_2"
    assert "более 2 дней без комментария" in violations[0]["reason"]


def test_rule2_violation_old_comment():
    deal = _deal(audit_rule=2, timeline=[
        {"author_id": 100, "comment": "x", "created": "2026-05-29T10:00:00+00:00"},
    ])
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert "более 10 дней назад" in violations[0]["reason"]


def test_rule2_no_violation_recent_comment():
    deal = _deal(audit_rule=2, timeline=[
        {"author_id": 100, "comment": "x", "created": "2026-06-06T12:00:00+00:00"},
    ])
    violations = check_buyer_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 44},
    )
    assert violations == []


def test_rule2_no_violation_when_has_non_overdue_contact_plan_activity():
    deal = _deal(
        audit_rule=2,
        timeline=[],
        open_activities=[
            {"ID": 11, "SUBJECT": "План связи с клиентом", "DEADLINE": "2026-06-10T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule2_no_violation_when_contact_plan_exists_for_responsible():
    deal = _deal(
        audit_rule=2,
        timeline=[],
        open_activities=[],
        responsible_open_activities=[
            {"ID": 13, "SUBJECT": "Связаться с клиентом", "DEADLINE": "2026-06-10T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule2_violation_when_contact_plan_activity_overdue():
    deal = _deal(
        audit_rule=2,
        timeline=[],
        open_activities=[
            {"ID": 12, "SUBJECT": "Созвон с клиентом", "DEADLINE": "2026-06-07T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_2"
    assert "просрочено" in violations[0]["reason"]


def test_rule3_violation_without_planned_activity_and_show_date():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Показ",
        audit_rule=3,
        uf_fields={},
        open_activities=[],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_3"
    assert violations[0]["severity"] == "high"
    assert "нет запланированного дела" in violations[0]["reason"]
    assert "не заполнена дата показа" in violations[0]["reason"]
    assert "перенести на другой этап" in violations[0]["reason"]


def test_rule3_no_violation_when_show_date_passed_but_activity_is_actual():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Показ",
        audit_rule=3,
        uf_fields={"Дата встречи": "2026-06-07T09:00:00+00:00"},
        open_activities=[
            {"ID": 1, "DEADLINE": "2026-06-09T12:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule3_violation_when_planned_activity_overdue():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Показ",
        audit_rule=3,
        uf_fields={"Дата встречи": "2026-06-10T09:00:00+00:00"},
        open_activities=[
            {"ID": 2, "DEADLINE": "2026-06-07T09:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert "запланированное дело просрочено" in violations[0]["reason"]


def test_rule3_no_violation_when_show_date_empty_but_activity_is_actual():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Показ",
        audit_rule=3,
        uf_fields={},
        open_activities=[
            {"ID": 4, "DEADLINE": "2026-06-10T12:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule3_no_violation_when_show_date_in_timeline_comment():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Показ",
        audit_rule=3,
        uf_fields={},
        open_activities=[],
        timeline=[
            {"author_id": 100, "comment": "Показ назначен на 27.06", "created": "2026-06-24T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule3_no_violation_when_show_plan_in_comment_without_date():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Показ",
        audit_rule=3,
        uf_fields={},
        open_activities=[],
        timeline=[
            {
                "author_id": 100,
                "comment": "Договариваемся на показ квартиры в ЖК Вишневый сад",
                "created": "2026-06-25T10:21:36+03:00",
            },
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule3_no_violation_with_future_show_date_and_valid_activity():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Показ",
        audit_rule=3,
        uf_fields={"Дата встречи": "2026-06-10T09:00:00+00:00"},
        open_activities=[
            {"ID": 3, "DEADLINE": "2026-06-10T12:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule4_violation_no_comments_on_peregovory():
    deal = _deal(
        stage_id="C18:UC_DVW1P9", stage_name="Переговоры", audit_rule=4,
        timeline=[],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_4"
    assert violations[0]["reason"] == "На этапе «Переговоры» нет комментариев."


def test_rule4_violation_no_comments():
    deal = _deal(
        stage_id="C18:UC_8Z3SP6", stage_name="Офер", audit_rule=4,
        timeline=[],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_4"
    assert violations[0]["reason"] == "На этапе «Офер» нет комментариев."


def test_rule4_violation_old_comment():
    deal = _deal(
        stage_id="C18:UC_L8NX87", stage_name="Дожим!!!", audit_rule=4,
        timeline=[
            {"author_id": 100, "comment": "x", "created": "2026-05-29T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_4"
    assert "более 10 дней назад" in violations[0]["reason"]


def test_rule4_no_violation_recent():
    deal = _deal(
        stage_id="C18:UC_DVW1P9", stage_name="Переговоры", audit_rule=4,
        timeline=[
            {"author_id": 100, "comment": "x", "created": "2026-06-05T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule5_violation():
    deal = _deal(
        stage_id="C18:LOSE", stage_name="Отложенный спрос", audit_rule=5,
        assigned_by_id=100,
        timeline=[
            {"author_id": 200, "comment": "x", "created": "2026-06-01T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_5"


def test_rule2_rop_comment_counts():
    """Комментарий от РОПа засчитывается — нарушения нет."""
    deal = _deal(
        audit_rule=2,
        assigned_by_id=100,
        timeline=[
            {"author_id": 200, "comment": "OK", "created": "2026-06-07T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations(
        [deal],
        CURRENT,
        rop_map={42: 200},
        broker_dept_map={100: 42},
    )
    assert violations == []


def test_rule2_stranger_comment_does_not_count():
    """Комментарий от постороннего не засчитывается — нарушение есть."""
    deal = _deal(
        audit_rule=2,
        assigned_by_id=100,
        timeline=[
            {"author_id": 999, "comment": "Привет", "created": "2026-06-07T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations(
        [deal],
        CURRENT,
        rop_map={42: 200},
        broker_dept_map={100: 42},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_2"


def test_rule5_rop_comment_counts():
    """Комментарий РОПа сбрасывает нарушение на этапе «Отложенный спрос»."""
    deal = _deal(
        stage_id="C18:LOSE",
        stage_name="Отложенный спрос",
        audit_rule=5,
        assigned_by_id=100,
        timeline=[
            {"author_id": 200, "comment": "Проверил", "created": "2026-06-07T10:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations(
        [deal],
        CURRENT,
        rop_map={42: 200},
        broker_dept_map={100: 42},
    )
    assert violations == []


def test_skip_stages_not_audited():
    for stage_id in BUYERS_SKIP_AUDIT_STAGES:
        assert _buyers_audit_rule(stage_id) is None
