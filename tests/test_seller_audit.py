"""Tests for seller-funnel deal audit rules and report icons."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools import (  # noqa: E402
    SELLERS_STAGE_CADENCE,
    _is_seller_violation,
    _severity_icon,
    check_seller_deal_violations,
    seller_violation_action,
)


CURRENT = "2026-07-31T12:00:00+00:00"


def _deal(**overrides):
    base = {
        "deal_id": 15000,
        "title": "Seller deal",
        "stage_id": "NEW",
        "stage_name": "Назначение встречи",
        "assigned_by_id": 100,
        "date_create": "2026-07-29T10:00:00+00:00",
        "source_id": "24",
        "category_id": 0,
        "timeline": [],
        "deal_activities": [],
        "open_activities": [],
        "calls": [],
    }
    base.update(overrides)
    return base


def test_cadence_map_covers_reglament_stages():
    from tools import SELLER_STAGE_SEARCH_CLIENT, SELLERS_SKIP_AUDIT_STAGES

    assert "NEW" in SELLERS_STAGE_CADENCE
    assert "UC_KEOOG8" in SELLERS_STAGE_CADENCE
    assert "FINAL_INVOICE" in SELLERS_STAGE_CADENCE
    assert "UC_A94BGF" in SELLERS_STAGE_CADENCE
    assert SELLER_STAGE_SEARCH_CLIENT not in SELLERS_STAGE_CADENCE
    assert SELLER_STAGE_SEARCH_CLIENT in SELLERS_SKIP_AUDIT_STAGES


def test_meeting_stale_after_24h():
    deal = _deal()
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_stage_stale"
    assert violations[0]["severity"] == "high"
    assert violations[0]["details"]["funnel"] == "sellers"
    assert "Назначение встречи" in violations[0]["reason"]
    assert "комментария брокера/РОПа" in violations[0]["reason"]
    assert "непросроченного дела" in violations[0]["reason"]


def test_meeting_ok_with_fresh_comment():
    deal = _deal(
        timeline=[
            {
                "author_id": 100,
                "comment": "Договорились созвониться завтра",
                "created": "2026-07-31T00:00:00+00:00",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_meeting_live_activity_counts():
    deal = _deal(
        deal_activities=[
            {
                "ID": 1,
                "RESPONSIBLE_ID": 100,
                "AUTHOR_ID": 100,
                "CREATED": "2026-07-30T18:00:00+00:00",
                "DEADLINE": "2026-07-31T18:00:00+00:00",
                "COMPLETED": "N",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_meeting_open_activity_without_deadline_counts():
    deal = _deal(
        deal_activities=[
            {
                "ID": 1,
                "RESPONSIBLE_ID": 100,
                "CREATED": "2026-07-30T18:00:00+00:00",
                "COMPLETED": "N",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_meeting_overdue_activity_does_not_count():
    deal = _deal(
        deal_activities=[
            {
                "ID": 1,
                "RESPONSIBLE_ID": 100,
                "AUTHOR_ID": 100,
                "CREATED": "2026-07-29T18:00:00+00:00",
                "DEADLINE": "2026-07-31T11:00:00+00:00",
                "COMPLETED": "N",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_stage_stale"


def test_meeting_grace_under_24h():
    deal = _deal(date_create="2026-07-31T10:00:00+00:00")
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_outgoing_call_alone_does_not_waive():
    deal = _deal(calls=[{"call_type": "outgoing", "crm_entity_id": 15000}])
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_stage_stale"


def test_non_paid_source_is_audited():
    deal = _deal(source_id="CALL")
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_stage_stale"


def test_negotiations_stale_after_3_days():
    deal = _deal(
        stage_id="UC_KEOOG8",
        stage_name="Переговоры",
        date_create="2026-07-20T10:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Старый комментарий",
                "created": "2026-07-27T10:00:00+00:00",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_stage_stale"
    assert "Переговоры" in violations[0]["reason"]


def test_search_client_stale_after_7_days():
    deal = _deal(
        stage_id="UC_FADPBF",
        stage_name="Поиск клиента",
        date_create="2026-07-01T10:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Неделю назад",
                "created": "2026-07-23T10:00:00+00:00",
            },
        ],
        uf_fields={"ID Афины": "99"},
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_search_client_fresh_within_7_days():
    deal = _deal(
        stage_id="UC_FADPBF",
        stage_name="Поиск клиента",
        date_create="2026-07-01T10:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Недавно",
                "created": "2026-07-28T10:00:00+00:00",
            },
        ],
        uf_fields={"ID Афины": "99"},
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_deferred_no_open_activity():
    deal = _deal(
        stage_id="LOSE",
        stage_name="Отложенная продажа",
        source_id="CALL",
        timeline=[
            {
                "author_id": 100,
                "comment": "Отложили",
                "created": "2026-07-30T10:00:00+00:00",
            },
        ],
        deal_activities=[],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_deferred_no_activity"
    assert violations[0]["severity"] == "medium"


def test_deferred_with_open_activity_ok():
    deal = _deal(
        stage_id="LOSE",
        stage_name="Отложенная продажа",
        source_id="CALL",
        deal_activities=[
            {
                "ID": 9,
                "RESPONSIBLE_ID": 100,
                "COMPLETED": "N",
                "CREATED": "2026-07-30T10:00:00+00:00",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_won_skipped_apology_audited():
    deals = [
        _deal(stage_id="WON", stage_name="Договор закрыт"),
        _deal(deal_id=15001, stage_id="APOLOGY", stage_name="Сделка проиграна"),
    ]
    violations = check_seller_deal_violations(
        deals, CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "seller_lost_no_reason"
    assert violations[0]["entity_id"] == 15001


def test_seller_lost_reason_comment_seconds_before_stage_counts():
    deal = _deal(
        deal_id=15002,
        stage_id="APOLOGY",
        stage_name="Сделка проиграна",
        stage_entered_at="2026-07-30T12:00:08+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "не продает, отказ",
                "created": "2026-07-30T12:00:00+00:00",
            },
        ],
    )
    assert check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    ) == []


def test_seller_lost_old_comment_counts():
    deal = _deal(
        deal_id=15003,
        stage_id="APOLOGY",
        stage_name="Сделка проиграна",
        stage_entered_at="2026-07-30T12:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Согласовали встречу на четверг",
                "created": "2026-07-20T10:00:00+00:00",
            },
        ],
    )
    assert check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    ) == []


def test_seller_lost_activity_counts():
    deal = _deal(
        deal_id=15004,
        stage_id="APOLOGY",
        stage_name="Сделка проиграна",
        timeline=[],
        deal_activities=[
            {
                "ID": 1,
                "RESPONSIBLE_ID": 100,
                "CREATED": "2026-07-20T10:00:00+00:00",
                "COMPLETED": "Y",
            },
        ],
    )
    assert check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    ) == []


def test_severity_icons_sellers_blue():
    assert _severity_icon("high", seller=True) == "🔵"
    assert _severity_icon("medium", seller=True) == "🟦"
    assert _severity_icon("high", seller=False) == "🔴"


def test_is_seller_violation():
    assert _is_seller_violation({"rule": "seller_stage_stale"})
    assert _is_seller_violation({"rule": "seller_deferred_no_activity"})
    assert _is_seller_violation({"rule": "seller_negotiations_max"})
    assert _is_seller_violation({"rule": "seller_afina_id_missing"})
    assert not _is_seller_violation({"rule": "buyer_stage_2"})
    assert not _is_seller_violation({"rule": "general_base_no_plan"})
    assert _is_seller_violation(
        {"rule": "x", "details": {"funnel": "sellers"}},
    )


def test_seller_violation_action_text():
    assert seller_violation_action({"rule": "seller_stage_stale"}) == (
        "Добавить комментарий или дело в карточку сделки"
    )
    assert seller_violation_action({
        "rule": "seller_stage_stale",
        "details": {"stage_id": "NEW"},
    }) == (
        "Добавить комментарий брокера/РОПа или запланировать "
        "непросроченное дело"
    )
    assert seller_violation_action({
        "rule": "seller_stage_stale",
        "details": {"stage_id": "UC_KEOOG8"},
    }) == "Добавить комментарий в карточку сделки"
    assert seller_violation_action({
        "rule": "seller_stage_stale",
        "details": {"stage_id": "FINAL_INVOICE"},
    }) == "Добавить комментарий или дело в карточку сделки"
    assert seller_violation_action({"rule": "seller_deferred_no_activity"}) == (
        "Запланировать дело в карточке сделки"
    )
    assert seller_violation_action({"rule": "seller_negotiations_max"}) == (
        "Перевести сделку с «Переговоры» (лимит 14 дней)"
    )
    assert seller_violation_action({"rule": "seller_lost_no_reason"}) == (
        "Добавить комментарий брокера/РОПа или дело в карточку"
    )
    assert seller_violation_action({"rule": "seller_afina_id_missing"}) == (
        "Заполнить поле «ID Афины» в карточке сделки"
    )


def test_negotiations_max_after_14_days_even_with_fresh_comment():
    deal = _deal(
        stage_id="UC_KEOOG8",
        stage_name="Переговоры",
        date_create="2026-07-01T10:00:00+00:00",
        stage_entered_at="2026-07-10T10:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Вчера созвонились",
                "created": "2026-07-30T10:00:00+00:00",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert [v["rule"] for v in violations] == ["seller_negotiations_max"]


def test_lost_ok_with_reason_comment():
    deal = _deal(
        stage_id="APOLOGY",
        stage_name="Сделка проиграна",
        timeline=[
            {
                "author_id": 100,
                "comment": "Собственник снял объект с продажи",
                "created": "2026-07-31T10:00:00+00:00",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_closed_sale_requires_afina_id():
    deal = _deal(
        stage_id="UC_A94BGF",
        stage_name="Закрытая продажа (На сайт)",
        date_create="2026-07-28T10:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Разместили на сайт",
                "created": "2026-07-30T10:00:00+00:00",
            },
        ],
        uf_fields={},
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert [v["rule"] for v in violations] == ["seller_afina_id_missing"]


def test_search_client_afina_filled_via_deal_uf_code():
    """Deal field is UF_CRM_1780911079, not the lead code UF_CRM_1780911032."""
    from tools import SELLERS_AFINA_UF, _afina_id_filled

    assert SELLERS_AFINA_UF == "UF_CRM_1780911079"
    deal = _deal(
        stage_id="UC_FADPBF",
        stage_name="Поиск клиента",
        date_create="2026-07-28T10:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "В работе",
                "created": "2026-07-30T10:00:00+00:00",
            },
        ],
        uf_fields={},
        **{SELLERS_AFINA_UF: "600403"},
    )
    assert _afina_id_filled(deal) is True
    assert check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    ) == []
    lead_code_only = _deal(
        stage_id="UC_FADPBF",
        stage_name="Поиск клиента",
        date_create="2026-07-28T10:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "В работе",
                "created": "2026-07-30T10:00:00+00:00",
            },
        ],
        uf_fields={},
        UF_CRM_1780911032="600403",
    )
    assert _afina_id_filled(lead_code_only) is False
    assert check_seller_deal_violations(
        [lead_code_only], CURRENT, rop_map={}, broker_dept_map={100: 1},
    ) == []


def test_search_client_ok_with_comment_and_afina():
    deal = _deal(
        stage_id="UC_FADPBF",
        stage_name="Поиск клиента",
        date_create="2026-07-01T10:00:00+00:00",
        timeline=[
            {
                "author_id": 100,
                "comment": "Недавно",
                "created": "2026-07-28T10:00:00+00:00",
            },
        ],
        uf_fields={"ID Афины": "12345"},
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_search_client_activity_does_not_replace_comment():
    deal = _deal(
        stage_id="UC_FADPBF",
        stage_name="Поиск клиента",
        date_create="2026-07-01T10:00:00+00:00",
        timeline=[],
        deal_activities=[
            {
                "ID": 1,
                "RESPONSIBLE_ID": 100,
                "CREATED": "2026-07-30T10:00:00+00:00",
                "COMPLETED": "N",
            },
        ],
        uf_fields={"ID Афины": "12345"},
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_meeting_reminder_window():
    from tools import list_seller_meeting_reminders

    deal = _deal(date_create="2026-07-30T13:00:00+00:00")
    reminders = list_seller_meeting_reminders(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert len(reminders) == 1
    assert reminders[0]["assigned_by_id"] == 100


def test_meeting_reminder_skipped_when_broker_commented():
    from tools import list_seller_meeting_reminders

    deal = _deal(
        date_create="2026-07-30T13:00:00+00:00",
        timeline=[{"author_id": 100, "comment": "Назначил встречу"}],
    )
    assert list_seller_meeting_reminders(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    ) == []


def test_meeting_reminder_skipped_when_rop_commented():
    from tools import list_seller_meeting_reminders

    deal = _deal(
        date_create="2026-07-30T13:00:00+00:00",
        timeline=[{"author_id": 200, "comment": "РОП назначил встречу"}],
    )
    assert list_seller_meeting_reminders(
        [deal], CURRENT, rop_map={1: 200}, broker_dept_map={100: 1},
    ) == []


def test_meeting_reminder_skipped_when_live_activity():
    from tools import list_seller_meeting_reminders

    deal = _deal(
        date_create="2026-07-30T13:00:00+00:00",
        deal_activities=[
            {
                "ID": 1,
                "RESPONSIBLE_ID": 100,
                "DEADLINE": "2026-07-31T18:00:00+00:00",
                "COMPLETED": "N",
            },
        ],
    )
    assert list_seller_meeting_reminders(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    ) == []


def test_search_client_never_moves_to_general_base(monkeypatch):
    from tools import move_seller_deal_to_general_base

    class _Settings:
        dry_run = False

    monkeypatch.setattr("tools.get_settings", lambda: _Settings())
    result = move_seller_deal_to_general_base(99, "UC_FADPBF")
    assert result["skipped"] is True
    assert result["reason"] == "search_client_never_move"


def test_meeting_ok_with_rop_comment():
    deal = _deal(
        timeline=[
            {
                "author_id": 200,
                "comment": "РОП проверил карточку",
                "created": "2026-07-31T00:00:00+00:00",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={1: 200}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_prep_ads_ok_with_fresh_activity():
    deal = _deal(
        stage_id="FINAL_INVOICE",
        stage_name="Подготовка объекта в рекламу",
        date_create="2026-07-01T10:00:00+00:00",
        timeline=[],
        deal_activities=[
            {
                "ID": 1,
                "RESPONSIBLE_ID": 100,
                "CREATED": "2026-07-28T10:00:00+00:00",
                "COMPLETED": "N",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert violations == []


def test_negotiations_activity_does_not_replace_comment():
    deal = _deal(
        stage_id="UC_KEOOG8",
        stage_name="Переговоры",
        date_create="2026-07-20T10:00:00+00:00",
        timeline=[],
        deal_activities=[
            {
                "ID": 1,
                "RESPONSIBLE_ID": 100,
                "CREATED": "2026-07-30T10:00:00+00:00",
                "COMPLETED": "N",
            },
        ],
    )
    violations = check_seller_deal_violations(
        [deal], CURRENT, rop_map={}, broker_dept_map={100: 1},
    )
    assert "seller_stage_stale" in [v["rule"] for v in violations]


def test_move_seller_deal_dry_run_targets_sellers_stage(monkeypatch):
    class _Settings:
        dry_run = True

    monkeypatch.setattr("tools.get_settings", lambda: _Settings())
    from tools import (
        GENERAL_BASE_CATEGORY_ID,
        GENERAL_BASE_SELLERS_STAGE_ID,
        move_seller_deal_to_general_base,
    )

    result = move_seller_deal_to_general_base(77, "NEW")
    assert result["dry_run_skipped"] is True
    assert result["category_id"] == GENERAL_BASE_CATEGORY_ID
    assert result["stage_id"] == GENERAL_BASE_SELLERS_STAGE_ID
    assert result["stage_id"] == "C26:NEW"


def test_process_sellers_moves_each_deal_once(monkeypatch):
    from tools import process_deals_to_general_base

    monkeypatch.setattr("tools.is_general_base_move_enabled", lambda: True)

    called: list[tuple[int, str]] = []

    def _fake_move(deal_id: int, stage_id: str = ""):
        called.append((deal_id, stage_id))
        return {
            "dry_run_skipped": True,
            "deal_id": deal_id,
            "stage_id": "C26:NEW",
        }

    monkeypatch.setattr("tools.move_seller_deal_to_general_base", _fake_move)
    violations = [
        {
            "entity_id": 11,
            "rule": "seller_stage_stale",
            "reason": "нет комментария",
            "details": {"stage_id": "NEW"},
        },
        {
            "entity_id": 11,
            "rule": "seller_afina_id_missing",
            "reason": "нет ID Афины",
            "details": {"stage_id": "NEW"},
        },
        {
            "entity_id": 12,
            "rule": "seller_negotiations_max",
            "reason": "более 14 дней",
            "details": {"stage_id": "UC_KEOOG8"},
        },
    ]
    process_deals_to_general_base(violations, "sellers")
    assert called == [(11, "NEW"), (12, "UC_KEOOG8")]
    assert all("Общая база" in v["reason"] for v in violations)
    assert violations[0]["details"]["dry_run_skipped"] is True


def test_process_sellers_skips_search_client(monkeypatch):
    from tools import process_deals_to_general_base

    monkeypatch.setattr("tools.is_general_base_move_enabled", lambda: True)

    called: list[int] = []
    monkeypatch.setattr(
        "tools.move_seller_deal_to_general_base",
        lambda deal_id, stage_id="": called.append(deal_id) or {"ok": True},
    )
    violations = [
        {
            "entity_id": 99,
            "rule": "seller_stage_stale",
            "reason": "нет комментария",
            "details": {"stage_id": "UC_FADPBF"},
        },
    ]
    process_deals_to_general_base(violations, "sellers")
    assert called == []
    assert "Общая база" not in violations[0]["reason"]


def test_process_sellers_skips_afina_and_lost(monkeypatch):
    from tools import process_deals_to_general_base

    monkeypatch.setattr("tools.is_general_base_move_enabled", lambda: True)
    called: list[int] = []
    monkeypatch.setattr(
        "tools.move_seller_deal_to_general_base",
        lambda deal_id, stage_id="": called.append(deal_id) or {"ok": True},
    )
    violations = [
        {
            "entity_id": 1,
            "rule": "seller_afina_id_missing",
            "reason": "нет ID Афины",
            "details": {"stage_id": "UC_A94BGF"},
        },
        {
            "entity_id": 2,
            "rule": "seller_lost_no_reason",
            "reason": "нет причины",
            "details": {"stage_id": "APOLOGY"},
        },
    ]
    process_deals_to_general_base(violations, "sellers")
    assert called == []
    assert all("Общая база" not in v["reason"] for v in violations)


def test_process_deals_skipped_before_move_after(monkeypatch):
    from tools import process_deals_to_general_base

    monkeypatch.setattr("tools.is_general_base_move_enabled", lambda: False)
    called: list[int] = []
    monkeypatch.setattr(
        "tools.move_seller_deal_to_general_base",
        lambda deal_id, stage_id="": called.append(deal_id) or {"ok": True},
    )
    violations = [
        {
            "entity_id": 5,
            "rule": "seller_stage_stale",
            "reason": "нет комментария",
            "details": {"stage_id": "NEW"},
        },
    ]
    process_deals_to_general_base(violations, "sellers")
    assert called == []
    assert "Общая база" not in violations[0]["reason"]
