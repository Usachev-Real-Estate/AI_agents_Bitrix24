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
        "stage_entered_at": "2026-06-06T10:00:00+00:00",
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
    assert _buyers_audit_rule("C18:UC_UFPFKK") == 3  # Первый показ
    assert _buyers_audit_rule("C18:UC_DVW1P9") == 3  # Повторный показ — как Первый показ
    assert _buyers_audit_rule("C18:LOSE") == 5
    assert _buyers_audit_rule("C18:UC_8Z3SP6") == 6  # Офер
    assert _buyers_audit_rule("C18:UC_RUCRAH") is None
    assert _buyers_audit_rule("C18:UC_8X12HI") is None
    assert _buyers_audit_rule("C18:WON") is None
    assert _buyers_audit_rule("C18:APOLOGY") == 7
    assert _buyers_audit_rule("C18:UC_2ZBA0G") == 8
    assert "C18:UC_V0DMMX" not in BUYERS_STAGE_AUDIT_RULE
    assert "C18:UC_A15GLR" not in BUYERS_STAGE_AUDIT_RULE
    assert "C18:UC_L8NX87" not in BUYERS_STAGE_AUDIT_RULE
    assert "C18:UC_8Z3SP6" in BUYERS_STAGE_AUDIT_RULE
    assert "C18:UC_8Z3SP6" not in BUYERS_SKIP_AUDIT_STAGES
    assert "C18:APOLOGY" not in BUYERS_SKIP_AUDIT_STAGES
    assert "C18:UC_2ZBA0G" not in BUYERS_SKIP_AUDIT_STAGES


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
    assert "более 2 дней" in violations[0]["reason"]
    assert "без комментария ответственного" in violations[0]["reason"]


def test_rule2_no_violation_when_deal_younger_than_2_days_without_comment():
    """Deal #15526-style: age < 2d, only non-broker timeline → not a violation."""
    deal = _deal(
        stage_id="C18:NEW",
        stage_name="Подбор",
        audit_rule=2,
        date_create="2026-06-07T12:00:00+00:00",
        timeline=[
            {
                "author_id": 154,
                "comment": "https://cian.ru/...",
                "created": "2026-06-07T12:00:00+00:00",
            },
        ],
    )
    violations = check_buyer_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 44},
    )
    assert violations == []


def test_rule2_violation_old_comment():
    deal = _deal(audit_rule=2, timeline=[
        {"author_id": 100, "comment": "x", "created": "2026-05-29T10:00:00+00:00"},
    ])
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert "более 10 дней назад" in violations[0]["reason"]
    assert "ответственного" in violations[0]["reason"]


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


def test_rule2_no_violation_for_fresh_deal_even_with_foreign_overdue_contact_plan():
    """Deal #15750-style: age < 2d; overdue «Связаться…» on another contact must not flag."""
    deal = _deal(
        deal_id=15750,
        stage_id="C18:NEW",
        stage_name="Подбор",
        audit_rule=2,
        date_create="2026-06-08T10:00:00+00:00",  # ~2h before CURRENT
        timeline=[],
        open_activities=[],
        responsible_open_activities=[
            {
                "ID": 42518,
                "OWNER_TYPE_ID": 3,  # contact
                "OWNER_ID": 171316,
                "SUBJECT": "Связаться с клиентом",
                "DEADLINE": "2026-04-17T15:00:00+03:00",
            },
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule2_ignores_overdue_contact_plan_on_other_entities():
    """Overdue contact-plan on a contact must not add «просрочено» for this deal."""
    deal = _deal(
        deal_id=15750,
        audit_rule=2,
        timeline=[],
        open_activities=[],
        responsible_open_activities=[
            {
                "ID": 42502,
                "OWNER_TYPE_ID": 3,
                "OWNER_ID": 171216,
                "SUBJECT": "Связаться с собственником",
                "DEADLINE": "2026-04-11T14:00:00+03:00",
            },
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_2"
    assert "просрочено" not in violations[0]["reason"]
    assert "более 2 дней" in violations[0]["reason"]


def test_rule3_violation_without_planned_activity_and_show_date():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Первый показ",
        audit_rule=3,
        uf_fields={},
        open_activities=[],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_3"
    assert violations[0]["severity"] == "high"
    assert "нет запланированного дела" in violations[0]["reason"]
    assert "дата показа" not in violations[0]["reason"]
    assert "будет перенесена в воронку «Общая база»" in violations[0]["reason"]


def test_rule3_no_violation_when_show_date_passed_but_activity_is_actual():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Первый показ",
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
        stage_name="Первый показ",
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
        stage_name="Первый показ",
        audit_rule=3,
        uf_fields={},
        open_activities=[
            {"ID": 4, "DEADLINE": "2026-06-10T12:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule3_comment_without_activity_is_violation():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Первый показ",
        audit_rule=3,
        uf_fields={},
        open_activities=[],
        timeline=[
            {
                "author_id": 100,
                "comment": "Показ назначен на 27.06",
                "created": "2026-06-24T10:00:00+00:00",
            },
        ],
        stage_entered_at="2026-06-20T10:00:00+00:00",
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_3"
    assert "нет запланированного дела" in violations[0]["reason"]


def test_rule3_show_plan_comment_without_activity_is_violation():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Первый показ",
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
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_3"


def test_rule3_no_violation_with_future_show_date_and_valid_activity():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Первый показ",
        audit_rule=3,
        uf_fields={"Дата встречи": "2026-06-10T09:00:00+00:00"},
        open_activities=[
            {"ID": 3, "DEADLINE": "2026-06-10T12:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule3_applies_to_repeat_show_like_first():
    """Повторный показ: без дела и даты — как первый показ."""
    deal = _deal(
        stage_id="C18:UC_DVW1P9",
        stage_name="Повторный показ",
        audit_rule=3,
        uf_fields={},
        open_activities=[],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_3"
    assert "Повторный показ" in violations[0]["reason"]
    assert "нет запланированного дела" in violations[0]["reason"]


def test_rule3_repeat_show_ok_with_activity_and_show_date():
    deal = _deal(
        stage_id="C18:UC_DVW1P9",
        stage_name="Повторный показ",
        audit_rule=3,
        uf_fields={"Дата встречи": "2026-06-10T12:00:00+00:00"},
        open_activities=[
            {"ID": 5, "DEADLINE": "2026-06-10T12:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert violations == []


def test_rule3_repeat_show_overdue_activity():
    deal = _deal(
        stage_id="C18:UC_DVW1P9",
        stage_name="Повторный показ",
        audit_rule=3,
        open_activities=[
            {"ID": 6, "DEADLINE": "2026-06-01T12:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert "просрочено" in violations[0]["reason"]


def test_offer_stage_requires_long_comment():
    deal = _deal(
        stage_id="C18:UC_8Z3SP6",
        stage_name="Офер",
        audit_rule=6,
        timeline=[],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_ofer_comment"


def test_offer_stage_ok_with_long_comment():
    deal = _deal(
        stage_id="C18:UC_8Z3SP6",
        stage_name="Офер",
        audit_rule=6,
        timeline=[
            {
                "author_id": 100,
                "comment": "Отправили офер по ЖК Символ, клиент думает до пятницы.",
                "created": "2026-06-08T10:00:00+00:00",
            },
        ],
    )
    assert check_buyer_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 44},
    ) == []


def test_rule5_violation_without_planned_activity():
    deal = _deal(
        stage_id="C18:LOSE",
        stage_name="Отложенный спрос",
        audit_rule=5,
        assigned_by_id=100,
        timeline=[
            {
                "author_id": 100,
                "comment": "свежий комментарий",
                "created": "2026-06-08T10:00:00+00:00",
            },
        ],
        open_activities=[],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_5"
    assert "нет запланированного дела" in violations[0]["reason"]


def test_rule5_no_violation_with_active_activity():
    deal = _deal(
        stage_id="C18:LOSE",
        stage_name="Отложенный спрос",
        audit_rule=5,
        timeline=[],
        open_activities=[
            {"ID": 9, "SUBJECT": "Связаться позже", "DEADLINE": "2026-06-20T12:00:00+00:00"},
        ],
    )
    assert check_buyer_deal_violations([deal], CURRENT) == []


def test_rule5_violation_when_activity_overdue():
    deal = _deal(
        stage_id="C18:LOSE",
        stage_name="Отложенный спрос",
        audit_rule=5,
        open_activities=[
            {"ID": 10, "DEADLINE": "2026-06-01T12:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert "просрочено" in violations[0]["reason"]


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


def test_rule5_comment_does_not_clear_without_activity():
    """На Отложенном спросе комментарий РОПа больше не спасает — нужно дело."""
    deal = _deal(
        stage_id="C18:LOSE",
        stage_name="Отложенный спрос",
        audit_rule=5,
        assigned_by_id=100,
        timeline=[
            {"author_id": 200, "comment": "Проверил", "created": "2026-06-07T10:00:00+00:00"},
        ],
        open_activities=[],
    )
    violations = check_buyer_deal_violations(
        [deal],
        CURRENT,
        rop_map={42: 200},
        broker_dept_map={100: 42},
    )
    assert len(violations) == 1
    assert "нет запланированного дела" in violations[0]["reason"]


def test_podbor_stale_after_7_days_on_stage():
    deal = _deal(
        stage_id="C18:NEW",
        stage_name="Подбор",
        audit_rule=2,
        date_create="2026-05-20T10:00:00+00:00",
        stage_entered_at="2026-05-30T10:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "свежий комментарий",
                "created": "2026-06-07T12:00:00+00:00",
            },
        ],
    )
    violations = check_buyer_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 44},
    )
    assert [v["rule"] for v in violations] == ["buyer_podbor_stale"]


def test_rule3_first_show_allows_deadline_beyond_14_days():
    deal = _deal(
        stage_id="C18:UC_UFPFKK",
        stage_name="Первый показ",
        audit_rule=3,
        stage_entered_at="2026-06-01T10:00:00+00:00",
        uf_fields={"Дата встречи": "2026-06-20T09:00:00+00:00"},
        open_activities=[
            {"ID": 3, "DEADLINE": "2026-06-20T12:00:00+00:00"},
        ],
    )
    assert check_buyer_deal_violations([deal], CURRENT) == []


def test_rule3_repeat_show_still_checks_14_days_horizon():
    deal = _deal(
        stage_id="C18:UC_DVW1P9",
        stage_name="Повторный показ",
        audit_rule=3,
        stage_entered_at="2026-06-01T10:00:00+00:00",
        uf_fields={"Дата встречи": "2026-06-20T09:00:00+00:00"},
        open_activities=[
            {"ID": 33, "DEADLINE": "2026-06-20T12:00:00+00:00"},
        ],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_stage_3"
    assert "14 дней" in violations[0]["reason"]


def test_lost_stage_requires_reason_comment():
    deal = _deal(
        stage_id="C18:APOLOGY",
        stage_name="Сделка проиграна",
        audit_rule=7,
        timeline=[],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_lost_no_reason"


def test_lost_reason_comment_seconds_before_stage_counts():
    """Brokers comment the reason, then immediately move to Проиграна."""
    deal = _deal(
        stage_id="C18:APOLOGY",
        stage_name="Сделка проиграна",
        audit_rule=7,
        stage_entered_at="2026-06-08T12:00:10+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Отказ от собственницы, цена не устроила",
                "created": "2026-06-08T12:00:00+00:00",
            },
        ],
    )
    assert check_buyer_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 44},
    ) == []


def test_lost_old_comment_counts():
    deal = _deal(
        stage_id="C18:APOLOGY",
        stage_name="Сделка проиграна",
        audit_rule=7,
        stage_entered_at="2026-06-08T12:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Согласовали показ на 20:00",
                "created": "2026-06-06T10:00:00+00:00",
            },
        ],
    )
    assert check_buyer_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 44},
    ) == []


def test_lost_activity_counts():
    deal = _deal(
        stage_id="C18:APOLOGY",
        stage_name="Сделка проиграна",
        audit_rule=7,
        timeline=[],
        deal_activities=[
            {
                "ID": 1,
                "RESPONSIBLE_ID": 100,
                "CREATED": "2026-06-01T10:00:00+00:00",
                "COMPLETED": "Y",
            },
        ],
    )
    assert check_buyer_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 44},
    ) == []


def test_lost_other_author_comment_does_not_count():
    deal = _deal(
        stage_id="C18:APOLOGY",
        stage_name="Сделка проиграна",
        audit_rule=7,
        timeline=[
            {
                "author_id": 999,
                "comment": "Покупка неактуальна",
                "created": "2026-06-08T11:00:00+00:00",
            },
        ],
    )
    violations = check_buyer_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 44},
    )
    assert [v["rule"] for v in violations] == ["buyer_lost_no_reason"]


def test_agent_stage_requires_comment():
    deal = _deal(
        stage_id="C18:UC_2ZBA0G",
        stage_name="Агент",
        audit_rule=8,
        timeline=[],
    )
    violations = check_buyer_deal_violations([deal], CURRENT)
    assert len(violations) == 1
    assert violations[0]["rule"] == "buyer_agent_no_comment"


def test_skip_stages_not_audited():
    for stage_id in BUYERS_SKIP_AUDIT_STAGES:
        assert _buyers_audit_rule(stage_id) is None


def test_move_buyer_deal_dry_run_targets_buyers_stage(monkeypatch):
    class _Settings:
        dry_run = True

    monkeypatch.setattr("tools.get_settings", lambda: _Settings())
    from tools import (
        GENERAL_BASE_CATEGORY_ID,
        GENERAL_BASE_BUYERS_STAGE_ID,
        move_buyer_deal_to_general_base,
    )

    result = move_buyer_deal_to_general_base(88, "C18:NEW")
    assert result["dry_run_skipped"] is True
    assert result["category_id"] == GENERAL_BASE_CATEGORY_ID
    assert result["stage_id"] == GENERAL_BASE_BUYERS_STAGE_ID
    assert result["stage_id"] == "C26:PREPARATION"


def test_process_buyers_skips_missed_callback(monkeypatch):
    from tools import process_deals_to_general_base

    monkeypatch.setattr("tools.is_general_base_move_enabled", lambda: True)

    called: list[int] = []
    monkeypatch.setattr(
        "tools.move_buyer_deal_to_general_base",
        lambda deal_id, stage_id="": called.append(deal_id) or {"ok": True},
    )
    violations = [
        {
            "entity_id": 50,
            "rule": "buyer_missed_callback",
            "reason": "пропущенный звонок",
            "details": {"stage_id": "C18:NEW"},
        },
        {
            "entity_id": 51,
            "rule": "buyer_podbor_stale",
            "reason": "более 7 дней",
            "details": {"stage_id": "C18:NEW"},
        },
    ]
    process_deals_to_general_base(violations, "buyers")
    assert called == [51]
    assert "Общая база" not in violations[0]["reason"]
    # Сделка уже перенесена — констатация, а не предупреждение о будущем.
    assert "Сделка перенесена в воронку «Общая база»" in violations[1]["reason"]
    assert "будет перенесена" not in violations[1]["reason"]


def test_move_warning_stays_future_tense_when_not_moved(monkeypatch):
    """DRY_RUN: перенос не выполнен — предупреждение остаётся в будущем времени."""
    from tools import process_deals_to_general_base

    monkeypatch.setattr("tools.is_general_base_move_enabled", lambda: True)
    monkeypatch.setattr(
        "tools.move_buyer_deal_to_general_base",
        lambda deal_id, stage_id="": {"dry_run_skipped": True},
    )
    violations = [{
        "entity_id": 52,
        "rule": "buyer_podbor_stale",
        "reason": "более 7 дней",
        "details": {"stage_id": "C18:NEW"},
    }]
    process_deals_to_general_base(violations, "buyers")
    assert "будет перенесена в воронку «Общая база»" in violations[0]["reason"]
    assert "Сделка перенесена" not in violations[0]["reason"]


def test_prebaked_warning_is_replaced_after_a_real_move(monkeypatch):
    """buyer_stage_3 вшивает предупреждение в текст до попытки переноса."""
    from tools import GENERAL_BASE_MOVE_WARNING, process_deals_to_general_base

    monkeypatch.setattr("tools.is_general_base_move_enabled", lambda: True)
    monkeypatch.setattr(
        "tools.move_buyer_deal_to_general_base",
        lambda deal_id, stage_id="": {"ok": True},
    )
    violations = [{
        "entity_id": 53,
        "rule": "buyer_stage_3",
        "reason": f"На этапе «Первый показ» нет дела. {GENERAL_BASE_MOVE_WARNING}",
        "details": {"stage_id": "C18:UC_UFPFKK"},
    }]
    process_deals_to_general_base(violations, "buyers")
    reason = violations[0]["reason"]
    assert "будет перенесена" not in reason, reason
    assert reason.count("Общая база") == 1, reason
    assert "Сделка перенесена в воронку «Общая база»" in reason


def test_process_buyers_skips_ofer_and_lost(monkeypatch):
    from tools import process_deals_to_general_base

    monkeypatch.setattr("tools.is_general_base_move_enabled", lambda: True)
    called: list[int] = []
    monkeypatch.setattr(
        "tools.move_buyer_deal_to_general_base",
        lambda deal_id, stage_id="": called.append(deal_id) or {"ok": True},
    )
    violations = [
        {
            "entity_id": 70,
            "rule": "buyer_ofer_comment",
            "reason": "короткий комментарий",
            "details": {"stage_id": "C18:UC_8Z3SP6"},
        },
        {
            "entity_id": 71,
            "rule": "buyer_lost_no_reason",
            "reason": "нет причины",
            "details": {"stage_id": "C18:APOLOGY"},
        },
    ]
    process_deals_to_general_base(violations, "buyers")
    assert called == []
    assert all("Общая база" not in v["reason"] for v in violations)
